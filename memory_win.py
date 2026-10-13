"""Bounded Windows process I/O and reversible patches for the DD1 trainer."""
from __future__ import annotations
import ctypes as C
from ctypes import wintypes as W
from dataclasses import dataclass
from contextlib import contextmanager
import os, struct

class MemoryError(RuntimeError): pass

if os.name == 'nt':
    K = C.WinDLL('kernel32', use_last_error=True)
    N = C.WinDLL('ntdll')
    K.OpenProcess.argtypes=[W.DWORD,W.BOOL,W.DWORD];K.OpenProcess.restype=W.HANDLE
    K.CloseHandle.argtypes=[W.HANDLE];K.CloseHandle.restype=W.BOOL
    K.ReadProcessMemory.argtypes=[W.HANDLE,C.c_void_p,C.c_void_p,C.c_size_t,C.POINTER(C.c_size_t)];K.ReadProcessMemory.restype=W.BOOL
    K.WriteProcessMemory.argtypes=K.ReadProcessMemory.argtypes;K.WriteProcessMemory.restype=W.BOOL
    K.VirtualAllocEx.argtypes=[W.HANDLE,C.c_void_p,C.c_size_t,W.DWORD,W.DWORD];K.VirtualAllocEx.restype=C.c_void_p
    K.VirtualFreeEx.argtypes=[W.HANDLE,C.c_void_p,C.c_size_t,W.DWORD];K.VirtualFreeEx.restype=W.BOOL
    K.VirtualProtectEx.argtypes=[W.HANDLE,C.c_void_p,C.c_size_t,W.DWORD,C.POINTER(W.DWORD)];K.VirtualProtectEx.restype=W.BOOL
    K.FlushInstructionCache.argtypes=[W.HANDLE,C.c_void_p,C.c_size_t];K.FlushInstructionCache.restype=W.BOOL
    K.QueryFullProcessImageNameW.argtypes=[W.HANDLE,W.DWORD,W.LPWSTR,C.POINTER(W.DWORD)];K.QueryFullProcessImageNameW.restype=W.BOOL
    K.GetExitCodeProcess.argtypes=[W.HANDLE,C.POINTER(W.DWORD)];K.GetExitCodeProcess.restype=W.BOOL
    K.CreateToolhelp32Snapshot.argtypes=[W.DWORD,W.DWORD];K.CreateToolhelp32Snapshot.restype=W.HANDLE
    N.NtSuspendProcess.argtypes=[W.HANDLE];N.NtSuspendProcess.restype=C.c_long
    N.NtResumeProcess.argtypes=[W.HANDLE];N.NtResumeProcess.restype=C.c_long

class MODULEENTRY32(C.Structure):
    _fields_=[('dwSize',W.DWORD),('th32ModuleID',W.DWORD),('th32ProcessID',W.DWORD),('GlblcntUsage',W.DWORD),('ProccntUsage',W.DWORD),('modBaseAddr',C.c_void_p),('modBaseSize',W.DWORD),('hModule',W.HMODULE),('szModule',W.WCHAR*256),('szExePath',W.WCHAR*260)]

class PROCESSENTRY32(C.Structure):
    _fields_=[('dwSize',W.DWORD),('cntUsage',W.DWORD),('th32ProcessID',W.DWORD),('th32DefaultHeapID',C.c_size_t),('th32ModuleID',W.DWORD),('cntThreads',W.DWORD),('th32ParentProcessID',W.DWORD),('pcPriClassBase',C.c_long),('dwFlags',W.DWORD),('szExeFile',W.WCHAR*260)]

def query_process_image(pid):
    """Return the executable path using query-only rights, without memory access."""
    if os.name!='nt':raise MemoryError('需要Windows')
    handle=K.OpenProcess(0x1000,False,int(pid))
    if not handle:raise MemoryError('无法读取游戏程序路径，错误码 '+str(C.get_last_error()))
    try:
        size=W.DWORD(32768);path=C.create_unicode_buffer(size.value)
        if not K.QueryFullProcessImageNameW(handle,0,path,C.byref(size)):
            raise MemoryError('游戏程序路径读取失败，错误码 '+str(C.get_last_error()))
        return path.value
    finally:K.CloseHandle(handle)

def find_game_pids():
    if os.name!='nt':return []
    K.CreateToolhelp32Snapshot.argtypes=[W.DWORD,W.DWORD];K.CreateToolhelp32Snapshot.restype=W.HANDLE
    K.Process32FirstW.argtypes=[W.HANDLE,C.POINTER(PROCESSENTRY32)];K.Process32FirstW.restype=W.BOOL
    K.Process32NextW.argtypes=K.Process32FirstW.argtypes;K.Process32NextW.restype=W.BOOL
    h=K.CreateToolhelp32Snapshot(2,0)
    if h==C.c_void_p(-1).value:raise MemoryError('进程列表读取失败')
    try:
        x=PROCESSENTRY32();x.dwSize=C.sizeof(x);out=[];ok=K.Process32FirstW(h,C.byref(x))
        while ok:
            if x.szExeFile.lower()=='darkest.exe':out.append(x.th32ProcessID)
            ok=K.Process32NextW(h,C.byref(x))
        return out
    finally:K.CloseHandle(h)

