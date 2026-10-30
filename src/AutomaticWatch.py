"""Automatic chart sampling and local evidence capture for Task Manager."""
import hashlib
import json
import ctypes
from ctypes import wintypes
import multiprocessing as mp
import os
import shutil
import subprocess
import struct
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

import cv2
from FlatlineDetection import FlatlineDetector
from NativeCapture import worker, taskmgr_pids
from OriginCapture import OriginWatch
from TaskmgrRecovery import restart_taskmgr, restore_cpu_page, save_navigation_failure_window, launch_taskmgr
from ProcessTrace import qpc
from RollingProcessTrace import RollingRecorder, TRACE_BUDGET
from RealtimeTrace import RealtimeConsumer, re_enable_session_provider
from IncidentWatch import Observations, OfflineProcessor, SNAPSHOT_WAIT_SECONDS, mark_recovery
from IncidentAnalysis import record_outside_flatline

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = Path('D:/TaskmgrFreezeCaptures')
PROCDUMP = ROOT / '.local/procdump/procdump64.exe'
VERIFY_REQUEST = ROOT / '.local/verify.request'
RETRY_LIMIT = 3
RETRY_INTERVAL = 30
CAPTURE_COOLDOWN = 600
RESTART_COOLDOWN = 60
BLIND_BACKOFF_SCHEDULE = (120, 300, 600, 1200, 1800)
MAX_BLIND_RETRIES_PER_HOUR = 4
USER_CLOSED_COOLDOWN_SECONDS = 600  # 10 min wait after user closes Taskmgr to avoid focus fighting
NATIVE_GAP_THRESHOLD_SECONDS = 15.0
NATIVE_GAP_BACKOFF_SCHEDULE = (5, 10, 20, 30, 60)
HEARTBEAT_GAP_THRESHOLD_SECONDS = 10.0
HEARTBEAT_GAP_BACKOFF_SCHEDULE = (10, 30, 60, 120)

class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [('cbSize', ctypes.c_uint), ('dwTime', ctypes.c_uint)]

class PROCESSENTRY32(ctypes.Structure):
    _fields_ = [
        ('dwSize', wintypes.DWORD),
        ('cntUsage', wintypes.DWORD),
        ('th32ProcessID', wintypes.DWORD),
        ('th32DefaultHeapID', ctypes.c_size_t),
        ('th32ModuleID', wintypes.DWORD),
        ('cntThreads', wintypes.DWORD),
        ('th32ParentProcessID', wintypes.DWORD),
        ('pcPriClassBase', wintypes.LONG),
        ('dwFlags', wintypes.DWORD),
        ('szExeFile', ctypes.c_wchar * 260)
    ]

def is_desktop_ready():
    """Return True when explorer is running and user session desktop is unlocked."""
    if os.environ.get('TASKMGR_WATCH_TEST') == '1':
        return getattr(Monitor, '_mock_desktop_ready', True)
    try:
        kernel = ctypes.windll.kernel32
        h = kernel.CreateToolhelp32Snapshot(2, 0)  # TH32CS_SNAPPROCESS
        if h == -1 or not h:
            return False
        found_explorer = False
        pe = PROCESSENTRY32()
        pe.dwSize = ctypes.sizeof(PROCESSENTRY32)
        if kernel.Process32FirstW(h, ctypes.byref(pe)):
            while True:
                if pe.szExeFile.lower() == 'explorer.exe':
                    found_explorer = True
                    break
                if not kernel.Process32NextW(h, ctypes.byref(pe)):
                    break
        kernel.CloseHandle(h)
        if not found_explorer:
            return False

        user32 = ctypes.windll.user32
        hdesk = user32.OpenInputDesktop(0, False, 0x0100)  # DESKTOP_SWITCHDESKTOP
        if not hdesk:
            return False
        try:
            unlocked = bool(user32.SwitchDesktop(hdesk))
        finally:
            user32.CloseDesktop(hdesk)
        return unlocked
    except Exception:
        return False

def is_user_interacting_system(idle_threshold_ms=10000):
    """Return True if user performed keyboard/mouse input within idle_threshold_ms."""
    if os.environ.get('TASKMGR_WATCH_TEST') == '1':
        return getattr(Monitor, '_mock_user_interacting_system', False)
    try:
        u = ctypes.windll.user32
        k = ctypes.windll.kernel32
        lii = LASTINPUTINFO()
        lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
        if not u.GetLastInputInfo(ctypes.byref(lii)):
            return False
        idle_ms = (k.GetTickCount() & 0xFFFFFFFF) - lii.dwTime
        return idle_ms < idle_threshold_ms
    except Exception:
        return False

def is_user_interacting_with_window(target_pid, idle_threshold_ms=10000):
    """Return True if target_pid window is in foreground and user interacted recently."""
    try:
        u = ctypes.windll.user32
        k = ctypes.windll.kernel32
        fg = u.GetForegroundWindow()
        if not fg:
            return False
        fg_pid = ctypes.c_uint()
        u.GetWindowThreadProcessId(fg, ctypes.byref(fg_pid))
        if fg_pid.value != target_pid:
            return False
        lii = LASTINPUTINFO()
        lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
        if not u.GetLastInputInfo(ctypes.byref(lii)):
            return True
        idle_ms = (k.GetTickCount() & 0xFFFFFFFF) - lii.dwTime
        return idle_ms < idle_threshold_ms
    except Exception:
        return False

def is_taskmgr_process_alive(pid):
    try:
        require_taskmgr(pid)
        return True
    except Exception:
        return False

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

def require_taskmgr(pid):
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [ctypes.c_uint32,ctypes.c_bool,ctypes.c_uint32]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel.QueryFullProcessImageNameW.argtypes = [ctypes.c_void_p,ctypes.c_uint32,ctypes.c_wchar_p,ctypes.POINTER(ctypes.c_uint32)]
    handle = kernel.OpenProcess(0x1000,False,pid)
    if not handle:
        raise OSError('Cannot verify target process identity')
    try:
        image = ctypes.create_unicode_buffer(32768)
        size = ctypes.c_uint32(len(image))
        if not kernel.QueryFullProcessImageNameW(handle,0,image,ctypes.byref(size)):
            raise OSError('Cannot query target executable')
        expected = Path(os.environ['SystemRoot'])/'System32/Taskmgr.exe'
        if Path(image.value).resolve() != expected.resolve():
            raise RuntimeError('Target is not Windows Task Manager')
    finally:
        kernel.CloseHandle(handle)

def storage_available():
    used = sum(p.stat().st_size for p in OUTPUT.glob('*/*.dmp'))
    return shutil.disk_usage(OUTPUT).free >= 10*1024**3 and used <= 50*1024**3

