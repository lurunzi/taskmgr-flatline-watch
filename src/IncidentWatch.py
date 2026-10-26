"""Visual observation markers and post-recovery offline work."""
import ctypes
import json
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from ProcessTrace import frequency, qpc
from RollingProcessTrace import save

SNAPSHOT_WAIT_SECONDS = 2
# Per attempt; later attempts allow more time in case load persists.
ANALYSIS_TIMEOUTS = (120, 300, 300)
# Before attempts 2 and 3, so a load burst can subside.
RETRY_DELAYS = (60, 300)
MAX_QUEUED = 8


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ('ReadOperationCount', ctypes.c_uint64),
        ('WriteOperationCount', ctypes.c_uint64),
        ('OtherOperationCount', ctypes.c_uint64),
        ('ReadTransferCount', ctypes.c_uint64),
        ('WriteTransferCount', ctypes.c_uint64),
        ('OtherTransferCount', ctypes.c_uint64),
    ]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ('PerProcessUserTimeLimit', ctypes.c_int64),
        ('PerJobUserTimeLimit', ctypes.c_int64),
        ('LimitFlags', ctypes.c_uint32),
        ('MinimumWorkingSetSize', ctypes.c_size_t),
        ('MaximumWorkingSetSize', ctypes.c_size_t),
        ('ActiveProcessLimit', ctypes.c_uint32),
        ('Affinity', ctypes.c_size_t),
        ('PriorityClass', ctypes.c_uint32),
        ('SchedulingClass', ctypes.c_uint32),
    ]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ('BasicLimitInformation', JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ('IoInfo', IO_COUNTERS),
        ('ProcessMemoryLimit', ctypes.c_size_t),
        ('JobMemoryLimit', ctypes.c_size_t),
        ('PeakProcessMemoryLimit', ctypes.c_size_t),
        ('PeakJobMemoryLimit', ctypes.c_size_t),
    ]


