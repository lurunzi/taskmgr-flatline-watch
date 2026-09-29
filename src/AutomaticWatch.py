"""Automatic chart sampling and local evidence capture for Task Manager."""
import json
import ctypes
import multiprocessing as mp
import os
import shutil
import subprocess
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

import cv2
from FlatlineDetection import FlatlineDetector
from NativeCapture import worker

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = Path('D:/TaskmgrFreezeCaptures')
PROCDUMP = ROOT / '.local/procdump/procdump64.exe'

def timestamp():
    return datetime.now().astimezone().isoformat()

def write_json(path, value):
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding='utf-8')
    temp.replace(path)

def write_dump(pid, destination):
    """Dump a PSS clone without terminating the target."""
    cmd = [str(PROCDUMP), '-accepteula', '-r', '-ma', str(pid), str(destination)]
    log = destination.with_suffix('.log')
    result = {'started':timestamp(), 'pid':pid, 'file':str(destination)}
    handle = None
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [ctypes.c_uint32,ctypes.c_bool,ctypes.c_uint32]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel.QueryFullProcessImageNameW.argtypes = [ctypes.c_void_p,ctypes.c_uint32,ctypes.c_wchar_p,ctypes.POINTER(ctypes.c_uint32)]
    try:
        handle = kernel.OpenProcess(0x1000,False,pid)
        if not handle:
            raise OSError('Cannot verify target process identity')
        image = ctypes.create_unicode_buffer(32768)
        size = ctypes.c_uint32(len(image))
        if not kernel.QueryFullProcessImageNameW(handle,0,image,ctypes.byref(size)):
            raise OSError('Cannot query target executable')
        expected = Path(os.environ['SystemRoot'])/'System32/Taskmgr.exe'
        if Path(image.value).resolve() != expected.resolve():
            raise RuntimeError('Target is not Windows Task Manager')
        with log.open('w', encoding='utf-8') as stream:
            completed = subprocess.run(cmd, stdout=stream, stderr=subprocess.STDOUT,
                timeout=180, creationflags=subprocess.CREATE_NO_WINDOW, check=False)
        result['exit_code'] = completed.returncode
        result['bytes'] = destination.stat().st_size if destination.exists() else 0
        with destination.open('rb') as stream:
            signature = stream.read(4)
        result['success'] = completed.returncode == 0 and result['bytes'] > 32 and signature == b'MDMP'
    except Exception as exc:
        result.update(success=False, error=str(exc))
    finally:
        if handle:
            kernel.CloseHandle(handle)
    result['finished'] = timestamp()
    return result

