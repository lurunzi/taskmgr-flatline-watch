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


class OfflineProcessor:
    """At most one decoder; busy incidents remain available for manual analysis."""
    def __init__(self):
        self.lock = threading.Lock()
        self.thread = None
        self.state = {'status': 'idle'}

    def status(self):
        with self.lock:
            return dict(self.state)

    def submit(self, folder, ready):
        with self.lock:
            if self.thread and self.thread.is_alive():
                save(Path(folder)/'analysis-status.json', {'status': 'deferred', 'reason': 'decoder busy'})
                return False
            self.state = {'status': 'queued', 'folder': str(folder)}
            self.thread = threading.Thread(target=self._run, args=(Path(folder), ready), daemon=False)
            self.thread.start()
            return True

    def _run(self, folder, ready):
        result = {'status': 'failed'}
        started = time.monotonic()
        try:
            if not ready.wait(30):
                raise RuntimeError('snapshot still pending; offline analysis can be retried later')
            capture = json.loads((folder/'trace-capture.json').read_text(encoding='utf-8'))
            if capture.get('status') != 'preserved':
                raise RuntimeError(capture.get('error', 'no preserved ETW snapshot'))
            root = Path(__file__).resolve().parents[1]
            executable = Path(sys.executable)
            if executable.name.lower() == 'pythonw.exe':
                executable = executable.with_name('python.exe')
            with (folder/'analysis.log').open('wb') as stream:
                child = subprocess.run([str(executable), str(root/'tools/process-incident.py'), str(folder)],
                                       stdout=stream, stderr=subprocess.STDOUT, timeout=120,
                                       creationflags=subprocess.CREATE_NO_WINDOW)
            result = {'status': 'complete' if child.returncode == 0 else 'incomplete',
                      'exit_code': child.returncode, 'report': str(folder/'incident-analysis.md')}
        except Exception as exc:
            result['error'] = repr(exc)
        result['elapsed_seconds'] = round(time.monotonic()-started, 3)
        save(folder/'analysis-status.json', result)
        with self.lock:
            self.state = {**result, 'folder': str(folder)}
