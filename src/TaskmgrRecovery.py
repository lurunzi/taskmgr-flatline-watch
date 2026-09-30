"""Restart only the captured Windows Task Manager, after evidence is durable."""
import ctypes as c
import os
import subprocess
import time
from ctypes import wintypes as w
from pathlib import Path

from NativeCapture import u, class_name, signature, locate

def restore_cpu_page(pid):
    script=Path(__file__).resolve().parents[1]/'tools/Restore-CpuPage.ps1'
    result={}
    for attempt in range(2):
        try:
            completed=subprocess.run(['powershell.exe','-NoProfile','-STA','-ExecutionPolicy','Bypass',
                                      '-File',str(script),'-TargetPid',str(pid)],
                                     capture_output=True,timeout=25,creationflags=subprocess.CREATE_NO_WINDOW)
            result.update(navigation_exit_code=completed.returncode,navigation_attempt=attempt+1,
                navigation_output=(completed.stdout+completed.stderr).decode(errors='replace')[-4000:])
        except subprocess.TimeoutExpired:
            result.update(navigation_exit_code=None,navigation_attempt=attempt+1,navigation_output='UI Automation timed out')
        for _ in range(20):
            time.sleep(.25)
            found=locate()
            if found and found[1]==pid:
                result.update(status='restarted',new_pid=pid,logical_graphs=len(found[3]))
                return result
    result.update(status='restarted_waiting_for_cpu_page')
    return result

def restart_taskmgr(pid, hwnd):
    k=c.WinDLL('kernel32',use_last_error=True)
    signature(k,'OpenProcess',[w.DWORD,w.BOOL,w.DWORD],w.HANDLE)
    signature(k,'CloseHandle',[w.HANDLE],w.BOOL)
    signature(k,'QueryFullProcessImageNameW',[w.HANDLE,w.DWORD,w.LPWSTR,c.POINTER(w.DWORD)],w.BOOL)
    signature(k,'WaitForSingleObject',[w.HANDLE,w.DWORD],w.DWORD)
    signature(k,'TerminateProcess',[w.HANDLE,w.UINT],w.BOOL)
    signature(u,'SendMessageTimeoutW',[w.HWND,w.UINT,w.WPARAM,w.LPARAM,w.UINT,w.UINT,c.POINTER(c.c_size_t)],w.LPARAM)
    handle=k.OpenProcess(0x100000|0x1000|1,False,pid)
    if not handle:
        raise OSError('Cannot open captured Task Manager for restart')
    expected=Path(os.environ['SystemRoot'])/'System32/Taskmgr.exe'
    result={'old_pid':pid,'status':'starting'}
    try:
        current=w.DWORD();u.GetWindowThreadProcessId(hwnd,c.byref(current))
        name=c.create_unicode_buffer(32768);size=w.DWORD(len(name))
        if current.value!=pid or class_name(hwnd)!='TaskManagerWindow':
            raise RuntimeError('Captured window identity changed; restart skipped')
        if not k.QueryFullProcessImageNameW(handle,0,name,c.byref(size)) or Path(name.value).resolve()!=expected.resolve():
            raise RuntimeError('Executable identity mismatch; restart skipped')
        message_result=c.c_size_t()
        u.SendMessageTimeoutW(hwnd,0x10,0,0,2,2000,c.byref(message_result))
        result['forced']=False
        if k.WaitForSingleObject(handle,8000)!=0:
            if not k.TerminateProcess(handle,0) or k.WaitForSingleObject(handle,5000)!=0:
                raise OSError('Captured Task Manager did not exit')
            result['forced']=True
    finally:
        k.CloseHandle(handle)
    startup=subprocess.STARTUPINFO()
    startup.dwFlags=subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow=4  # Show without activating.
    child=subprocess.Popen([str(expected)],startupinfo=startup)
    result['launch_pid']=child.pid
    for _ in range(40):
        time.sleep(.25)
        found=locate()
        if found and found[1]!=pid:
            result.update(status='restarted',new_pid=found[1],logical_graphs=len(found[3]))
            return result
    # Navigation is bounded to the newly launched Task Manager.
    result.update(restore_cpu_page(child.pid))
    return result