def module_info(pid):
    K.Module32FirstW.argtypes=[W.HANDLE,C.POINTER(MODULEENTRY32)];K.Module32FirstW.restype=W.BOOL
    h=K.CreateToolhelp32Snapshot(0x18,pid)
    if h==C.c_void_p(-1).value:raise MemoryError('模块列表读取失败；请使用64位游戏')
    try:
        x=MODULEENTRY32();x.dwSize=C.sizeof(x)
        if not K.Module32FirstW(h,C.byref(x)):raise MemoryError('游戏模块尚未准备好')
        return int(x.modBaseAddr),int(x.modBaseSize),x.szExePath
    finally:K.CloseHandle(h)

class Process:
    def __init__(self,pid):
        if os.name!='nt' or C.sizeof(C.c_void_p)!=8:raise MemoryError('需要64位Windows')
        self.pid=pid;self.handle=K.OpenProcess(0x438|0x800,False,pid);self.allocations=[]
        if not self.handle:raise MemoryError('无法连接游戏，错误码 '+str(C.get_last_error()))
        try:self.base,self.size,self.path=module_info(pid)
        except Exception:
            K.CloseHandle(self.handle);self.handle=None
            raise
    def read(self,address,size):
        if not 0<=size<=16*1024*1024:raise MemoryError('读取范围超出限制')
        out=C.create_string_buffer(size);done=C.c_size_t()
        if not K.ReadProcessMemory(self.handle,address,out,size,C.byref(done)) or done.value!=size:
            raise MemoryError('游戏内存读取失败 @'+hex(address))
        return out.raw
    def write(self,address,data):
        data=bytes(data);done=C.c_size_t()
        if len(data)>1024*1024:raise MemoryError('写入范围超出限制')
        buf=C.create_string_buffer(data)
        if not K.WriteProcessMemory(self.handle,address,buf,len(data),C.byref(done)) or done.value!=len(data):
            raise MemoryError('游戏内存写入失败 @'+hex(address))
    def u64(self,a):return struct.unpack('<Q',self.read(a,8))[0]
    def u32(self,a):return struct.unpack('<I',self.read(a,4))[0]
    def f32(self,a):return struct.unpack('<f',self.read(a,4))[0]
    def alive(self):
        c=W.DWORD();return bool(self.handle and K.GetExitCodeProcess(self.handle,C.byref(c)) and c.value==259)
    def allocate(self,size=4096,near=None):
        if near is not None:
            # Small exact reservations around the module; never overwrite an existing mapping.
            for offset in range(0x4000000,0x70000000,0x1000000):
                addr=(near+offset)&~0xffff
                p=K.VirtualAllocEx(self.handle,addr,size,0x3000,0x40)
                if p:self.allocations.append((p,size));return p
        p=K.VirtualAllocEx(self.handle,None,size,0x3000,0x40)
        if not p:raise MemoryError('修改器临时内存分配失败')
        self.allocations.append((p,size));return p
    @contextmanager
    def paused(self):
        if N.NtSuspendProcess(self.handle)<0:raise MemoryError('无法安全暂停游戏线程')
        try:yield
        finally:
            if N.NtResumeProcess(self.handle)<0:raise MemoryError('游戏线程恢复失败')
    def write_code(self,a,data):
        old=W.DWORD()
        if not K.VirtualProtectEx(self.handle,a,len(data),0x40,C.byref(old)):raise MemoryError('无法临时写入指令')
        try:self.write(a,data);K.FlushInstructionCache(self.handle,a,len(data))
        finally:
            ignored=W.DWORD();K.VirtualProtectEx(self.handle,a,len(data),old.value,C.byref(ignored))
    def close(self,free=True):
        if self.handle:
            if free and self.alive():
                for a,_ in reversed(self.allocations):K.VirtualFreeEx(self.handle,a,0,0x8000)
            K.CloseHandle(self.handle);self.handle=None

def absolute_jump(address):return b'\xff\x25\x00\x00\x00\x00'+struct.pack('<Q',address)

@dataclass
class Patch:
    address:int
    original:bytes
    replacement:bytes
    def install(self,p):
        if p.read(self.address,len(self.original))!=self.original:raise MemoryError('指令校验失败；版本或其他修改器冲突')
        p.write_code(self.address,self.replacement)
    def restore(self,p):
        if p.read(self.address,len(self.replacement))!=self.replacement:raise MemoryError('指令已被其他工具更改，拒绝覆盖')
        p.write_code(self.address,self.original)
