"""Visual observation markers and post-recovery offline work."""
import json
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

from ProcessTrace import frequency, qpc
from RollingProcessTrace import save

SNAPSHOT_WAIT_SECONDS = 2
# Per attempt; later attempts allow more time in case load persists.
ANALYSIS_TIMEOUTS = (120, 300, 300)
# Before attempts 2 and 3, so a load burst can subside.
RETRY_DELAYS = (60, 300)
MAX_QUEUED = 8


class Observations:
    def __init__(self):
        self.breaks = deque(maxlen=32)
        self.previous_normal = None
        self.break_count = 0
        self.reset('watcher_start')

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

    def record(self, frames, taskmgr_pid, watcher_pid):
        return {'schema_version': 2, 'qpc_frequency': frequency(),
                'taskmgr_pid': taskmgr_pid, 'watcher_pid': watcher_pid,
                'clock_basis': 'watcher sample receipt; not native fault timing',
                'continuity': {'break_count':self.break_count,'recent_breaks':list(self.breaks),
                               'previous_normal_disconnected':self.previous_normal,
                               'limit':'Disconnected normal is context only, never a control or native fault boundary.'},
                'markers': {'last_normal': self.last_normal, 'first_candidate': self.first_candidate,
                            'confirmed': self.confirmed, 'recovery_started': None, 'recovery_finished': None},
                'samples': [{'qpc': f['qpc'], 'utc': f['timestamp'], **f['result']} for f in frames]}


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
        self.state = {'status': 'idle'}

    def status(self):
        with self.lock:
            return dict(self.state)

    def submit(self, folder, ready):
        folder = Path(folder)
        with self.lock:
            if self.closing or len(self.queue) >= MAX_QUEUED:
                save(folder/'analysis-status.json', {'status': 'deferred',
                     'reason': 'watcher closing' if self.closing else 'analysis queue full'})
                return False
            self.queue.append({'folder': folder, 'ready': ready, 'attempt': 1, 'due': 0, 'history': []})
            if self.thread and self.thread.is_alive():
                self.lock.notify()
            else:
                self.state = {'status': 'queued', 'folder': str(folder)}
                self.thread = threading.Thread(target=self._loop, daemon=False)
                self.thread.start()
            return True

    def close(self):
        """Stop waiting for retries; a running attempt finishes within its timeout."""
        with self.lock:
            self.closing = True
            self.lock.notify_all()

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

    def _run(self, item):
        folder, attempt = item['folder'], item['attempt']
        result = {'status': 'failed', 'attempt': attempt}
        started = time.monotonic()
        retryable = False
        try:
            if attempt == 1 and not item['ready'].wait(30):
                raise Retryable('snapshot still pending; offline analysis can be retried later')
            capture = json.loads((folder/'trace-capture.json').read_text(encoding='utf-8'))
            if capture.get('status') != 'preserved':
                raise RuntimeError(capture.get('error', 'no preserved ETW snapshot'))
            root = Path(__file__).resolve().parents[1]
            executable = Path(sys.executable)
            if executable.name.lower() == 'pythonw.exe':
                executable = executable.with_name('python.exe')
            command = [str(executable), str(root/'tools/process-incident.py'), str(folder)]
            output, log = folder, folder/'analysis.log'
            if attempt > 1:
                # The first attempt may leave a decode in place; reanalyse a copy.
                output = retry_output(folder, attempt)
                command += ['--output', str(output)]
                log = folder/f'analysis-attempt{attempt}.log'
                result['output'] = str(output)
            with log.open('wb') as stream:
                child = subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT,
                                       timeout=self.timeouts[attempt-1],
                                       creationflags=subprocess.CREATE_NO_WINDOW)
            retryable = child.returncode not in (0, 2)
            result.update(status='complete' if child.returncode == 0 else 'incomplete',
                          exit_code=child.returncode, report=str(output/'incident-analysis.md'))
        except (Retryable, subprocess.TimeoutExpired) as exc:
            retryable = True
            result['error'] = repr(exc)
        except Exception as exc:
            result['error'] = repr(exc)
        result['elapsed_seconds'] = round(time.monotonic()-started, 3)
        item['history'].append(result)
        if retryable and attempt < len(self.timeouts):
            delay = self.delays[attempt-1]
            status = {'status': 'retry_pending', 'next_attempt': attempt+1,
                      'retry_in_seconds': delay, 'attempts': item['history']}
            with self.lock:
                if not self.closing:
                    self.queue.append({**item, 'attempt': attempt+1, 'due': time.monotonic()+delay})
                else:
                    status['status'] = 'retry_cancelled'
        else:
            status = {**result, 'attempts': item['history']}
        save(folder/'analysis-status.json', status)
        with self.lock:
            self.state = {**status, 'folder': str(folder)}


def retry_output(folder, attempt):
    """A new sibling directory; reanalysis must never write inside the evidence."""
    base = folder.parent/f'analysis-retry{attempt}-{folder.name}'
    candidate, n = base, 1
    while candidate.exists():
        n += 1
        candidate = base.with_name(f'{base.name}-{n}')
    return candidate
