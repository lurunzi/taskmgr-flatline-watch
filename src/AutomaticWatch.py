"""Automatic chart sampling and local evidence capture for Task Manager."""
import json
import ctypes
import multiprocessing as mp
import os
import shutil
import subprocess
import struct
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

import cv2
from FlatlineDetection import FlatlineDetector
from NativeCapture import worker
from TaskmgrRecovery import restart_taskmgr, restore_cpu_page

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = Path('D:/TaskmgrFreezeCaptures')
PROCDUMP = ROOT / '.local/procdump/procdump64.exe'
VERIFY_REQUEST = ROOT / '.local/verify.request'

def timestamp():
    return datetime.now().astimezone().isoformat()

def write_json(path, value):
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding='utf-8')
    temp.replace(path)

def validate_full_dump(path):
    """Validate directory and full-memory ranges, including truncation checks."""
    length = path.stat().st_size
    with path.open('rb') as stream:
        magic, version, count, directory, _, _, flags = struct.unpack('<4sIIIIIQ',stream.read(32))
        if magic != b'MDMP' or not flags & 2 or not 1 <= count <= 1024:
            raise ValueError('Invalid full-memory minidump header')
        if directory < 32 or directory+count*12 > length:
            raise ValueError('Invalid stream directory')
        stream.seek(directory)
        entries = [struct.unpack('<III',stream.read(12)) for _ in range(count)]
        streams = {kind:(size,offset) for kind,size,offset in entries if kind}
        if not {3,4,7,9}.issubset(streams):
            raise ValueError('Missing thread, module, system or memory stream')
        if any(offset+size > length for size,offset in streams.values()):
            raise ValueError('Truncated metadata stream')
        _, offset = streams[9]
        stream.seek(offset)
        ranges, base = struct.unpack('<QQ',stream.read(16))
        if not 1 <= ranges <= 1_000_000 or offset+16+ranges*16 > length:
            raise ValueError('Invalid full-memory ranges')
        total = 0
        for _ in range(ranges):
            address, size = struct.unpack('<QQ',stream.read(16))
            total += size
        if base < 32 or base+total > length:
            raise ValueError('Truncated full-memory payload')
    return {'bytes':length,'memory_ranges':ranges,'memory_bytes':total,'flags':hex(flags)}

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
        # ProcDump 12.01 was observed returning 1 after a completed one-dump run.
        # Its redirected output mixes an ASCII banner with UTF-16LE messages.
        transcript = log.read_bytes().replace(b'\x00',b'').decode('utf-8',errors='replace')
        log.with_suffix('.txt').write_text(transcript,encoding='utf-8')
        result['validation'] = validate_full_dump(destination)
        result['completion_logged'] = 'Dump 1 complete:' in transcript
        result['success'] = completed.returncode in (0,1) and result['completion_logged']
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
        prior = [p for p in OUTPUT.glob('*/event.json') if not p.parent.name.startswith('verification-')]
        if prior:
            age = max(0,time.time()-max(p.stat().st_mtime for p in prior))
            if age < 600:
                self.last_capture = time.monotonic()-age
        self.enabled = True
        self.last_event = None
        self.capture_result = None
        self.rearm_requested = False
        self.recovery_result = None
        self.pending_recovery = None
        self.pending_navigation = None
        if prior:
            latest=max(prior,key=lambda p:p.stat().st_mtime)
            saved=json.loads(latest.read_text(encoding='utf-8'))
            if saved.get('status')=='complete' and not saved.get('recovery'):
                self.pending_recovery=(latest.parent,saved)
        history=list(OUTPUT.glob('*/event.json'))
        if history:
            latest=max(history,key=lambda p:p.stat().st_mtime)
            saved=json.loads(latest.read_text(encoding='utf-8'))
            if saved.get('recovery',{}).get('status')=='restarted_waiting_for_cpu_page':
                self.pending_navigation=(latest.parent,saved)

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
                      'capture_result':self.capture_result, 'recovery':self.recovery_result, **extra}
        if time.monotonic()-self.last_status_write > 5:
            write_json(OUTPUT/'status.json', self.state)
            self.last_status_write = time.monotonic()

    def tick(self):
        now = time.monotonic()
        if not self.enabled:
            self.publish('stopped')
            return
        try:
            if self.pending_navigation:
                folder,saved=self.pending_navigation
                self.pending_navigation=None
                self.last_event=str(folder)
                self.dump_thread=threading.Thread(target=self.resume_navigation,args=(folder,saved),daemon=False)
                self.dump_thread.start()
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
        if self.rearm_requested:
            self.detector.reset()
            self.frames.clear()
            self.rearm_requested = False
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
        if self.pending_recovery:
            folder, saved = self.pending_recovery
            self.pending_recovery = None
            if sample['pid']==saved['taskmgr_pid'] and sample['hwnd']==saved['window']:
                self.last_event=str(folder)
                self.capture_result='complete'
                self.dump_thread=threading.Thread(target=self.recover,args=(folder,saved),daemon=False)
                self.dump_thread.start()
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
        if VERIFY_REQUEST.exists() and not (self.dump_thread and self.dump_thread.is_alive()):
            recovery_test=VERIFY_REQUEST.read_text(encoding='ascii').strip()=='recovery_check'
            VERIFY_REQUEST.unlink()
            self.trigger(sample, verification=True, recovery_test=recovery_test)
            return
        if result['state'] == 'suspect' and (result['alert'] or self.capture_result in ('dump_failed','recovered',None)):
            self.trigger(sample)

    def trigger(self, sample, verification=False, recovery_test=False):
        if self.dump_thread and self.dump_thread.is_alive():
            return
        if not verification and self.last_capture and time.monotonic()-self.last_capture < 600:
            self.publish('cooldown')
            return
        used = sum(p.stat().st_size for p in OUTPUT.glob('*/*.dmp'))
        if shutil.disk_usage(OUTPUT).free < 10*1024**3 or used > 50*1024**3:
            self.publish('storage_limit')
            return
        prefix = 'verification-' if verification else ''
        folder = OUTPUT/(prefix+datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
        folder.mkdir()
        frames = list(self.frames)
        evidence = {'classification':'installation_verification' if verification else 'suspected_visual_aggregate_freeze', 'timestamp':timestamp(),
            'taskmgr_pid':sample['pid'], 'window':sample['hwnd'], 'geometry':sample['geometry'],
            'confirm_seconds':30, 'samples':[], 'dumps':[], 'status':'capturing'}
        if verification and recovery_test:
            evidence['classification']='recovery_verification'
        for i, frame in enumerate(frames):
            evidence['samples'].append({'timestamp':frame['timestamp'], **frame['result']})
            if i in {0,len(frames)//2,len(frames)-1}:
                for key,data in frame['images'].items():
                    (folder/f'{i:02d}-{key}.png').write_bytes(data)
        write_json(folder/'event.json',evidence)
        if not verification:
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
            if evidence['classification'] == 'installation_verification':
                self.capture_result = 'verification_complete' if evidence['status'] == 'complete' else 'dump_failed'
                self.rearm_requested = True
            elif evidence['status']=='complete':
                self.recover(folder,evidence)

    def recover(self, folder, evidence):
        # Verify durable files again; test captures and incomplete pairs never restart.
        if evidence.get('classification') not in ('suspected_visual_aggregate_freeze','recovery_verification') or evidence.get('status')!='complete':
            return
        try:
            if len(evidence.get('dumps',[]))!=2 or not all(d.get('success') for d in evidence['dumps']):
                raise RuntimeError('Two successful dumps are required before restart')
            for index in (1,2):
                validate_full_dump(folder/f'taskmgr-{index}.dmp')
            evidence['recovery']={'status':'restarting','started':timestamp()}
            write_json(folder/'event.json',evidence)
            self.recovery_result=evidence['recovery']
            result=restart_taskmgr(evidence['taskmgr_pid'],evidence['window'])
            evidence['recovery'].update(result,finished=timestamp())
            self.capture_result='recovered' if result['status']=='restarted' else 'recovery_failed'
        except Exception as exc:
            evidence['recovery']={'status':'failed','error':str(exc),'finished':timestamp()}
            self.capture_result='recovery_failed'
        finally:
            write_json(folder/'event.json',evidence)
            self.recovery_result=evidence.get('recovery')
            self.rearm_requested=True

    def resume_navigation(self,folder,evidence):
        try:
            result=restore_cpu_page(evidence['recovery']['launch_pid'])
            evidence['recovery'].update(result,finished=timestamp())
            self.capture_result='recovered' if result['status']=='restarted' else 'recovery_failed'
        except Exception as exc:
            evidence['recovery'].update(error=str(exc),finished=timestamp())
            self.capture_result='recovery_failed'
        finally:
            write_json(folder/'event.json',evidence)
            self.recovery_result=evidence['recovery']
            self.rearm_requested=True

    def stop(self):
        self.enabled = False
        self.reset_worker()
        self.detector.reset()
        self.frames.clear()
        self.last_status_write = 0
        self.publish('stopped')