def write_dump(pid, destination):
    """Dump a PSS clone without terminating the target."""
    cmd = [str(PROCDUMP), '-accepteula', '-r', '-ma', str(pid), str(destination)]
    log = destination.with_suffix('.log')
    result = {'started':timestamp(), 'pid':pid, 'file':str(destination)}
    try:
        require_taskmgr(pid)
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
    result['finished'] = timestamp()
    return result


def read_pe_file_version(path):
    try:
        kernel = ctypes.WinDLL('version', use_last_error=True)
        size = kernel.GetFileVersionInfoSizeW(str(path), None)
        if not size:
            return None
        buf = ctypes.create_string_buffer(size)
        if not kernel.GetFileVersionInfoW(str(path), 0, size, buf):
            return None
        p = ctypes.c_void_p()
        uLen = ctypes.c_uint32()
        class VS_FIXEDFILEINFO(ctypes.Structure):
            _fields_ = [
                ('dwSignature', ctypes.c_uint32),
                ('dwStrucVersion', ctypes.c_uint32),
                ('dwFileVersionMS', ctypes.c_uint32),
                ('dwFileVersionLS', ctypes.c_uint32),
            ]
        if not kernel.VerQueryValueW(buf, r'\\', ctypes.byref(p), ctypes.byref(uLen)):
            return None
        vs = ctypes.cast(p, ctypes.POINTER(VS_FIXEDFILEINFO)).contents
        return f'{vs.dwFileVersionMS >> 16}.{vs.dwFileVersionMS & 0xFFFF}.{vs.dwFileVersionLS >> 16}.{vs.dwFileVersionLS & 0xFFFF}'
    except Exception:
        return None

def inspect_target_dll(baseline_path=None, dll_path=None):
    if dll_path is None:
        dll_path = Path(os.environ.get('SystemRoot', r'C:\Windows'))/'System32/TaskManagerDataLayer.dll'
    else:
        dll_path = Path(dll_path)
    info = {
        'path': str(dll_path),
        'sha256': None,
        'file_version': None,
        'modified': None,
        'dll_changed': False,
    }
    if not dll_path.is_file():
        info['error'] = 'file not found'
        return info

    try:
        with dll_path.open('rb') as f:
            info['sha256'] = hashlib.file_digest(f, 'sha256').hexdigest()
        stat = dll_path.stat()
        info['modified'] = datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat()
    except Exception as exc:
        info['error'] = str(exc)
        return info

    info['file_version'] = read_pe_file_version(dll_path)

    if baseline_path:
        base_file = Path(baseline_path)
        prior_sha = None
        if base_file.exists():
            try:
                prior = json.loads(base_file.read_text(encoding='utf-8'))
                prior_sha = prior.get('sha256') if isinstance(prior, dict) else None
            except Exception:
                pass
        elif (base_file.parent/'status.json').exists():
            try:
                prior_status = json.loads((base_file.parent/'status.json').read_text(encoding='utf-8'))
                prior_sha = prior_status.get('target_dll', {}).get('sha256')
            except Exception:
                pass
        if prior_sha and prior_sha != info['sha256']:
            info['dll_changed'] = True
            info['previous_sha256'] = prior_sha
        try:
            write_json(base_file, info)
        except Exception:
            pass
    return info

