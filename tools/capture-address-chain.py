"""Bounded local capture. Run with the project's Python as administrator.

Stops the existing watcher through Stop.ps1, reuses its native chart helper and
detector, and restarts the watcher only after cdb has exited and is detached.
Does not change saved preferences, autostart, Windows DLLs, or system settings.
"""
import argparse
import ctypes
import hashlib
import json
import multiprocessing as mp
import subprocess
import sys
import threading
import time
import traceback
from collections import deque
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src'))
import AddressChain
import ProcessTrace
from AutomaticWatch import require_taskmgr, storage_available, validate_full_dump, write_dump, write_json
from FlatlineDetection import FlatlineDetector
from NativeCapture import worker
from OriginCapture import find_cdb
from QueryTrace import timestamp


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pid', type=int, required=True)
    parser.add_argument('--minutes', type=float, default=20)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--process-events', action='store_true',help='Record bounded ETW process/thread events for candidate correlation')
    args = parser.parse_args()
    if not 0 < args.minutes <= 60:
        parser.error('minutes must be in (0,60]')
    if not ctypes.windll.shell32.IsUserAnAdmin():
        raise RuntimeError('Administrator token required to trace elevated Taskmgr')
    args.output.mkdir(parents=True, exist_ok=False)
    state = {'started':timestamp(),'pid':args.pid,'minutes':args.minutes,'status':'preflight',
             'no_generated_load':True,'callback_changes_timing':True}
    def save(**values):
        state.update(values)
        write_json(args.output/'capture.json',state)
    save()
    sources = args.output/'collector-source'
    sources.mkdir()
    source_manifest = {}
    for path in (ROOT/'src/AddressChain.py',ROOT/'src/QueryTrace.py',ROOT/'src/ProcessTrace.py',Path(__file__)):
        data = path.read_bytes()
        (sources/path.name).write_bytes(data)
        source_manifest[path.name] = hashlib.sha256(data).hexdigest()
    write_json(args.output/'collector-source.json',source_manifest)
    session = child = pipe = None
    process_trace = None
    stopped_watcher = False
    restart_args = []
    try:
        require_taskmgr(args.pid)
        if not storage_available():
            raise RuntimeError('existing watcher storage budget refuses new dumps')
        status_path = Path('D:/TaskmgrFreezeCaptures/status.json')
        previous = json.loads(status_path.read_text(encoding='utf-8'))
        write_json(args.output/'watcher-before.json', previous)
        if previous.get('dump_running'):
            raise RuntimeError('watcher is capturing; no stop requested')
        if previous.get('origin_mode')=='query_trace':
            restart_args = ['--query-trace']
        watcher = AddressChain.Memory(previous['watcher_pid'])
        try:
            if previous.get('state')!='stopped':
                subprocess.run(['powershell','-NoProfile','-File',str(ROOT/'Stop.ps1')],check=True,
                               creationflags=subprocess.CREATE_NO_WINDOW)
                # Wait for its actual process exit, not just an old status file.
                watcher.k.GetExitCodeProcess.argtypes = [ctypes.c_void_p,ctypes.POINTER(ctypes.c_uint32)]
                deadline = time.monotonic()+40
                while True:
                    code = ctypes.c_uint32()
                    if not watcher.k.GetExitCodeProcess(watcher.handle,ctypes.byref(code)):
                        raise ctypes.WinError(ctypes.get_last_error())
                    if code.value != 259:
                        stopped_watcher = True
                        break
                    if time.monotonic() > deadline:
                        raise RuntimeError('watcher did not exit; no debugger attached')
                    time.sleep(.2)
        finally:
            watcher.close()
        require_taskmgr(args.pid)
        if args.process_events:
            process_trace = ProcessTrace.Session(args.output)
            process_trace.start()
            save(process_events=process_trace.name)
        ended = threading.Event()
        session = AddressChain.Session(find_cdb(),args.pid,args.output,write_json,lambda s:ended.set())
        session.start()
        save(status='tracing',instance=session.instance,watcher_stopped=stopped_watcher)
        pipe,remote = mp.Pipe()
        child = mp.Process(target=worker,args=(remote,),daemon=True)
        child.start()
        remote.close()
        detector = FlatlineDetector()
        frames = deque(maxlen=31)
        deadline = time.monotonic()+args.minutes*60
        after_detach = None
        while time.monotonic() < deadline:
            if (args.output/'stop.request').exists():
                save(stop_requested=True)
                break
            if session.process.poll() is not None:
                if not session.conflict.is_set():
                    save(status='trace_ended_without_conflict')
                    break
                if after_detach is None:
                    after_detach = time.monotonic()
                    detector.reset()
                    frames.clear()
                    deadline = min(deadline,after_detach+100)
                    save(status='observing_after_detach')
                if session.memory.debugger():
                    raise RuntimeError('debugger still attached after cdb exit')
            pipe.send('sample')
            if not pipe.poll(8):
                save(sample_error='native chart helper timed out')
                break
            sample = pipe.recv()
            result = {'state':sample.get('state')}
            if sample.get('state')=='captured' and sample['pid']==args.pid:
                result = detector.observe(time.monotonic(),sample['total'],sample['cores'])
                frames.append((timestamp(),result,sample))
            else:
                detector.reset()
            with (args.output/'visual-samples.jsonl').open('a',encoding='utf-8') as f:
                f.write(json.dumps({'time':timestamp(),'instance':session.instance,
                                    'pid':sample.get('pid'),'debugger_exited':after_detach is not None,
                                    **result})+'\n')
            save(last_sample=result,query_counts=dict(session.record['query_counts']))
            if after_detach is not None and result.get('state')=='suspect':
                import cv2
                selected = [frames[0],frames[len(frames)//2],frames[-1]]
                metadata = []
                for n,(stamp,measurement,frame) in enumerate(selected):
                    for key in ('total','cores'):
                        if not cv2.imwrite(str(args.output/f'freeze-{n}-{key}.png'),frame[key]):
                            raise RuntimeError('PNG save failed')
                    metadata.append({'time':stamp,'measurement':measurement,'hwnd':frame['hwnd'],
                                     'pid':frame['pid'],'logical_graphs':len(frame['geometry'])-1})
                write_json(args.output/'freeze.json',{'instance':session.instance,'frames':metadata,
                                                     'after_debugger_detach':True})
                save(status='freeze_observed',freeze=True)
                if session.memory.debugger():
                    raise RuntimeError('refusing second dump while debugger attached')
                result = write_dump(args.pid,args.output/'Taskmgr-after-freeze.dmp')
                write_json(args.output/'after-freeze-dump.json',result)
                break
            time.sleep(2)
    except Exception as exc:
        save(status='incomplete',error=repr(exc),traceback=traceback.format_exc())
    finally:
        try:
            if child:
                if child.is_alive():
                    child.terminate()  # Only our chart worker, never cdb or Taskmgr.
                child.join(5)
            if pipe:
                pipe.close()
            if session and session.process:
                if session.process.poll() is None:
                    session.record['cancelled'] = not session.conflict.is_set()
                    session.detach()
                # Never restart automatic recovery while a debugger remains attached.
                while session.process.poll() is None or session.memory.debugger():
                    save(status='detach_pending',recovery_resumed=False)
                    time.sleep(2)
                for dump in args.output.glob('*.dmp'):
                    try:
                        save(**{dump.stem+'_validation':validate_full_dump(dump)})
                    except Exception as exc:
                        save(**{dump.stem+'_validation_error':repr(exc)})
                session.memory.close()
            if stopped_watcher:
                subprocess.Popen([str(ROOT/'.venv/Scripts/pythonw.exe'),str(ROOT/'src/FlatlineWatch.py'),*restart_args],
                                 cwd=ROOT,creationflags=subprocess.CREATE_NO_WINDOW)
                save(recovery_resumed=True)
        except Exception as exc:
            save(status='incomplete',cleanup_error=repr(exc))
        finally:
            # ETW ownership survives a chart/debugger/recovery cleanup error.
            if process_trace:
                try:
                    process_trace.stop()
                    process_trace.decode()
                except Exception as exc:
                    save(process_trace_error=repr(exc),process_trace_active=process_trace.active)
        if state['status'] in ('preflight','tracing','observing_after_detach'):
            save(status='incomplete',reason='stop requested' if state.get('stop_requested') else 'bounded observation ended')
        save(finished=timestamp())


if __name__=='__main__':
    mp.freeze_support()
    main()
