import time
import sys
from musashi import emu
from musashi.m68k import *
from .regs import *
from .opcodes import *
from .error import ErrorReporter
from .cpustate import CPUState
from amitools.vamos.Exceptions import *
from amitools.vamos.Log import log_machine
from amitools.vamos.label import LabelManager


class RunState(object):
  def __init__(self, name, pc, sp, ret_addr):
    self.name = name
    self.pc = pc
    self.sp = sp
    self.ret_addr = ret_addr
    self.error = None
    self.done = False
    self.cycles = 0
    self.time_delta = 0
    self.regs = None

  def __str__(self):
    return "RunState('%s', pc=%06x,sp=%06x,ret_addr=%06x,error=%s,done=%s," \
        "cycles=%s,time_delta=%s,regs=%s)" % \
        (self.name, self.pc, self.sp, self.ret_addr, self.error, self.done,
         self.cycles, self.time_delta, self.regs)


class Machine(object):
  """the main interface to the m68k emulation including CPU, memory,
     and traps. The machine does only a minimal setup of RAM and the CPU.
     It provides a way to run m68k code.

     Minimal Memory Layout:
     ----------------------
     000000    SP before Reset / Later mem0
     000004    PC before Reset / Later mem4

     000008    Exception Vectors
     ......    mapped to RESET @ 0x400
     0003FC

     000400    RESET opcode for run nesting 0
     000402    RESET opcode for run nesting 1
     ...
     00041C    RESET opcode for run nesting 15
     000420    RESET opcode for Exception Vectors
     000422    TRAP (autorts) for shutdown func

     000800    RAM begin. useable by applications
  """

  CPU_TYPE_68000 = M68K_CPU_TYPE_68000
  CPU_TYPE_68020 = M68K_CPU_TYPE_68020
  CPU_TYPE_68030 = M68K_CPU_TYPE_68030

  run_reset_addr = 0x400
  run_max_nesting = 16
  reset_exvec_addr = 0x420
  shutdown_trap_addr = 0x422
  ram_begin = 0x800

  def __init__(self, cpu_type=M68K_CPU_TYPE_68000, ram_size_kib=1024,
               use_labels=True, raise_on_main_run=True):
    # setup musashi components
    self.cpu_type = cpu_type
    self.cpu = emu.CPU(cpu_type)
    self.mem = emu.Memory(ram_size_kib)
    self.traps = emu.Traps()
    self.raise_on_main_run = raise_on_main_run
    # internal state
    if use_labels:
      self.label_mgr = LabelManager()
    else:
      self.label_mgr = None
    self.ram_total = ram_size_kib * 1024
    self.ram_bytes = self.ram_total - self.ram_begin
    self.error_reporter = ErrorReporter(self)
    self.run_states = []
    self.mem0 = 0
    self.mem4 = 0
    self.shutdown_func = None
    self.instr_hook = None
    self.cycles_per_run = 1000
    self.bail_out = False
    # call init
    self._alloc_trap()
    self._init_base_mem()
    self._setup_handler()

  def cleanup(self):
    """clean up after use"""
    self._free_trap()
    self.cpu.cleanup()
    self.mem.cleanup()
    self.traps.cleanup()
    self.cpu = None
    self.mem = None
    self.traps = None

  def _alloc_trap(self):
    self.shutdown_tid = self.traps.setup(self._shutdown_trap, auto_rts=True)

  def _free_trap(self):
    self.traps.free(self.shutdown_tid)

  def _init_base_mem(self):
    m = self.mem
    # m68k exception vector table
    addr = 8
    for i in xrange(254):
      m.w32(addr, self.reset_exvec_addr)
    # run reset table
    addr = self.run_reset_addr
    for i in xrange(self.run_max_nesting):
      m.w16(addr, op_reset)
      addr += 2
    assert addr == self.reset_exvec_addr
    # reset exc vector
    m.w16(self.reset_exvec_addr, op_reset)
    # trap for shutdown
    opc = op_trap | self.shutdown_tid
    m.w16(self.shutdown_trap_addr, opc)

  def _setup_handler(self):
    # reset opcode handler
    self.cpu.set_reset_instr_callback(self._reset_opcode_handler)
    # set invalid access handler for memory
    self.mem.set_invalid_func(self._invalid_mem_access)
    # set traps exception handler
    self.traps.set_exc_func(self._trap_exc_handler)

  def get_cpu(self):
    return self.cpu

  def get_cpu_type(self):
    return self.cpu_type

  def get_mem(self):
    return self.mem

  def get_traps(self):
    return self.traps

  def get_label_mgr(self):
    return self.label_mgr

  def get_ram_begin(self):
    """start of useable RAM for applications"""
    return self.ram_begin

  def get_ram_bytes(self):
    """number of useable bytes for applications starting at ram begin"""
    return self.ram_bytes

  def get_ram_total(self):
    """number of total bytes in RAM including zero range"""
    return self.ram_total

  def set_zero_mem(self, mem0, mem4):
    """define the long words at memory address 0 and 4 that are written
       after a reset was performed. On Amiga typically 0 and ExecBase.
    """
    self.mem0 = mem0
    self.mem4 = mem4

  def get_zero_mem(self):
    return self.mem0, self.mem4

  def set_cycles_per_run(self, num):
    self.cycles_per_run = num

  def set_shutdown_hook(self, func):
    """set a function to trigger before top-level run ends.
       its a trap that still runs during m68k execution.
    """
    self.shutdown_func = func

  def set_instr_hook(self, func):
    self.cpu.set_instr_hook_callback(func)

  def show_instr(self, show_regs=False):
    if show_regs:
      state = CPUState()
      def instr_hook():
        state.get(self.cpu)
        res = state.dump()
        for r in res:
          log_machine.info(r)
        pc = self.cpu.r_pc()
        _, txt = self.cpu.disassemble(pc)
        log_machine.info("%06x: %s", pc, txt)
    else:
      def instr_hook():
        pc = self.cpu.r_pc()
        _, txt = self.cpu.disassemble(pc)
        log_machine.info("%06x: %s", pc, txt)
    self.set_instr_hook(instr_hook)

  def set_cpu_mem_trace_hook(self, func):
    self.mem.set_trace_mode(1)
    self.mem.set_trace_func(func)

  def get_cur_run_state(self):
    assert len(self.run_states) > 0
    return self.run_states[-1]

  def _shutdown_trap(self, op, pc):
    log_machine.debug("trigger shutdown func")
    self.shutdown_func()

  def _invalid_mem_access(self, mode, width, addr):
    log_machine.debug("invalid memory access: mode=%s width=%d addr=%06x",
                      mode, width, addr)
    run_state = self.get_cur_run_state()
    # already a pending error?
    if run_state.error:
      return
    run_state.error = InvalidMemoryAccessError(mode, width, addr)
    run_state.done = True
    # end time slice of cpu
    self.cpu.end()
    # report error
    self.error_reporter.report_error(run_state.error)

  def _trap_exc_handler(self, op, pc):
    log_machine.debug("trap exception handler: op=%04x pc=%06x", op, pc)
    run_state = self.get_cur_run_state()
    # get pending exception
    exc_info = sys.exc_info()
    if exc_info:
      run_state.error = exc_info[1]
    run_state.done = True
    # end time slice of cpu
    self.cpu.end()
    # report error
    self.error_reporter.report_error(run_state.error)

  def _reset_opcode_handler(self):
    run_state = self.get_cur_run_state()
    # get current pc
    pc = self.cpu.r_pc() - 2
    sp = self.cpu.r_reg(REG_A7)
    callee_pc = self.mem.r32(sp)
    # get current run state
    ret_addr = run_state.ret_addr
    log_machine.debug("reset handler: pc=%06x sp=%06x callee=%06x ret_pc=%06x",
                      pc, sp, callee_pc, ret_addr)
    if pc == ret_addr:
      run_state.done = True
      self.cpu.end()
      return
    # m68k Exception Triggered
    elif pc == self.reset_exvec_addr:
      exc_num = callee_pc >> 2
      txt = "m68k Exception #%d" % exc_num
    # some other unexpected RESET opcode found
    else:
      txt = "Unexpected RESET opcode"
    # report error
    run_state.error = InvalidCPUStateError(pc, txt)
    run_state.done = True
    self.cpu.end()
    # report error
    self.error_reporter.report_error(run_state.error)

  def get_run_nesting(self):
    return len(self.run_states)

  def run(self, pc, sp=None, set_regs=None, get_regs=None,
          max_cycles=0, cycles_per_run=0, name=None):
    mem = self.mem
    cpu = self.cpu

    if name is None:
      name = "default"

    # current run nesting level
    nesting = len(self.run_states)

    # return reset opcode for this run
    ret_addr = self.run_reset_addr + nesting * 4

    # get cpu context
    if nesting > 0:
      cpu_ctx = cpu.get_cpu_context()
    else:
      cpu_ctx = None

    # share stack with last run if not specified
    if sp is None:
      if nesting == 0:
        raise ValueError("stack must be specified!")
      else:
        sp = cpu.r_reg(REG_A7)
        sp -= 4

    log_machine.info("run#%d(%s): begin pc=%06x, sp=%06x, ret_addr=%06x",
                     nesting, name, pc, sp, ret_addr)

    # store return address on stack
    mem.w32(sp, ret_addr)
    # if a shutdown func is set then push its trap addr to stack, too
    if self.shutdown_func and nesting == 0:
      sp -= 4
      mem.w32(sp, self.shutdown_trap_addr)

    # pulse reset to setup PC, SP and restore mem0,4
    mem.w32(0, sp)
    mem.w32(4, pc)
    self.cpu.pulse_reset()
    mem.w32(0, self.mem0)
    mem.w32(4, self.mem4)

    # create run state for this run and push it
    run_state = RunState(name, pc, sp, ret_addr)
    self.run_states.append(run_state)

    # setup regs
    if set_regs:
      log_machine.info("run#%d: set_regs=%s", nesting, set_regs)
      for reg in set_regs:
        val = set_regs[reg]
        cpu.w_reg(reg, val)

    # main execution loop of run
    total_cycles = 0
    if not cycles_per_run:
      cycles_per_run = self.cycles_per_run
    start_time = time.clock()
    try:
      while not run_state.done:
        log_machine.debug("+ cpu.execute")
        total_cycles += cpu.execute(cycles_per_run)
        log_machine.debug("- cpu.execute")
        # end after enough cycles
        if max_cycles > 0 and total_cycles >= max_cycles:
          break
    except Exception as e:
      self.error_reporter.report_error(e)
    end_time = time.clock()

    # retrieve regs
    if get_regs:
      regs = {}
      for reg in get_regs:
        val = cpu.r_reg(reg)
        regs[reg] = val
      log_machine.info("run #%d: get_regs=%s", nesting, regs)
      run_state.regs = regs

    # restore cpu context
    if cpu_ctx:
      cpu.set_cpu_context(cpu_ctx)

    # update run state
    run_state.time_delta = end_time - start_time
    run_state.cycles = total_cycles
    # pop
    self.run_states.pop()

    log_machine.info("run #%d(%s): end. state=%s", nesting, name, run_state)

    # if run_state has error and we are not a top-level raise an error
    # so the running trap code gets aborted and propagates the abort
    if run_state.error:
      if nesting > 0 or self.raise_on_main_run:
        pc = cpu.r_pc()
        raise NestedCPURunError(pc, run_state.error)

    return run_state