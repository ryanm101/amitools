from LabelManager import LabelManager
from LabelRange import LabelRange
from MemoryAlloc import MemoryAlloc
from MainMemory import MainMemory
from AmigaLibrary import AmigaLibrary
from LibManager import LibManager
from SegmentLoader import SegmentLoader
from VamosContext import VamosContext
from PathManager import PathManager
from FileManager import FileManager
from LockManager import LockManager
from PortManager import PortManager
from AccessMemory import AccessMemory
from AccessStruct import AccessStruct
from ErrorTracker import ErrorTracker

# lib
from lib.ExecLibrary import ExecLibrary
from lib.DosLibrary import DosLibrary
from structure.ExecStruct import *
from structure.DosStruct import *

from Log import *

class Vamos:
  
  def __init__(self, ram_size, cpu):
    self.cpu = cpu
    # create a label manager
    self.label_mgr = LabelManager()
    # set a label for first two dwords
    label = LabelRange("zero_page",0,8)
    self.label_mgr.add_label(label)
    
    # create memory and allocate RAM
    log_mem_init.info("setting up main memory with %s KiB RAM" % ram_size)
    self.error_tracker = ErrorTracker(cpu)
    self.mem = MainMemory(self.label_mgr, self.error_tracker)
    self.mem.init_ram(ram_size)

    # create memory allocator
    self.mem_begin = 0x1000
    self.alloc = MemoryAlloc(self.mem, 0, ram_size * 1024, self.mem_begin, self.label_mgr)
    
    # create segment loader
    self.seg_loader = SegmentLoader( self.mem, self.alloc, self.label_mgr )

    # lib manager
    self.lib_mgr = LibManager( self.label_mgr )

  def load_main_binary(self, bin_file):
    self.bin_file = bin_file
    log_main.info("loading binary: %s", bin_file)
    self.bin_seg_list = self.load_seg(bin_file)
    if self.bin_seg_list == None:
      return False
    self.prog_start = self.bin_seg_list[0].addr
    return True

  def load_seg(self, bin_file):
    # load binary
    seg_list = self.seg_loader.load_seg(bin_file)
    if seg_list == None:
      log_main.error("failed loading binary: '%s' %s", bin_file, seg_loader.error)
      return None
    log_mem_init.info("binary segments: %s",bin_file)
    for s in seg_list:
      log_mem_init.info(s.label)
    return seg_list
    
  # stack size in KiB
  def init_stack(self, stack_size=4):
    # --- setup stack ---
    self.last_addr = 0x0
    self.stack_size = stack_size * 1024
    self.stack = self.alloc.alloc_memory( "stack", self.stack_size )
    self.stack_base = self.stack.addr
    self.stack_end = self.stack_base + self.stack_size
    log_mem_init.info(self.stack)
    # prepare stack
    # TOP: size
    # TOP-4: return from program -> magic_ed
    self.stack_initial = self.stack_end - 4
    self.mem.access.w32(self.stack_initial, self.stack_size)
    self.stack_initial -= 4
    self.mem.access.w32(self.stack_initial, self.last_addr)

  def init_args(self, bin_args):
    # setup arguments
    self.bin_args = bin_args
    self.arg_text = " ".join(bin_args) + "\n" # AmigaDOS appends a new line to the end
    self.arg_len  = len(self.arg_text)
    self.arg_size = self.arg_len + 1
    self.arg = self.alloc.alloc_memory("args", self.arg_size)
    self.arg_base = self.arg.addr
    self.mem.access.w_cstr(self.arg_base, self.arg_text)
    log_main.info("args: %s (%d)", self.arg_text, self.arg_size)
    log_mem_init.info(self.arg)

  def init_managers(self, prefix):
    self.prefix = prefix
    self.path_mgr = PathManager(prefix)

    self.lock_base = self.mem.reserve_special_range()
    self.lock_size = 0x010000
    self.lock_mgr = LockManager(self.path_mgr, self.lock_base, self.lock_size)
    label = LabelRange("lock", self.lock_base, self.lock_size)
    self.label_mgr.add_label(label)
    log_mem_init.info(label)
    
    self.file_base = self.mem.reserve_special_range()
    self.file_size = 0x010000
    self.file_mgr = FileManager(self.path_mgr, self.file_base, self.file_size)
    label = LabelRange("file", self.file_base, self.file_size)
    self.label_mgr.add_label(label)
    log_mem_init.info(label)

    self.port_base = self.mem.reserve_special_range()
    self.port_size = 0x010000
    self.port_mgr  = PortManager(self.port_base, self.port_size)
    label = LabelRange("port", self.port_base, self.port_size)
    self.label_mgr.add_label(label)
    log_mem_init.info(label)

  def register_base_libs(self, exec_version, dos_version):
    # register libraries
    # exec
    self.exec_lib_def = ExecLibrary(self.lib_mgr, self.alloc, version=exec_version)
    self.exec_lib_def.set_managers(self.port_mgr)
    self.lib_mgr.register_int_lib(self.exec_lib_def)
    # dos
    self.dos_lib_def = DosLibrary(self.mem, self.alloc, version=dos_version)
    self.dos_lib_def.set_managers(self.path_mgr, self.lock_mgr, self.file_mgr, self.port_mgr)
    self.lib_mgr.register_int_lib(self.dos_lib_def)

  def init_context(self):
    self.ctx = VamosContext( self.cpu, self.mem, self.lib_mgr, self.alloc )
    self.ctx.bin_args = self.bin_args
    self.ctx.bin_file = self.bin_file
    self.ctx.seg_loader = self.seg_loader
    self.ctx.path_mgr = self.path_mgr
    self.ctx.mem = self.mem
    self.mem.ctx = self.ctx
    return self.ctx

  def setup_process(self):
    # create CLI
    self.cli = self.alloc.alloc_struct("CLI",CLIDef)
    self.cli.access.w_s("cli_DefaultStack", self.stack_size / 4) # in longs
    self.cmd = self.alloc.alloc_bstr("cmd",self.bin_file)
    self.cli.access.w_s("cli_CommandName", self.cmd.addr)
    log_mem_init.info(self.cli)

    # create my task structure
    self.this_task = self.alloc.alloc_struct("ThisTask",ProcessDef)
    self.this_task.access.w_s("pr_CLI", self.cli.addr)
    self.ctx.this_task = self.this_task
    log_mem_init.info(self.this_task)

  def open_exec_lib(self):
    # open exec lib
    self.exec_lib = self.lib_mgr.open_lib(ExecLibrary.name, 0, self.ctx)
    log_mem_init.info(self.exec_lib)

  def create_old_dos_guard(self):
    # create a guard memory for tracking invalid old dos access
    self.dos_guard_base = self.mem.reserve_special_range()
    self.dos_guard_size = 0x010000
    label = LabelRange("old_dos",self.dos_guard_base, self.dos_guard_size)
    self.label_mgr.add_label(label)
    log_mem_init.info(label)