class Monitor:
    def __init__(self, query_trace=False, restart_only=False, light_etw=False,
                 etw_budget=TRACE_BUDGET):
        # Lightweight ETW is separate from the origin debugger/dump monitor.
        self.restart_only = bool(restart_only or light_etw)
        self.light_etw = light_etw
        OUTPUT.mkdir(parents=True, exist_ok=True)
        self.target_dll = inspect_target_dll(OUTPUT/'target-dll-baseline.json')
        self.detector = FlatlineDetector(30)
        self.frames = deque(maxlen=61 if light_etw else 31)
        self.observations = Observations()
        self.recorder = RollingRecorder(OUTPUT, trace_budget=etw_budget) if light_etw else None
        self.offline = OfflineProcessor() if light_etw else None
        self.realtime = RealtimeConsumer() if light_etw else None
        if self.realtime:
            try:
                self.realtime.start()
            except Exception:
                pass
        self.process = self.pipe = None
        self.pending = None
        self.identity = None
        self.state = {'state':'starting'}
        self.last_status_write = 0
        self.last_request = 0
        self.dump_thread = None
        self.last_capture = 0
        self.last_capture_target = None
        prior = [p for p in OUTPUT.glob('*/event.json') if not p.parent.name.startswith('verification-')]
        if prior:
            age = max(0,time.time()-max(p.stat().st_mtime for p in prior))
            if age < 600:
                self.last_capture = time.monotonic()-age
                recent=max(prior,key=lambda p:p.stat().st_mtime)
                saved=json.loads(recent.read_text(encoding='utf-8'))
                self.last_capture_target=(saved.get('taskmgr_pid'),saved.get('window'))
        self.enabled = True
        self.last_event = None
        self.capture_result = None
        self.rearm_requested = False
        self.recovery_result = None
        self.pending_recovery = None
        self.pending_navigation = None
        self.retry = None
        self.retry_folder = None
        self.retry_count = 0
        self.last_retry = 0
        self.waiting_since_monotonic = None
        self.waiting_since_utc = None
        self.blind_after_recovery = False
        self.current_blind_interval = None
        self.blind_navigation_target = None
        self.blind_retry_count = 0
        self.blind_retry_history = []
        self.next_navigation_retry_monotonic = None
        self.next_navigation_retry_utc = None
        self.auto_open_count = 0
        self.auto_open_last_utc = None
        self.auto_open_last_result = None
        self.next_auto_open_monotonic = None
        self.next_auto_open_utc = None
        self.next_auto_open_text_zh = None
        self.next_auto_open_text_en = None
        self.auto_open_reason = None
        self.taskmgr_was_running = None
        self.boot_auto_open_checked = False
        self.auto_navigation_count = 0
        self.auto_navigation_last_utc = None
        self.auto_navigation_last_result = None
        self.current_provider_gap = None
        self.last_provider_re_enable_monotonic = 0.0
        self.provider_gap_retry_count = 0
        self.cumulative_provider_gap_seconds = 0.0
        self.last_known_event_count = 0
        self.current_heartbeat_gap = None
        self.last_heartbeat_re_enable_monotonic = 0.0
        self.heartbeat_gap_retry_count = 0
        self.cumulative_heartbeat_gap_seconds = 0.0
        self.last_known_heartbeat_count = 0
        self.daily_monitoring_file = OUTPUT / 'daily-monitoring.json'
        self.daily_monitoring_history = self._load_daily_monitoring()
        self.last_daily_update_mono = time.monotonic()
        self.current_monitored_pid = None
        self.target_pid_start_monotonic = None
        self.origin = OriginWatch(PROCDUMP, OUTPUT, validate_full_dump, write_json, query_trace)
        self.last_origin_scan = 0
        self.origin_error = None
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
                if saved.get('recovery',{}).get('retry_exhausted'):
                    self.blind_after_recovery = True
                    self.arm_blind_navigation(latest.parent, saved)
                else:
                    self.pending_navigation=(latest.parent,saved)
        if self.recorder:
            self.recorder.start()

    def reset_observations(self, reason='unspecified', detail=None):
        self.detector.reset()
        self.frames.clear()
        self.observations.reset(reason, detail)

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

    def background_phase(self):
        if not (self.dump_thread and self.dump_thread.is_alive()):
            return None
        return 'recovering' if self.recovery_result else 'capturing'

    def publish(self, state, **extra):
        # Live samples keep arriving while dumps or recovery run in the
        # background; the background phase must stay the visible state.
        phase = self.background_phase()
        if phase and state not in ('stopped','capturing','recovering'):
            extra['sample_state'] = state
            state = phase
        changed = state != self.state.get('state')
        sample_state = extra.get('sample_state', state)
        if sample_state == 'waiting':
            blind_seconds = round(time.monotonic() - self.waiting_since_monotonic, 1) if self.waiting_since_monotonic else 0.0
            waiting_since = self.waiting_since_utc
            blind_after = bool(self.blind_after_recovery)
            next_retry = self.next_navigation_retry_utc
        else:
            blind_seconds = 0
            waiting_since = None
            blind_after = False
            next_retry = None
        n1_tally = None
        n1_file = OUTPUT/'n1-summary.json'
        if n1_file.exists():
            try:
                n1_data = json.loads(n1_file.read_text(encoding='utf-8'))
                n1_tally = {
                    'qualifying_count': n1_data.get('qualifying_count', 0),
                    'conforming_count': n1_data.get('conforming_count', 0),
                    'non_conforming_count': n1_data.get('non_conforming_count', 0),
                    'indeterminate_count': n1_data.get('indeterminate_count', 0),
                    'outside_flatlines_count': n1_data.get('outside_flatlines_count', 0),
                    'status': n1_data.get('status', 'evaluating')
                }
            except Exception:
                pass
        if not n1_tally:
            n1_tally = {
                'qualifying_count': 1,
                'conforming_count': 1,
                'non_conforming_count': 0,
                'indeterminate_count': 1,
                'outside_flatlines_count': 0,
                'status': 'evaluating'
            }
        if self.current_provider_gap:
            gap_seconds = round(time.monotonic() - self.current_provider_gap['start_monotonic'], 1)
            native_coverage = 'gap'
            gap_started = self.current_provider_gap['start_utc']
            cumulative_gap = round(self.cumulative_provider_gap_seconds + gap_seconds, 1)
        else:
            gap_seconds = 0
            native_coverage = 'ok'
            gap_started = None
            cumulative_gap = round(self.cumulative_provider_gap_seconds, 1)

        if self.current_heartbeat_gap:
            hb_gap_seconds = round(time.monotonic() - self.current_heartbeat_gap['start_monotonic'], 1)
            heartbeat_coverage = 'gap'
            hb_gap_started = self.current_heartbeat_gap['start_utc']
            cumulative_hb_gap = round(self.cumulative_heartbeat_gap_seconds + hb_gap_seconds, 1)
        else:
            hb_gap_seconds = 0
            heartbeat_coverage = 'ok'
            hb_gap_started = None
            cumulative_hb_gap = round(self.cumulative_heartbeat_gap_seconds, 1)

        today_date = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        today_eff_hours = 0.0
        for entry in self.daily_monitoring_history:
            if entry.get('date') == today_date:
                today_eff_hours = entry.get('effective_monitoring_hours', 0.0)
                break

        auto_open_text_zh = self.next_auto_open_text_zh if not self.get_taskmgr_pids() else None
        auto_open_text_en = self.next_auto_open_text_en if not self.get_taskmgr_pids() else None
        auto_open_text = auto_open_text_zh

        self.state = {'state':state, 'timestamp':timestamp(), 'watcher_pid':os.getpid(),
                      'output':str(OUTPUT), 'last_event':self.last_event,
                      'dump_running':bool(self.dump_thread and self.dump_thread.is_alive()),
                      'capture_result':self.capture_result, 'recovery':self.recovery_result,
                      'origin_monitoring':sorted(self.origin.sessions), 'origin_error':self.origin_error,
                      'origin_mode':'query_trace' if self.origin.query_trace else 'procdump',
                      'light_etw':self.recorder.status() if self.recorder else {'state':'disabled'},
                      'incident_analysis':self.offline.status() if self.offline else {'status':'disabled'},
                      'realtime_consumer':self.realtime.status() if self.realtime else {'state':'disabled'},
                      'target_dll':self.target_dll,
                      'dll_changed':self.target_dll.get('dll_changed', False),
                      'waiting_since':waiting_since,
                      'blind_seconds':blind_seconds,
                      'blind_after_recovery':blind_after,
                      'next_navigation_retry':next_retry,
                      'n1_tally':n1_tally,
                      'native_coverage':native_coverage,
                      'native_coverage_gap_started':gap_started,
                      'native_coverage_gap_seconds':gap_seconds,
                      'native_coverage_cumulative_gap_seconds':cumulative_gap,
                      'heartbeat_coverage':heartbeat_coverage,
                      'heartbeat_gap_started':hb_gap_started,
                      'heartbeat_gap_seconds':hb_gap_seconds,
                      'heartbeat_cumulative_gap_seconds':cumulative_hb_gap,
                      'daily_monitoring_history':list(self.daily_monitoring_history),
                      'today_effective_monitoring_hours':today_eff_hours,
                      'mode':'restart_only' if self.restart_only else 'capture',
                      'auto_open': {
                          'count': self.auto_open_count,
                          'last_utc': self.auto_open_last_utc,
                          'last_result': self.auto_open_last_result,
                          'next_open_utc': self.next_auto_open_utc,
                          'status_text': auto_open_text,
                          'status_text_zh': auto_open_text_zh,
                          'status_text_en': auto_open_text_en,
                          'reason': self.auto_open_reason
                      },
                      'auto_navigation': {
                          'count': self.auto_navigation_count,
                          'last_utc': self.auto_navigation_last_utc,
                          'last_result': self.auto_navigation_last_result,
                          'next_retry_utc': self.next_navigation_retry_utc
                      },
                      **extra}
        if changed or time.monotonic()-self.last_status_write > 5:
            write_json(OUTPUT/'status.json', self.state)
            self.last_status_write = time.monotonic()

    def get_taskmgr_pids(self):
        if os.environ.get('TASKMGR_WATCH_TEST') == '1' and hasattr(self, '_mock_taskmgr_pids'):
            return set(self._mock_taskmgr_pids)
        return taskmgr_pids()

    def is_pid_alive(self, pid):
        if not pid:
            return False
        if os.environ.get('TASKMGR_WATCH_TEST') == '1':
            return getattr(self, '_mock_pid_alive', True)
        try:
            if pid in self.get_taskmgr_pids():
                return True
            kernel32 = ctypes.windll.kernel32
            h = kernel32.OpenProcess(0x1000, False, pid)
            if h:
                exit_code = wintypes.DWORD()
                kernel32.GetExitCodeProcess(h, ctypes.byref(exit_code))
                kernel32.CloseHandle(h)
                return exit_code.value == 259
            return False
        except Exception:
            return False

    def re_enable_native_providers(self):
        if self.realtime:
            try:
                self.realtime.re_enable_provider()
            except Exception:
                pass
        corr_session = None
        if self.recorder:
            try:
                rec_status = self.recorder.status()
                corr_session = rec_status.get('session')
            except Exception:
                pass
        if corr_session:
            try:
                re_enable_session_provider(corr_session, level=2)
            except Exception:
                pass

    def check_native_coverage(self, now):
        if not self.realtime:
            return
        target_pid = self.realtime.target_pid or self.state.get('taskmgr_pid')
        if not target_pid:
            if self.current_provider_gap:
                self.current_provider_gap = None
            return

        if not self.is_pid_alive(target_pid):
            if self.current_provider_gap:
                self.current_provider_gap = None
            return

        rt_status = self.realtime.status()
        cur_event_count = rt_status.get('provider_event_count', 0)
        last_mono = rt_status.get('last_provider_event_mono')
        last_utc = rt_status.get('last_provider_event_utc')
        last_qpc = rt_status.get('last_provider_event_qpc')

        if cur_event_count > self.last_known_event_count:
            self.last_known_event_count = cur_event_count
            if self.current_provider_gap:
                gap = dict(self.current_provider_gap)
                gap['end_utc'] = last_utc or timestamp()
                gap['end_qpc'] = last_qpc or qpc()
                duration = round(now - gap.pop('start_monotonic'), 1)
                gap['duration_seconds'] = duration
                self.cumulative_provider_gap_seconds += duration
                self.observations.record_native_coverage_gap(gap)
                self.current_provider_gap = None
                self.provider_gap_retry_count = 0

        ref_mono = last_mono if last_mono is not None else self.target_pid_start_monotonic
        if ref_mono is not None and (now - ref_mono >= NATIVE_GAP_THRESHOLD_SECONDS):
            if self.current_provider_gap is None:
                self.current_provider_gap = {
                    'start_utc': last_utc or timestamp(),
                    'start_qpc': last_qpc or qpc(),
                    'start_monotonic': ref_mono,
                    'gap_detected_utc': timestamp(),
                    'gap_detected_qpc': qpc(),
                    'taskmgr_pid': target_pid,
                    'reason': 'no_provider_events_for_15s'
                }
                self.provider_gap_retry_count = 0
                self.re_enable_native_providers()
                self.last_provider_re_enable_monotonic = now
            else:
                delay = NATIVE_GAP_BACKOFF_SCHEDULE[min(self.provider_gap_retry_count, len(NATIVE_GAP_BACKOFF_SCHEDULE)-1)]
                if now - self.last_provider_re_enable_monotonic >= delay:
                    self.provider_gap_retry_count += 1
                    self.re_enable_native_providers()
                    self.last_provider_re_enable_monotonic = now

    def check_heartbeat_coverage(self, now):
        if not self.realtime:
            return
        target_pid = self.realtime.target_pid or self.state.get('taskmgr_pid')
        if not target_pid or not self.is_pid_alive(target_pid):
            if self.current_heartbeat_gap:
                self.current_heartbeat_gap = None
            return

        rt_status = self.realtime.status()
        cur_hb_count = rt_status.get('heartbeat_count', 0)
        last_mono = rt_status.get('last_heartbeat_mono')
        last_utc = rt_status.get('last_heartbeat_utc')

        if cur_hb_count > self.last_known_heartbeat_count:
            self.last_known_heartbeat_count = cur_hb_count
            if self.current_heartbeat_gap:
                gap = dict(self.current_heartbeat_gap)
                gap['end_utc'] = last_utc or timestamp()
                gap['end_qpc'] = qpc()
                duration = round(now - gap.pop('start_monotonic'), 1)
                gap['duration_seconds'] = duration
                self.cumulative_heartbeat_gap_seconds += duration
                self.observations.record_heartbeat_coverage_gap(gap)
                self.current_heartbeat_gap = None
                self.heartbeat_gap_retry_count = 0

        ref_mono = last_mono if last_mono is not None else self.target_pid_start_monotonic
        if ref_mono is not None and (now - ref_mono >= HEARTBEAT_GAP_THRESHOLD_SECONDS):
            if self.current_heartbeat_gap is None:
                self.current_heartbeat_gap = {
                    'start_utc': last_utc or timestamp(),
                    'start_qpc': qpc(),
                    'start_monotonic': ref_mono,
                    'gap_detected_utc': timestamp(),
                    'gap_detected_qpc': qpc(),
                    'taskmgr_pid': target_pid,
                    'reason': 'no_heartbeat_for_10s'
                }
                self.heartbeat_gap_retry_count = 0
                try:
                    self.realtime.re_enable_provider()
                except Exception:
                    pass
                self.last_heartbeat_re_enable_monotonic = now
            else:
                delay = HEARTBEAT_GAP_BACKOFF_SCHEDULE[min(self.heartbeat_gap_retry_count, len(HEARTBEAT_GAP_BACKOFF_SCHEDULE)-1)]
                if now - self.last_heartbeat_re_enable_monotonic >= delay:
                    self.heartbeat_gap_retry_count += 1
                    try:
                        self.realtime.re_enable_provider()
                    except Exception:
                        pass
                    self.last_heartbeat_re_enable_monotonic = now

    def _load_daily_monitoring(self):
        if hasattr(self, 'daily_monitoring_file') and self.daily_monitoring_file.exists():
            try:
                data = json.loads(self.daily_monitoring_file.read_text(encoding='utf-8'))
                if isinstance(data, list):
                    return data
            except Exception:
                pass
        return []

    def _save_daily_monitoring(self):
        try:
            write_json(self.daily_monitoring_file, self.daily_monitoring_history)
        except Exception:
            pass

    def update_daily_monitoring(self, now):
        dt = now - self.last_daily_update_mono
        self.last_daily_update_mono = now
        if not (0.0 < dt <= 5.0):
            return

        today_date = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        today_entry = None
        for entry in self.daily_monitoring_history:
            if entry.get('date') == today_date:
                today_entry = entry
                break

        if today_entry is None:
            today_entry = {
                'date': today_date,
                'effective_monitoring_hours': 0.0,
                'effective_monitoring_seconds': 0.0,
                'blind_seconds': 0.0,
                'native_gap_seconds': 0.0,
                'heartbeat_gap_seconds': 0.0,
                'flatlines_confirmed': 0,
                'native_0x8007139f_count': 0
            }
            self.daily_monitoring_history.append(today_entry)

        cur_state = self.state.get('state')
        has_taskmgr = bool(self.state.get('taskmgr_pid'))
        is_cpu_page = has_taskmgr and cur_state not in ('blind', 'waiting')
        detector_valid = (cur_state != 'invalid')
        native_ok = (self.current_provider_gap is None)

        if is_cpu_page and detector_valid and native_ok:
            today_entry['effective_monitoring_seconds'] = round(today_entry.get('effective_monitoring_seconds', 0.0) + dt, 1)
            today_entry['effective_monitoring_hours'] = round(today_entry['effective_monitoring_seconds'] / 3600.0, 2)

        if cur_state in ('blind', 'waiting'):
            today_entry['blind_seconds'] = round(today_entry.get('blind_seconds', 0.0) + dt, 1)

        if self.current_provider_gap is not None:
            today_entry['native_gap_seconds'] = round(today_entry.get('native_gap_seconds', 0.0) + dt, 1)

        if self.current_heartbeat_gap is not None:
            today_entry['heartbeat_gap_seconds'] = round(today_entry.get('heartbeat_gap_seconds', 0.0) + dt, 1)

        if len(self.daily_monitoring_history) > 30:
            self.daily_monitoring_history = self.daily_monitoring_history[-30:]

        if getattr(self, '_last_daily_save', 0) == 0 or now - self._last_daily_save >= 30:
            self._save_daily_monitoring()
            self._last_daily_save = now

    def tick(self):
        now = time.monotonic()
        if not self.enabled:
            self.publish('stopped')
            return
        try:
            if self.recorder and self.recorder.status()['state']=='stopped':
                self.recorder.start()
            if self.realtime and self.realtime.status()['state']=='stopped' and self.light_etw:
                try:
                    self.realtime.start()
                except Exception:
                    pass
            self.check_native_coverage(now)
            self.check_heartbeat_coverage(now)
            self.update_daily_monitoring(now)
            if self.pending_navigation:
                folder,saved=self.pending_navigation
                self.pending_navigation=None
                self.last_event=str(folder)
                self.dump_thread=threading.Thread(target=self.resume_navigation,args=(folder,saved),daemon=False)
                self.dump_thread.start()
            self.retry_recovery(now)
            self.check_blind_navigation(now)
            self.check_auto_open(now)
            if now-self.last_origin_scan >= 5:
                # Any page can hit the aggregation error, not only the CPU page.
                self.last_origin_scan = now
                for pid in taskmgr_pids():
                    self.watch_origin(pid)
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
                    self.reset_observations('capture_timeout')
                    self.publish('capture_timeout')
                return
            if now-self.last_request >= 2:
                self.pipe.send('sample')
                self.pending = self.last_request = now
        except Exception as exc:
            self.reset_worker()
            self.reset_observations('capture_error', {'error':str(exc)})
            self.publish('error', error=str(exc))

    def accept(self, sample):
        if self.rearm_requested:
            self.reset_observations('recovery_rearm')
            self.rearm_requested = False
        if sample['state'] != 'captured':
            if sample['state'] == 'waiting':
                now = time.monotonic()
                pid = sample.get('pid')
                if not pid:
                    pids = self.get_taskmgr_pids()
                    if pids:
                        pid = next(iter(pids))
                if pid:
                    self.taskmgr_was_running = True
                if self.waiting_since_monotonic is None:
                    self.waiting_since_monotonic = now
                    self.waiting_since_utc = timestamp()
                    self.current_blind_interval = {
                        'start_utc': self.waiting_since_utc,
                        'start_qpc': qpc(),
                        'start_monotonic': now,
                        'reason': 'post_recovery_navigation' if self.blind_after_recovery else 'waiting_for_cpu_page',
                        'after_recovery': bool(self.blind_after_recovery),
                        'taskmgr_pid': pid or self.state.get('taskmgr_pid')
                    }
                if pid and (not self.blind_navigation_target or self.blind_navigation_target.get('target_pid') != pid):
                    self.arm_blind_navigation(target_pid=pid, now=now)
            self.reset_observations('capture_state', {'state':sample['state'],'error':sample.get('error')})
            self.publish(sample['state'], error=sample.get('error'))
            return
        self.taskmgr_was_running = True
        self.boot_auto_open_checked = True
        if self.next_auto_open_monotonic is not None:
            self.clear_auto_open()
        if self.waiting_since_monotonic is not None:
            now = time.monotonic()
            if self.current_blind_interval:
                interval = dict(self.current_blind_interval)
                interval['end_utc'] = timestamp()
                interval['end_qpc'] = qpc()
                interval['duration_seconds'] = round(now - interval.pop('start_monotonic'), 1)
                self.observations.record_blind_interval(interval)
                self.current_blind_interval = None
            self.waiting_since_monotonic = None
            self.waiting_since_utc = None
            self.blind_after_recovery = False
            self.blind_navigation_target = None
            self.next_navigation_retry_monotonic = None
            self.next_navigation_retry_utc = None
            self.blind_retry_count = 0
        else:
            now = time.monotonic()
        self.watch_origin(sample['pid'])
        if self.realtime:
            self.realtime.set_target_pid(sample['pid'])
        if sample['pid'] != self.current_monitored_pid:
            self.current_monitored_pid = sample['pid']
            self.target_pid_start_monotonic = now
            self.last_known_event_count = self.realtime.status().get('provider_event_count', 0) if self.realtime else 0
            if self.current_provider_gap:
                self.current_provider_gap = None
        if self.retry and sample['pid'] != self.retry[1]['taskmgr_pid']:
            self.retry = None  # A working CPU page from another Taskmgr ends the retry.
        identity = (sample['pid'], sample['hwnd'], sample['total'].shape, sample['cores'].shape)
        if identity != self.identity:
            self.reset_observations('identity_or_geometry_changed', {'previous':self.identity,'current':identity})
            self.identity = identity
        if self.pending_recovery:
            folder, saved = self.pending_recovery
            self.pending_recovery = None
            if sample['pid']==saved['taskmgr_pid'] and sample['hwnd']==saved['window']:
                self.last_event=str(folder)
                self.capture_result='complete'
                self.dump_thread=threading.Thread(target=self.recover,args=(folder,saved),daemon=False)
                self.dump_thread.start()
        now = time.monotonic()
        if self.observations.last_time is not None and not 0 < now-self.observations.last_time <= 6:
            self.reset_observations('sample_clock_gap', {'seconds':now-self.observations.last_time})
        has_native = False
        if self.realtime:
            ne = self.realtime.poll_native_error()
            if (ne and ne.get('taskmgr_pid') == sample['pid'] and
                str(ne.get('hresult', '')).upper() in ('0X8007139F', '2147947423', '-2147019873') and
                ne.get('module') == 'TaskManagerDataLayer.dll'):
                has_native = True
        result = self.detector.observe(now, sample['total'], sample['cores'],
                                       system_cpu_percent=sample.get('system_cpu_percent'),
                                       native_corroborated=has_native)
        frame = {'timestamp':timestamp(), 'qpc':qpc(), 'monotonic':now, 'result':result, 'images':{}}
        for key in ('total','cores'):
            ok, encoded = cv2.imencode('.png',sample[key])
            if not ok:
                raise RuntimeError('PNG encoding failed')
            frame['images'][key] = encoded.tobytes()
        self.frames.append(frame)
        self.observations.observe(frame)
        if VERIFY_REQUEST.exists() and not (self.dump_thread and self.dump_thread.is_alive()):
            recovery_test=VERIFY_REQUEST.read_text(encoding='ascii').strip()=='recovery_check'
            VERIFY_REQUEST.unlink()
            self.trigger(sample, verification=True, recovery_test=recovery_test)
            return
        if self.realtime:
            native_error = self.realtime.poll_native_error()
            if (native_error and native_error.get('taskmgr_pid') == sample['pid'] and
                str(native_error.get('hresult', '')).upper() in ('0X8007139F', '2147947423', '-2147019873') and
                native_error.get('module') == 'TaskManagerDataLayer.dll'):
                t_native = native_error['timestamp_monotonic']
                dt = now - t_native
                flat_since = self.detector.since
                if flat_since is not None and self.detector.anchor is not None:
                    corroboration_seconds = now - max(flat_since, t_native)
                    if dt >= 10.0 and corroboration_seconds >= 10.0:
                        self.realtime.clear_native_error()
                        trigger_kind = 'both' if result['state'] == 'suspect' else 'native_fast'
                        self.trigger(sample, trigger_kind=trigger_kind, native_error=native_error,
                                     corroboration_seconds=corroboration_seconds)
                        return
                elif dt >= 25.0:
                    self.realtime.clear_native_error()
                    self.observations.record_uncorroborated_native_error(native_error, 'chart_not_flat_for_10s')
                    try:
                        record_outside_flatline(OUTPUT, native_error, 'chart_not_flat_for_10s')
                        today_date = datetime.now(timezone.utc).strftime('%Y-%m-%d')
                        for entry in self.daily_monitoring_history:
                            if entry.get('date') == today_date:
                                entry['native_0x8007139f_count'] = entry.get('native_0x8007139f_count', 0) + 1
                                break
                        self._save_daily_monitoring()
                    except Exception:
                        pass

        if result['state'] == 'suspect' and (result['alert'] or self.capture_result in ('dump_failed','recovered',None)):
            native_error = self.realtime.clear_native_error() if self.realtime else None
            trigger_kind = 'both' if native_error else 'visual'
            corroboration_seconds = (now - native_error['timestamp_monotonic']) if native_error else None
            self.trigger(sample, trigger_kind=trigger_kind, native_error=native_error,
                         corroboration_seconds=corroboration_seconds)
            return
        self.publish(result['state'], taskmgr_pid=sample['pid'], seconds=result['seconds'],
                     logical_graphs=len(sample['geometry'])-1,
                     detector_kind=result.get('detector_kind'),
                     detector_reason=result.get('reason'),
                     system_cpu_percent=sample.get('system_cpu_percent'))

    def trigger(self, sample, verification=False, recovery_test=False,
                trigger_kind='visual', native_error=None, corroboration_seconds=None):
        if self.dump_thread and self.dump_thread.is_alive():
            self.publish(self.background_phase(),taskmgr_pid=sample['pid'])
            return
        # A replacement Taskmgr must get its own evidence and recovery immediately
        # after confirmation. Rate limiting the previous PID blocks that recovery.
        target=(sample['pid'],sample['hwnd'])
        remaining=(RESTART_COOLDOWN if self.restart_only else CAPTURE_COOLDOWN)-(time.monotonic()-self.last_capture)
        if not verification and self.last_capture and target==self.last_capture_target and remaining > 0:
            self.publish('cooldown',taskmgr_pid=sample['pid'],cooldown_remaining=round(remaining))
            return
        if self.restart_only and not verification:
            self.restart_now(sample, target, trigger_kind=trigger_kind, native_error=native_error,
                             corroboration_seconds=corroboration_seconds)
            return
        if not storage_available():
            self.publish('storage_limit')
            return
        prefix = 'verification-' if verification else ''
        folder = OUTPUT/(prefix+datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
        folder.mkdir()
        if self.realtime:
            try:
                write_json(folder/'heartbeat.json', {
                    'schema_version': 1,
                    'event_count': len(self.realtime.get_heartbeat_events()),
                    'filter_type': 'EVENT_FILTER_TYPE_EVENT_NAME',
                    'filter_names': ['CurrentUpdateCycle', 'FallbackError'],
                    'events': self.realtime.get_heartbeat_events()
                })
            except Exception:
                pass
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
            self.last_capture_target = target
        self.last_event = str(folder)
        self.capture_result = 'capturing'
        self.recovery_result = None
        self.dump_thread = threading.Thread(target=self.capture_dumps,args=(folder,evidence),daemon=False)
        self.dump_thread.start()
        self.publish('capturing',taskmgr_pid=sample['pid'])

    def restart_now(self, sample, target, trigger_kind='visual', native_error=None, corroboration_seconds=None):
        folder = OUTPUT/('restart-'+datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
        folder.mkdir()
        if self.realtime:
            try:
                write_json(folder/'heartbeat.json', {
                    'schema_version': 1,
                    'event_count': len(self.realtime.get_heartbeat_events()),
                    'filter_type': 'EVENT_FILTER_TYPE_EVENT_NAME',
                    'filter_names': ['CurrentUpdateCycle', 'FallbackError'],
                    'events': self.realtime.get_heartbeat_events()
                })
            except Exception:
                pass
        record = {'classification':'restart_only', 'timestamp':timestamp(), 'taskmgr_pid':sample['pid'],
                  'window':sample['hwnd'],
                  'confirm_seconds':30 if trigger_kind == 'visual' else round(corroboration_seconds or 10.0, 1),
                  'trigger_kind':trigger_kind, 'status':'complete'}
        if native_error:
            record['native_event_time'] = native_error.get('timestamp_utc')
            record['native_event_qpc'] = native_error.get('timestamp_qpc')
        if corroboration_seconds is not None:
            record['visual_corroboration_seconds'] = round(corroboration_seconds, 1)
        write_json(folder/'event.json',record)
        self.last_capture = time.monotonic()
        self.last_capture_target = target
        self.last_event = str(folder)
        self.capture_result = None
        self.recovery_result = None
        frames = list(self.frames)
        observation = self.observations.record(frames, sample['pid'], os.getpid(),
                                               current_blind=self.current_blind_interval,
                                               trigger_kind=trigger_kind, native_error=native_error,
                                               corroboration_seconds=corroboration_seconds,
                                               current_native_gap=self.current_provider_gap,
                                               current_heartbeat_gap=self.current_heartbeat_gap) if self.recorder else None
        self.dump_thread = threading.Thread(target=self.restart_with_trace,
                                            args=(folder,record,frames,observation),daemon=False)
        self.dump_thread.start()
        self.publish('recovering',taskmgr_pid=sample['pid'])

    def restart_with_trace(self, folder, record, frames, observation):
        ready = None
        if self.recorder:
            try:
                write_json(folder/'observation.json', observation)
                markers = {m['qpc'] for m in observation['markers'].values() if m}
                for i, frame in enumerate(frames):
                    if i in {0,len(frames)//2,len(frames)-1} or frame['qpc'] in markers:
                        for key, data in frame['images'].items():
                            (folder/f'{i:02d}-{key}.png').write_bytes(data)
                started = time.monotonic()
                ready = self.recorder.snapshot(folder)
                completed = ready.wait(SNAPSHOT_WAIT_SECONDS)
                record['light_etw'] = {'snapshot_finished_before_recovery':completed,
                                       'wait_seconds':round(time.monotonic()-started,3),
                                       'wait_limit_seconds':SNAPSHOT_WAIT_SECONDS,
                                       'result_file':'trace-capture.json'}
            except Exception as exc:
                record['light_etw'] = {'error':repr(exc)}
        # Evidence collection failure must never disable the visual safety net.
        self.recover(folder, record)
        if ready is not None:
            try:
                self.offline.submit(folder, ready)
            except Exception as exc:
                write_json(folder/'analysis-status.json', {'status':'failed','error':repr(exc)})

    def set_restart_only(self, value):
        # Light daily recording never implicitly enables the old debugger mode.
        self.restart_only = bool(value or self.light_etw)
        if self.restart_only:
            # Detaching can take seconds; never block the tray on it.
            threading.Thread(target=self.origin.cancel_all,daemon=True).start()
        self.last_status_write = 0

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
        if evidence.get('classification') not in ('suspected_visual_aggregate_freeze','recovery_verification','restart_only') or evidence.get('status')!='complete':
            return
        try:
            if evidence['classification']!='restart_only':
                if len(evidence.get('dumps',[]))!=2 or not all(d.get('success') for d in evidence['dumps']):
                    raise RuntimeError('Two successful dumps are required before restart')
                for index in (1,2):
                    validate_full_dump(folder/f'taskmgr-{index}.dmp')
            evidence['recovery']={'status':'restarting','started':timestamp()}
            write_json(folder/'event.json',evidence)
            self.recovery_result=evidence['recovery']
            self.last_status_write=0
            self.recovery_marker(folder,evidence,'recovery_started')
            result=restart_taskmgr(evidence['taskmgr_pid'],evidence['window'])
            if result.get('launch_pid'):
                self.watch_origin(result['launch_pid'])
            evidence['recovery'].update(result,finished=timestamp())
            self.capture_result='recovered' if result['status']=='restarted' else 'recovery_failed'
            if result.get('status') == 'restarted_waiting_for_cpu_page':
                save_navigation_failure_window(result.get('launch_pid'), folder)
                self.blind_after_recovery = True
                if self.current_blind_interval:
                    self.current_blind_interval['after_recovery'] = True
                    self.current_blind_interval['reason'] = 'post_recovery_navigation'
        except Exception as exc:
            evidence['recovery']={'status':'failed','error':str(exc),'finished':timestamp()}
            self.capture_result='recovery_failed'
        finally:
            self.recovery_marker(folder,evidence,'recovery_finished')
            self.schedule_retry(folder,evidence)
            write_json(folder/'event.json',evidence)
            self.recovery_result=evidence.get('recovery')
            self.rearm_requested=True

    def recovery_marker(self, folder, evidence, name):
        try:
            mark_recovery(folder,name,timestamp())
        except Exception as exc:
            evidence.setdefault('observation_errors',[]).append(repr(exc))

    def resume_navigation(self, folder, evidence, target_pid=None):
        if target_pid is None and evidence:
            target_pid = evidence.get('recovery', {}).get('launch_pid') or evidence.get('recovery', {}).get('new_pid')
        if not target_pid:
            return
        if evidence:
            self.recovery_result = evidence.get('recovery')
        self.auto_navigation_count += 1
        self.auto_navigation_last_utc = timestamp()
        try:
            result = restore_cpu_page(target_pid)
            self.auto_navigation_last_result = result.get('status', 'unknown')
            if evidence and 'recovery' in evidence:
                evidence['recovery'].update(result, finished=timestamp())
            if result.get('status') == 'restarted':
                if evidence:
                    self.capture_result = 'recovered'
                self.blind_after_recovery = False
                self.blind_navigation_target = None
                self.next_navigation_retry_monotonic = None
                self.next_navigation_retry_utc = None
                self.blind_retry_count = 0
            else:
                if folder:
                    save_navigation_failure_window(target_pid, folder)
                if evidence:
                    self.capture_result = 'recovery_failed'
                    self.blind_after_recovery = True
                if self.current_blind_interval:
                    self.current_blind_interval['after_recovery'] = bool(self.blind_after_recovery)
                    if self.blind_after_recovery:
                        self.current_blind_interval['reason'] = 'post_recovery_navigation'
        except Exception as exc:
            self.auto_navigation_last_result = f"error: {exc}"
            if evidence and 'recovery' in evidence:
                evidence['recovery'].update(error=str(exc), finished=timestamp())
                self.capture_result = 'recovery_failed'
                self.blind_after_recovery = True
            if self.current_blind_interval and self.blind_after_recovery:
                self.current_blind_interval['after_recovery'] = True
                self.current_blind_interval['reason'] = 'post_recovery_navigation'
        finally:
            if evidence and folder:
                if not self.blind_navigation_target:
                    self.schedule_retry(folder, evidence)
                write_json(folder/'event.json', evidence)
                self.recovery_result = evidence.get('recovery')
                self.rearm_requested = True

    def schedule_retry(self, folder, evidence):
        if self.capture_result != 'recovery_failed':
            self.retry = None
            return
        if self.retry_folder != folder:
            self.retry_folder, self.retry_count = folder, 0
        self.retry = (folder, evidence)
        self.last_retry = time.monotonic()
        evidence['recovery']['retries'] = self.retry_count
        if self.retry_count >= RETRY_LIMIT:
            evidence['recovery']['retry_exhausted'] = True
            self.retry = None
            if evidence.get('recovery', {}).get('status') == 'restarted_waiting_for_cpu_page':
                self.blind_after_recovery = True
                self.arm_blind_navigation(folder, evidence)

    def arm_blind_navigation(self, folder=None, evidence=None, now=None, target_pid=None):
        if target_pid is None and evidence:
            target_pid = evidence.get('recovery', {}).get('launch_pid') or evidence.get('recovery', {}).get('new_pid')
        if not target_pid:
            return
        self.blind_navigation_target = {'folder': folder, 'evidence': evidence, 'target_pid': target_pid}
        delay = BLIND_BACKOFF_SCHEDULE[min(self.blind_retry_count, len(BLIND_BACKOFF_SCHEDULE)-1)]
        cur = time.monotonic() if now is None else now
        self.next_navigation_retry_monotonic = cur + delay
        self.next_navigation_retry_utc = datetime.fromtimestamp(time.time() + (cur - time.monotonic()) + delay).astimezone().isoformat()

    def check_blind_navigation(self, now):
        if not self.blind_navigation_target or (self.dump_thread and self.dump_thread.is_alive()):
            return
        target_pid = self.blind_navigation_target['target_pid']
        if not is_taskmgr_process_alive(target_pid):
            self.blind_navigation_target = None
            self.next_navigation_retry_monotonic = None
            self.next_navigation_retry_utc = None
            return
        if self.next_navigation_retry_monotonic is None or now < self.next_navigation_retry_monotonic:
            return
        cutoff = now - 3600
        self.blind_retry_history = [t for t in self.blind_retry_history if t > cutoff]
        if len(self.blind_retry_history) >= MAX_BLIND_RETRIES_PER_HOUR:
            delay = max(60, self.blind_retry_history[0] + 3600 - now)
            self.next_navigation_retry_monotonic = now + delay
            self.next_navigation_retry_utc = datetime.fromtimestamp(time.time() + delay).astimezone().isoformat()
            return
        if is_user_interacting_with_window(target_pid):
            self.next_navigation_retry_monotonic = now + 60
            self.next_navigation_retry_utc = datetime.fromtimestamp(time.time() + 60).astimezone().isoformat()
            return

        folder = self.blind_navigation_target.get('folder')
        evidence = self.blind_navigation_target.get('evidence')
        self.blind_retry_history.append(now)
        self.blind_retry_count += 1
        next_delay = BLIND_BACKOFF_SCHEDULE[min(self.blind_retry_count, len(BLIND_BACKOFF_SCHEDULE)-1)]
        self.next_navigation_retry_monotonic = now + next_delay
        self.next_navigation_retry_utc = datetime.fromtimestamp(time.time() + next_delay).astimezone().isoformat()
        if folder:
            self.last_event = str(folder)
        self.dump_thread = threading.Thread(target=self.resume_navigation, args=(folder, evidence, target_pid), daemon=False)
        self.dump_thread.start()

    def schedule_auto_open(self, delay, reason, now=None):
        cur = time.monotonic() if now is None else now
        self.next_auto_open_monotonic = cur + delay
        target_time = time.time() + (cur - time.monotonic()) + delay
        open_dt = datetime.fromtimestamp(target_time).astimezone()
        self.next_auto_open_utc = open_dt.isoformat()
        time_str = open_dt.strftime('%H:%M:%S')
        self.next_auto_open_text_zh = f"任务管理器未运行，将于 {time_str} 自动打开"
        self.next_auto_open_text_en = f"Task Manager not running; will open automatically at {time_str}"
        self.auto_open_reason = reason

    def clear_auto_open(self):
        self.next_auto_open_monotonic = None
        self.next_auto_open_utc = None
        self.next_auto_open_text_zh = None
        self.next_auto_open_text_en = None
        self.auto_open_reason = None

    def check_auto_open(self, now):
        if self.dump_thread and self.dump_thread.is_alive():
            return
        pids = self.get_taskmgr_pids()
        has_pids = bool(pids)

        if not self.boot_auto_open_checked:
            self.boot_auto_open_checked = True
            if has_pids:
                self.taskmgr_was_running = True
            else:
                self.taskmgr_was_running = False
                # Boot startup: wait for desktop ready, then open Taskmgr
                self.schedule_auto_open(2.0, 'boot_startup', now=now)
            return

        if has_pids:
            self.taskmgr_was_running = True
            if self.next_auto_open_monotonic is not None:
                self.clear_auto_open()
            return

        # If Taskmgr was running and now exited (and not in recovery restart):
        if self.taskmgr_was_running is True and not self.recovery_result:
            self.taskmgr_was_running = False
            # User closed Taskmgr. Wait 10 minutes to respect user intent before reopening.
            self.schedule_auto_open(USER_CLOSED_COOLDOWN_SECONDS, 'user_closed', now=now)
            return

        if self.next_auto_open_monotonic is None or now < self.next_auto_open_monotonic:
            return

        if not is_desktop_ready():
            return

        if is_user_interacting_system():
            self.schedule_auto_open(60.0, self.auto_open_reason or 'user_closed', now=now)
            return

        self.clear_auto_open()
        self.dump_thread = threading.Thread(target=self._run_auto_open, daemon=False)
        self.dump_thread.start()

    def _run_auto_open(self):
        self.auto_open_count += 1
        self.auto_open_last_utc = timestamp()
        try:
            result = launch_taskmgr()
            self.auto_open_last_result = result.get('status', 'unknown')
            pid = result.get('launch_pid') or result.get('new_pid')
            if pid:
                self.watch_origin(pid)
            if result.get('status') == 'restarted':
                self.taskmgr_was_running = True
                self.auto_open_reason = None
            else:
                self.taskmgr_was_running = bool(pid and self.is_pid_alive(pid))
        except Exception as exc:
            self.auto_open_last_result = f"error: {exc}"
            self.schedule_auto_open(120.0, 'retry_after_error')

    def retry_recovery(self, now):
        """Bounded retry of a failed recovery; the saved dumps are revalidated, never retaken."""
        if not self.retry or (self.dump_thread and self.dump_thread.is_alive()):
            return
        if now-self.last_retry < RETRY_INTERVAL:
            return
        folder, evidence = self.retry
        self.retry = None
        self.retry_count += 1
        self.last_event = str(folder)
        recovery = evidence.get('recovery', {})
        if recovery.get('status') == 'restarted_waiting_for_cpu_page':
            target, args = self.resume_navigation, (folder, evidence)
        elif recovery.get('status') == 'failed':
            evidence = {k:v for k,v in evidence.items() if k != 'recovery'}
            target, args = self.recover, (folder, evidence)
        else:
            return
        self.dump_thread = threading.Thread(target=target, args=args, daemon=False)
        self.dump_thread.start()

    def watch_origin(self, pid):
        """Attach the first-report monitor once per Task Manager instance."""
        if pid in self.origin.attempted or not self.enabled or self.restart_only:
            return
        try:
            if not storage_available():
                return
            require_taskmgr(pid)
            self.origin.attach(pid)
            self.origin_error = None
        except Exception as exc:
            self.origin.attempted.add(pid)
            self.origin_error = str(exc)

    def stop(self):
        self.enabled = False
        self.origin.cancel_all()
        self.reset_worker()
        if self.recorder:
            self.recorder.stop()
        if self.realtime:
            self.realtime.stop()
        self.reset_observations('monitor_stopped')
        self.last_status_write = 0
        self.publish('stopped')
