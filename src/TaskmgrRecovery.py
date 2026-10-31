"""Restart only the captured Windows Task Manager, after evidence is durable."""
import ctypes as c
import os
import subprocess
import sys
import time
from ctypes import wintypes as w
from pathlib import Path

from NativeCapture import u, class_name, signature, locate, enumerate_windows

def is_test_environment():
    return os.environ.get('TASKMGR_WATCH_TEST') == '1' or 'unittest' in sys.modules

def main_window(pid):
    for hwnd in enumerate_windows():
        owner=w.DWORD();u.GetWindowThreadProcessId(hwnd,c.byref(owner))
        if owner.value==pid and class_name(hwnd)=='TaskManagerWindow':
            return hwnd
    return None

def save_navigation_failure_window(pid, folder):
    """Save bounded screenshot of Taskmgr window only upon navigation failure."""
    if not folder:
        return
    try:
        dest = Path(folder)/'navigation-window.png'
        if dest.exists():
            return
        hwnd = main_window(pid)
        if hwnd:
            from NativeCapture import capture
            import cv2
            img = capture(hwnd)
            cv2.imwrite(str(dest), img)
    except Exception as exc:
        if "TEST ISOLATION VIOLATION" in str(exc):
            raise

def launch_taskmgr(folder=None):
    """Launch Task Manager without activating it, wait for UI, and navigate to CPU page."""
    if is_test_environment():
        raise RuntimeError("TEST ISOLATION VIOLATION: Test environment attempted to launch real Taskmgr")
    expected = Path(os.environ['SystemRoot']) / 'System32/Taskmgr.exe'
    startup = subprocess.STARTUPINFO()
    startup.dwFlags = subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = 4  # Show without activating.
    child = subprocess.Popen([str(expected)], startupinfo=startup)
    result = {'launch_pid': child.pid, 'status': 'starting'}
    started = time.monotonic()
    shown = None
    while time.monotonic() - started < 10:
        time.sleep(.25)
        found = locate()
        if found and found[1] == child.pid:
            result.update(status='restarted', new_pid=child.pid, logical_graphs=len(found[3]),
                          launch_seconds=round(time.monotonic() - started, 1))
            return result
        if shown is None and main_window(child.pid):
            shown = time.monotonic()
        if shown is not None and time.monotonic() - shown >= 2:
            break
    result['launch_seconds'] = round(time.monotonic() - started, 1)
    result.update(restore_cpu_page(child.pid, folder=folder))
    return result

def restore_cpu_page(pid, folder=None):
    if is_test_environment():
        raise RuntimeError("TEST ISOLATION VIOLATION: Test environment attempted to navigate real Taskmgr")
    script=Path(__file__).resolve().parents[1]/'tools/Restore-CpuPage.ps1'
    result={'navigation':[]}
    for attempt in range(2):
        started=time.monotonic()
        try:
            cmd = ['powershell.exe','-NoProfile','-STA','-ExecutionPolicy','Bypass',
                   '-File',str(script),'-TargetPid',str(pid)]
            if folder:
                cmd.extend(['-DiagnoseFolder',str(folder)])
            completed=subprocess.run(cmd,
                                     capture_output=True,timeout=30,creationflags=subprocess.CREATE_NO_WINDOW)
            code,output=completed.returncode,(completed.stdout+completed.stderr).decode(errors='replace')[-4000:]
        except subprocess.TimeoutExpired:
            code,output=None,'UI Automation timed out'
        # Keep every attempt; the step timings show where recovery time is spent.
        result['navigation'].append({'attempt':attempt+1,'exit_code':code,'seconds':round(time.monotonic()-started,1),'output':output})
        result.update(navigation_exit_code=code,navigation_attempt=attempt+1,navigation_output=output)
        for _ in range(20):
            time.sleep(.25)
            found=locate()
            if found and found[1]==pid:
                result.update(status='restarted',new_pid=pid,logical_graphs=len(found[3]))
                return result
    if folder:
        save_navigation_failure_window(pid, folder)
    result.update(status='restarted_waiting_for_cpu_page')
    return result

def restart_taskmgr(pid, hwnd, folder=None):
    if is_test_environment():
        raise RuntimeError("TEST ISOLATION VIOLATION: Test environment attempted to restart real Taskmgr")
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
    started=time.monotonic();shown=None
    # Wait for the CPU grid, but navigate as soon as the new window has been
    # shown for two seconds on another page instead of idling for ten.
    while time.monotonic()-started < 10:
        time.sleep(.25)
        found=locate()
        if found and found[1]!=pid:
            result.update(status='restarted',new_pid=found[1],logical_graphs=len(found[3]),
                          launch_seconds=round(time.monotonic()-started,1))
            return result
        if shown is None and main_window(child.pid):
            shown=time.monotonic()
        if shown is not None and time.monotonic()-shown >= 2:
            break
    result['launch_seconds']=round(time.monotonic()-started,1)
    # Navigation is bounded to the newly launched Task Manager.
    result.update(restore_cpu_page(child.pid, folder=folder))
    return result