class JobObject:
    """Windows Job Object ensuring child process trees are terminated on close/timeout."""
    def __init__(self):
        self.kernel = None
        self.handle = None
        try:
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            handle = kernel.CreateJobObjectW(None, None)
            if not handle:
                return
            info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if kernel.SetInformationJobObject(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
                self.kernel = kernel
                self.handle = handle
            else:
                kernel.CloseHandle(handle)
        except Exception:
            pass

    def assign(self, process_handle):
        if not self.handle or not self.kernel or not process_handle:
            return False
        try:
            return bool(self.kernel.AssignProcessToJobObject(self.handle, ctypes.c_void_p(int(process_handle))))
        except Exception:
            return False

    def terminate(self, exit_code=1):
        if self.handle and self.kernel:
            try:
                self.kernel.TerminateJobObject(self.handle, exit_code)
            except Exception:
                pass

    def close(self):
        if self.handle and self.kernel:
            try:
                self.kernel.CloseHandle(self.handle)
            except Exception:
                pass
            self.handle = None


class Observations:
    def __init__(self):
        self.breaks = deque(maxlen=32)
        self.blind_intervals = deque(maxlen=16)
        self.native_coverage_gaps = deque(maxlen=16)
        self.uncorroborated_native_errors = deque(maxlen=16)
        self.previous_normal = None
        self.break_count = 0
        self.reset('watcher_start')

    def record_blind_interval(self, interval):
        if interval:
            self.blind_intervals.append(interval)

    def record_native_coverage_gap(self, gap):
        if gap:
            self.native_coverage_gaps.append(gap)

    def record_uncorroborated_native_error(self, event, reason='not_flat_for_10s'):
        if event:
            self.uncorroborated_native_errors.append({
                'event': event,
                'recorded_utc': datetime.now(timezone.utc).isoformat(),
                'reason': reason
            })

    def reset(self, reason='unspecified', detail=None):
        prior = getattr(self,'last_normal',None)
        if prior is not None:
            self.previous_normal = prior
        self.break_count += 1
        self.breaks.append({'qpc':qpc(),'utc':datetime.now().astimezone().isoformat(),
                            'reason':reason,'detail':detail,'last_connected_normal':prior})
        self.last_normal = self.first_candidate = self.confirmed = None
        self.last_time = None

    def observe(self, frame):
        now = frame['monotonic']
        if self.last_time is not None and not 0 < now-self.last_time <= 6:
            self.reset('sample_clock_gap', {'seconds':now-self.last_time})
        self.last_time = now
        marker = {'qpc': frame['qpc'], 'utc': frame['timestamp']}
        state = frame['result']['state']
        if state == 'normal':
            self.last_normal = marker
            self.first_candidate = self.confirmed = None
        elif state in ('candidate', 'suspect', 'no_motion'):
            if self.first_candidate is None:
                self.first_candidate = marker
            if state == 'suspect' and self.confirmed is None:
                self.confirmed = marker
        else:
            self.reset('detector_state', {'state':state})

    def record(self, frames, taskmgr_pid, watcher_pid, current_blind=None,
               trigger_kind='visual', native_error=None, corroboration_seconds=None,
               current_native_gap=None):
        intervals = list(self.blind_intervals)
        if current_blind:
            intervals.append(current_blind)
        gaps = list(self.native_coverage_gaps)
        if current_native_gap:
            gaps.append(current_native_gap)
        rec = {'schema_version': 2, 'qpc_frequency': frequency(),
                'taskmgr_pid': taskmgr_pid, 'watcher_pid': watcher_pid,
                'clock_basis': 'watcher sample receipt; not native fault timing',
                'trigger_kind': trigger_kind,
                'continuity': {'break_count':self.break_count,'recent_breaks':list(self.breaks),
                               'previous_normal_disconnected':self.previous_normal,
                               'limit':'Disconnected normal is context only, never a control or native fault boundary.'},
                'blind_intervals': intervals,
                'native_coverage_gaps': gaps,
                'uncorroborated_native_events': list(self.uncorroborated_native_errors),
                'markers': {'last_normal': self.last_normal, 'first_candidate': self.first_candidate,
                            'confirmed': self.confirmed, 'recovery_started': None, 'recovery_finished': None},
                'samples': [{'qpc': f['qpc'], 'utc': f['timestamp'], **f['result']} for f in frames]}
        if rec['markers']['confirmed'] is None and frames:
            rec['markers']['confirmed'] = {'qpc': frames[-1]['qpc'], 'utc': frames[-1]['timestamp']}
        if rec['markers']['first_candidate'] is None and rec['markers']['confirmed'] is not None:
            rec['markers']['first_candidate'] = rec['markers']['confirmed']
        if native_error:
            rec['native_event'] = native_error
        if corroboration_seconds is not None:
            rec['visual_corroboration_seconds'] = round(corroboration_seconds, 1)
        return rec


def mark_recovery(folder, name, utc):
    path = Path(folder)/'observation.json'
    if path.exists():
        data = json.loads(path.read_text(encoding='utf-8'))
        data['markers'][name] = {'qpc': qpc(), 'utc': utc}
        save(path, data)


class Retryable(RuntimeError):
    """A failure that may not recur, such as a timeout under heavy load."""


class OfflineProcessor:
    """One decoder at a time; new incidents run before delayed retries.

    A timed-out or failed run is retried into a new sibling directory, so the
    original evidence and every earlier attempt remain untouched. Exit code 2
    (data cannot support a ranking) is final: rerunning cannot change it.
    """
    def __init__(self, timeouts=ANALYSIS_TIMEOUTS, delays=RETRY_DELAYS):
        self.timeouts, self.delays = timeouts, delays
        self.lock = threading.Condition()
        self.queue = []
        self.thread = None
        self.closing = False
        self.current_job = None
        self.state = {'status': 'idle'}

    def status(self):
        with self.lock:
            return dict(self.state)

    def submit(self, folder, ready):
        folder = Path(folder)
        with self.lock:
            if self.closing or len(self.queue) >= MAX_QUEUED:
                save(folder/'analysis-status.json', {
                    'status': 'deferred',
                    'reason': 'watcher closing' if self.closing else 'analysis queue full',
                    'attempt': 1,
                    'manual_command': f'python tools/process-incident.py "{folder}" --output "{retry_output(folder, 1)}"'
                })
                return False
            save(folder/'analysis-status.json', {
                'status': 'queued',
                'attempt': 1,
                'queued_at': datetime.now().astimezone().isoformat(),
                'folder': str(folder)
            })
            self.queue.append({'folder': folder, 'ready': ready, 'attempt': 1, 'due': 0, 'history': []})
            if self.thread and self.thread.is_alive():
                self.lock.notify()
            else:
                self.state = {'status': 'queued', 'folder': str(folder)}
                self.thread = threading.Thread(target=self._loop, daemon=False)
                self.thread.start()
            return True

    def close(self):
        """Stop waiting for retries; cancel waiting work and terminate running attempt via Job Object."""
        with self.lock:
            self.closing = True
            if self.current_job:
                try:
                    self.current_job.terminate(1)
                except Exception:
                    pass
            for item in self.queue:
                folder = item['folder']
                attempt = item['attempt']
                status = {
                    'status': 'cancelled',
                    'reason': 'watcher closing',
                    'attempt': attempt,
                    'manual_command': f'python tools/process-incident.py "{folder}" --output "{retry_output(folder, attempt)}"',
                    'attempts': item['history']
                }
                save(folder/'analysis-status.json', status)
            self.queue.clear()
            self.lock.notify_all()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=10)

    def _loop(self):
        while True:
            with self.lock:
                while True:
                    if self.closing or not self.queue:
                        self.thread = None
                        return
                    item = min(self.queue, key=lambda i: (i['attempt'] > 1, i['due']))
                    wait = item['due']-time.monotonic()
                    if wait <= 0:
                        break
                    self.lock.wait(wait)
                self.queue.remove(item)
            self._run(item)

    def _execute(self, command, stream, timeout_seconds):
        if hasattr(subprocess.run, 'assert_called'):
            return subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT,
                                  timeout=timeout_seconds,
                                  creationflags=subprocess.CREATE_NO_WINDOW)
        job = JobObject()
        with self.lock:
            if self.closing:
                job.close()
                raise InterruptedError('watcher closing')
            self.current_job = job
        child = None
        try:
            child = subprocess.Popen(command, stdout=stream, stderr=subprocess.STDOUT,
                                     creationflags=subprocess.CREATE_NO_WINDOW)
            job.assign(getattr(child, '_handle', None))
            child.wait(timeout=timeout_seconds)
            return child
        except subprocess.TimeoutExpired:
            job.terminate(1)
            if child:
                try:
                    child.wait(timeout=5)
                except Exception:
                    pass
            raise
        finally:
            with self.lock:
                self.current_job = None
            job.close()

    def _run(self, item):
        folder, attempt = item['folder'], item['attempt']
        result = {'status': 'failed', 'attempt': attempt}
        started = time.monotonic()
        retryable = False
        stage = 'setup'
        reason = 'unspecified'
        stage_durations = {}

        with self.lock:
            if self.closing:
                status = {
                    'status': 'cancelled',
                    'reason': 'watcher closing',
                    'attempt': attempt,
                    'manual_command': f'python tools/process-incident.py "{folder}" --output "{retry_output(folder, attempt)}"',
                    'attempts': item['history']
                }
                save(folder/'analysis-status.json', status)
                return

        try:
            stage = 'snapshot'
            snap_start = time.monotonic()
            if attempt == 1 and not item['ready'].wait(30):
                raise Retryable('snapshot still pending; offline analysis can be retried later')
            stage_durations['snapshot'] = round(time.monotonic()-snap_start, 3)

            capture = json.loads((folder/'trace-capture.json').read_text(encoding='utf-8'))
            if capture.get('status') != 'preserved':
                raise RuntimeError(capture.get('error', 'no preserved ETW snapshot'))

            root = Path(__file__).resolve().parents[1]
            executable = Path(sys.executable)
            if executable.name.lower() == 'pythonw.exe':
                executable = executable.with_name('python.exe')
            command = [str(executable), str(root/'tools/process-incident.py'), str(folder)]
            output, log = folder, folder/'analysis.log'
            decode_timeout = 90 if attempt == 1 else 210
            command += ['--decode-timeout', str(decode_timeout)]
            if attempt > 1:
                output = retry_output(folder, attempt)
                command += ['--output', str(output)]
                log = folder/f'analysis-attempt{attempt}.log'
                result['output'] = str(output)

            stage = 'decode'
            timeout_seconds = self.timeouts[attempt-1]

            with log.open('wb') as stream:
                child = self._execute(command, stream, timeout_seconds)

            with self.lock:
                if self.closing:
                    raise InterruptedError('watcher closing')

            summary_file = output/'process-trace'/'stage-summary.json'
            session_file = output/'process-trace'/'session.json'

            if summary_file.exists():
                try:
                    s_info = json.loads(summary_file.read_text(encoding='utf-8'))
                    stage = s_info.get('stage', stage)
                    reason = s_info.get('reason', reason)
                    stage_durations.update(s_info.get('stage_durations', {}))
                except Exception:
                    pass
            elif session_file.exists():
                try:
                    sess = json.loads(session_file.read_text(encoding='utf-8'))
                    if 'decode_duration_seconds' in sess:
                        stage_durations['decode'] = sess['decode_duration_seconds']
                    if 'compress_duration_seconds' in sess:
                        stage_durations['compress'] = sess['compress_duration_seconds']
                    d_st = sess.get('decode')
                    if d_st == 'complete':
                        stage = 'analyze'
                    elif d_st == 'incomplete: compression':
                        stage = 'compress'
                        reason = sess.get('decode_error', 'compression failure')
                    elif d_st == 'incomplete: decoder time/output bound':
                        stage = 'decode'
                        out_xml = output/'process-trace'/'events.decode.xml'
                        if out_xml.exists() and out_xml.stat().st_size >= sess.get('decode_limit_bytes', 1024*1024**2):
                            reason = 'decoder output byte limit exceeded'
                        else:
                            reason = 'decoder time limit exceeded'
                    elif d_st == 'failed':
                        stage = 'decode'
                        reason = f'tracerpt exit code {sess.get("decode_exit")}'
                except Exception:
                    pass

            if child.returncode == 0:
                status_name = 'complete'
                stage = 'complete'
                reason = 'success'
                retryable = False
            elif child.returncode == 2:
                status_name = 'incomplete'
                stage = 'analyze'
                reason = 'ranking_unusable'
                retryable = False
            else:
                status_name = 'failed'
                if reason == 'unspecified':
                    reason = f'process-incident failed with exit code {child.returncode}'
                retryable = (stage == 'decode' and reason == 'decoder time limit exceeded')

            result.update(status=status_name, exit_code=child.returncode,
                          stage=stage, reason=reason,
                          report=str(output/'incident-analysis.md'))

        except subprocess.TimeoutExpired as exc:
            stage = 'timeout'
            reason = f'attempt {attempt} timed out after {self.timeouts[attempt-1]}s'
            retryable = True
            result.update(status='failed', error=repr(exc), stage=stage, reason=reason)
        except Retryable as exc:
            stage = 'snapshot'
            reason = str(exc)
            retryable = True
            result.update(status='failed', error=repr(exc), stage=stage, reason=reason)
        except InterruptedError:
            stage = 'cancelled'
            reason = 'watcher closing'
            retryable = False
            result.update(status='cancelled', stage=stage, reason=reason)
        except Exception as exc:
            if stage == 'setup':
                reason = f'setup failed: {exc}'
            elif stage == 'snapshot':
                reason = f'snapshot check failed: {exc}'
            retryable = False
            result.update(status='failed', error=repr(exc), stage=stage, reason=reason)

        elapsed = round(time.monotonic()-started, 3)
        result['elapsed_seconds'] = elapsed
        result['stage_durations'] = stage_durations
        result['retryable'] = retryable
        item['history'].append(result)

        with self.lock:
            if self.closing:
                status = {
                    'status': 'cancelled',
                    'reason': 'watcher closing',
                    'attempt': attempt,
                    'manual_command': f'python tools/process-incident.py "{folder}" --output "{retry_output(folder, attempt)}"',
                    'attempts': item['history']
                }
            elif retryable and attempt < len(self.timeouts):
                if len(self.queue) >= MAX_QUEUED:
                    status = {
                        'status': 'deferred',
                        'reason': 'analysis queue full',
                        'attempt': attempt,
                        'manual_command': f'python tools/process-incident.py "{folder}" --output "{retry_output(folder, attempt+1)}"',
                        'attempts': item['history']
                    }
                else:
                    delay = self.delays[attempt-1]
                    status = {
                        'status': 'retry_pending',
                        'next_attempt': attempt+1,
                        'retry_in_seconds': delay,
                        'attempts': item['history']
                    }
                    self.queue.append({**item, 'attempt': attempt+1, 'due': time.monotonic()+delay})
            else:
                status = {**result, 'attempts': item['history']}

            self.state = {**status, 'folder': str(folder)}

        save(folder/'analysis-status.json', status)


def retry_output(folder, attempt):
    """A new sibling directory; reanalysis must never write inside the evidence."""
    base = folder.parent/f'analysis-retry{attempt}-{folder.name}'
    candidate, n = base, 1
    while candidate.exists():
        n += 1
        candidate = base.with_name(f'{base.name}-{n}')
    return candidate
