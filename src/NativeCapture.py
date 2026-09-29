"""Capture Task Manager's native chart controls, without desktop screenshots."""
import ctypes as c
import os
from ctypes import wintypes as w
from collections import Counter

import numpy as np

u = c.WinDLL('user32', use_last_error=True)
g = c.WinDLL('gdi32', use_last_error=True)
CALLBACK = c.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)

def signature(lib, name, args, result):
    fn = getattr(lib, name)
    fn.argtypes, fn.restype = args, result
    return fn

signature(u, 'EnumWindows', [CALLBACK, w.LPARAM], w.BOOL)
signature(u, 'EnumChildWindows', [w.HWND, CALLBACK, w.LPARAM], w.BOOL)
signature(u, 'GetClassNameW', [w.HWND, w.LPWSTR, c.c_int], c.c_int)
signature(u, 'GetWindowRect', [w.HWND, c.POINTER(w.RECT)], w.BOOL)
signature(u, 'IsWindowVisible', [w.HWND], w.BOOL)
signature(u, 'IsIconic', [w.HWND], w.BOOL)
signature(u, 'GetWindowThreadProcessId', [w.HWND, c.POINTER(w.DWORD)], w.DWORD)
signature(u, 'GetDC', [w.HWND], w.HDC)
signature(u, 'ReleaseDC', [w.HWND, w.HDC], c.c_int)
signature(u, 'PrintWindow', [w.HWND, w.HDC, w.UINT], w.BOOL)
signature(g, 'CreateCompatibleDC', [w.HDC], w.HDC)
signature(g, 'CreateCompatibleBitmap', [w.HDC, c.c_int, c.c_int], w.HBITMAP)
signature(g, 'SelectObject', [w.HDC, w.HANDLE], w.HANDLE)
signature(g, 'DeleteObject', [w.HANDLE], w.BOOL)
signature(g, 'DeleteDC', [w.HDC], w.BOOL)

class Header(c.Structure):
    _fields_ = [('size', w.DWORD), ('width', w.LONG), ('height', w.LONG),
                ('planes', w.WORD), ('bits', w.WORD), ('compression', w.DWORD),
                ('image_size', w.DWORD), ('xppm', w.LONG), ('yppm', w.LONG),
                ('used', w.DWORD), ('important', w.DWORD)]

signature(g, 'GetDIBits', [w.HDC, w.HBITMAP, w.UINT, w.UINT, c.c_void_p,
                         c.POINTER(Header), w.UINT], c.c_int)

def class_name(hwnd):
    buf = c.create_unicode_buffer(256)
    u.GetClassNameW(hwnd, buf, 256)
    return buf.value

def rectangle(hwnd):
    r = w.RECT()
    if not u.GetWindowRect(hwnd, c.byref(r)):
        raise OSError('GetWindowRect failed')
    return (r.left, r.top, r.right-r.left, r.bottom-r.top)

def enumerate_windows(parent=None):
    result = []
    callback = CALLBACK(lambda h, _: result.append(h) or True)
    if parent is None:
        u.EnumWindows(callback, 0)
    else:
        u.EnumChildWindows(parent, callback, 0)
    return result

def locate():
    for hwnd in enumerate_windows():
        if class_name(hwnd) != 'TaskManagerWindow' or u.IsIconic(hwnd):
            continue
        charts = [(h, rectangle(h)) for h in enumerate_windows(hwnd)
                  if class_name(h) == 'CvChartWindow' and u.IsWindowVisible(h)]
        charts = [(h, r) for h, r in charts if r[2] >= 25 and r[3] >= 12]
        if not charts:
            continue
        left = min(r[0] for _, r in charts)
        sidebar = sorted([(h,r) for h,r in charts if abs(r[0]-left) <= 2], key=lambda v:v[1][1])
        total = sidebar[0]
        right = [(h,r) for h,r in charts if r[0] > left+total[1][2]+20]
        # Require the complete logical-processor grid, not a memory/GPU page.
        if len(right) != os.cpu_count() or len(right) < 2:
            continue
        widths = Counter(round(r[2]/4) for _,r in right)
        if widths.most_common(1)[0][1] < len(right)*.8:
            continue
        right.sort(key=lambda v:(v[1][1],v[1][0]))
        pid = w.DWORD()
        u.GetWindowThreadProcessId(hwnd, c.byref(pid))
        return hwnd, pid.value, total, right
    return None

def capture(hwnd):
    _, _, width, height = rectangle(hwnd)
    if width <= 0 or height <= 0 or width*height > 10_000_000:
        raise ValueError('Invalid chart dimensions')
    dc = u.GetDC(hwnd)
    memory = bitmap = previous = None
    try:
        memory = g.CreateCompatibleDC(dc)
        bitmap = g.CreateCompatibleBitmap(dc, width, height)
        if not dc or not memory or not bitmap:
            raise OSError('GDI allocation failed')
        previous = g.SelectObject(memory, bitmap)
        if not u.PrintWindow(hwnd, memory, 2):
            raise OSError('PrintWindow failed (permissions or renderer)')
        g.SelectObject(memory, previous)
        previous = None
        header = Header(c.sizeof(Header), width, -height, 1, 32, 0, 0, 0, 0, 0, 0)
        data = np.empty((height, width, 4), np.uint8)
        if g.GetDIBits(memory, bitmap, 0, height, data.ctypes.data, c.byref(header), 0) != height:
            raise OSError('GetDIBits failed')
        return data[:,:,:3].copy()
    finally:
        if previous:
            g.SelectObject(memory, previous)
        if bitmap:
            g.DeleteObject(bitmap)
        if memory:
            g.DeleteDC(memory)
        if dc:
            u.ReleaseDC(hwnd, dc)

def sample():
    found = locate()
    if not found:
        return {'state':'waiting'}
    hwnd, pid, total, cores = found
    images = [capture(h) for h,_ in cores]
    # Preserve each graph, with stable padding for fractional DPI sizes.
    height = max(im.shape[0] for im in images)
    width = max(im.shape[1] for im in images)
    canvas = np.full((height*4, width*((len(images)+3)//4), 3), 255, np.uint8)
    columns = (len(images)+3)//4
    for i, im in enumerate(images):
        y,x = (i//columns)*height,(i%columns)*width
        canvas[y:y+im.shape[0],x:x+im.shape[1]] = im
    return {'state':'captured','hwnd':hwnd,'pid':pid,'total':capture(total[0]),
            'cores':canvas,'geometry':[total[1]]+[r for _,r in cores]}

def worker(pipe):
    try:
        u.SetProcessDpiAwarenessContext(c.c_void_p(-4))
        while pipe.recv() == 'sample':
            try:
                pipe.send(sample())
            except Exception as exc:
                pipe.send({'state':'capture_error','error':str(exc)})
    except (EOFError, BrokenPipeError):
        pass
    finally:
        pipe.close()