class Monitor:
    def __init__(self):
        OUTPUT.mkdir(parents=True, exist_ok=True)
        self.detector = FlatlineDetector(30)
        self.frames = deque(maxlen=31)
        self.process = self.pipe = None
        self.pending = None
        self.identity = None
        self.state = {'state':'starting'}
        self.last_status_write = 0
        self.last_request = 0
        self.dump_thread = None
        self.last_capture = 0
        prior = list(OUTPUT.glob('*/event.json'))
        if prior:
            age = max(0,time.time()-max(p.stat().st_mtime for p in prior))
            if age < 600:
                self.last_capture = time.monotonic()-age
        self.enabled = True
        self.last_event = None
        self.capture_result = None

    def reset_worker(self):
        if self.pipe:
            self.pipe.close()
        if self.process:
            if self.process.is_alive():
                self.process.terminate()  # Only our capture helper, never Taskmgr.
            self.process.join(timeout=1)
            self.process.close()
        self.pipe = self.process = None
        self.pending = None

    def publish(self, state, **extra):
        self.state = {'state':state, 'timestamp':timestamp(), 'watcher_pid':os.getpid(),
                      'output':str(OUTPUT), 'last_event':self.last_event,
                      'dump_running':bool(self.dump_thread and self.dump_thread.is_alive()),
                      'capture_result':self.capture_result, **extra}
        if time.monotonic()-self.last_status_write > 5:
            write_json(OUTPUT/'status.json', self.state)
            self.last_status_write = time.monotonic()

    def tick(self):
        now = time.monotonic()
        if not self.enabled:
            self.publish('stopped')
            return
        try:
            if self.process is None:
                self.pipe, child = mp.Pipe()
                self.process = mp.Process(target=worker, args=(child,), daemon=True)
                self.process.start()
                child.close()
            if self.pending is not None:
                if self.pipe.poll():
                    sample = self.pipe.recv()
                    self.pending = None
                    self.accept(sample)
                elif now-self.pending > 8:
                    self.reset_worker()
                    self.detector.reset()
                    self.frames.clear()
                    self.publish('capture_timeout')
                return
            if now-self.last_request >= 2:
                self.pipe.send('sample')
                self.pending = self.last_request = now
        except Exception as exc:
            self.reset_worker()
            self.detector.reset()
            self.frames.clear()
            self.publish('error', error=str(exc))

    def accept(self, sample):
        if sample['state'] != 'captured':
            self.detector.reset()
            self.frames.clear()
            self.publish(sample['state'], error=sample.get('error'))
            return
        identity = (sample['pid'], sample['hwnd'], sample['total'].shape, sample['cores'].shape)
        if identity != self.identity:
            self.detector.reset()
            self.frames.clear()
            self.identity = identity
        result = self.detector.observe(time.monotonic(), sample['total'], sample['cores'])
        frame = {'timestamp':timestamp(), 'result':result, 'images':{}}
        for key in ('total','cores'):
            ok, encoded = cv2.imencode('.png',sample[key])
            if not ok:
                raise RuntimeError('PNG encoding failed')
            frame['images'][key] = encoded.tobytes()
        self.frames.append(frame)
        self.publish(result['state'], taskmgr_pid=sample['pid'], seconds=result['seconds'],
                     logical_graphs=len(sample['geometry'])-1)
        if result['state'] == 'suspect' and (result['alert'] or self.capture_result in ('dump_failed',None)):
            self.trigger(sample)

    def trigger(self, sample):
        if self.dump_thread and self.dump_thread.is_alive():
            return
        if self.last_capture and time.monotonic()-self.last_capture < 600:
            self.publish('cooldown')
            return
        used = sum(p.stat().st_size for p in OUTPUT.glob('*/*.dmp'))
        if shutil.disk_usage(OUTPUT).free < 10*1024**3 or used > 50*1024**3:
            self.publish('storage_limit')
            return
        folder = OUTPUT/datetime.now().strftime('%Y%m%d-%H%M%S-%f')
        folder.mkdir()
        frames = list(self.frames)
        evidence = {'classification':'suspected_visual_aggregate_freeze', 'timestamp':timestamp(),
            'taskmgr_pid':sample['pid'], 'window':sample['hwnd'], 'geometry':sample['geometry'],
            'confirm_seconds':30, 'samples':[], 'dumps':[], 'status':'capturing'}
        for i, frame in enumerate(frames):
            evidence['samples'].append({'timestamp':frame['timestamp'], **frame['result']})
            if i in {0,len(frames)//2,len(frames)-1}:
                for key,data in frame['images'].items():
                    (folder/f'{i:02d}-{key}.png').write_bytes(data)
        write_json(folder/'event.json',evidence)
        self.last_capture = time.monotonic()
        self.last_event = str(folder)
        self.capture_result = 'capturing'
        self.dump_thread = threading.Thread(target=self.capture_dumps,args=(folder,evidence),daemon=False)
        self.dump_thread.start()

    def capture_dumps(self, folder, evidence):
        try:
            for index in range(2):
                if index:
                    time.sleep(5)
                from NativeCapture import u, class_name
                from ctypes import wintypes
                current_pid = wintypes.DWORD()
                u.GetWindowThreadProcessId(evidence['window'],ctypes.byref(current_pid))
                if current_pid.value != evidence['taskmgr_pid'] or class_name(evidence['window']) != 'TaskManagerWindow':
                    raise RuntimeError('Original Task Manager window exited or changed')
                evidence['dumps'].append(write_dump(evidence['taskmgr_pid'],folder/f'taskmgr-{index+1}.dmp'))
                write_json(folder/'event.json',evidence)
            evidence['status'] = 'complete' if all(d['success'] for d in evidence['dumps']) else 'dump_failed'
        except Exception as exc:
            evidence.update(status='dump_failed',error=str(exc))
        finally:
            evidence['finished'] = timestamp()
            write_json(folder/'event.json',evidence)
            self.capture_result = evidence['status']

    def stop(self):
        self.enabled = False
        self.reset_worker()
        self.detector.reset()
        self.frames.clear()
        self.last_status_write = 0
        self.publish('stopped')
