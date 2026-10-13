"""Background circular ETW, independent of any debugger or recovery thread."""
import json
import os
import queue
import shutil
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from ProcessTrace import Session, qpc

TRACE_BUDGET = 2 * 1024**3
MIN_FREE = 1024**3


def save(path, value):
    text = json.dumps(value, ensure_ascii=False, indent=2) + '\n'
    temp = path.with_suffix('.tmp')
    temp.write_text(text, encoding='utf-8')
    temp.replace(path)


class RollingRecorder:
    """One serial owner; no historic incident evidence is deleted automatically.

    snapshot() queues preservation and returns an Event, never a success promise.
    Callers may wait briefly then recover while this worker finishes saving.
    """
    def __init__(self, output, session_factory=Session):
        self.output = Path(output)
        self.factory = session_factory
        self.jobs = queue.Queue(maxsize=1)
        self.cancel = threading.Event()
        self.lock = threading.Lock()
        self.thread = None
        self.session = None
        self.state = {'state': 'disabled', 'attribution': 'unproven'}

    def status(self):
        with self.lock:
            return dict(self.state)

    def update(self, **values):
        with self.lock:
            self.state.update(values)

    def start(self):
        with self.lock:
            if self.thread and self.thread.is_alive():
                return
            # A failed stop still owns its session; do not start another one.
            if self.session and self.session.active:
                return
            self.cancel.clear()
            self.state.update(state='starting', error=None)
            self.thread = threading.Thread(target=self._run, name='rolling-etw', daemon=False)
            self.thread.start()

    def snapshot(self, folder):
        ready = threading.Event()
        folder = Path(folder)
        request = {'requested_qpc': qpc(), 'status': 'queued'}
        if (self.cancel.is_set() or not self.thread or not self.thread.is_alive() or
                self.status()['state'] not in ('starting','running','saving')):
            request.update(status='unavailable', error=self.status().get('error') or 'collector not running')
        else:
            try:
                self.jobs.put_nowait((folder, ready, request))
                return ready
            except queue.Full:
                request.update(status='unavailable', error='snapshot queue full')
        save(folder/'trace-capture.json', request)
        ready.set()
        return ready

    def stop(self, timeout=25):
        self.cancel.set()
        if self.thread:
            self.thread.join(timeout)
            if self.thread.is_alive():
                self.update(state='stop_pending', error='owned ETW cleanup still running')
        return self.status()

    def _storage(self):
        used = 0
        # Only this feature's known directories; old dumps are never scanned.
        for root in [self.output/'light-etw', *self.output.glob('restart-*')]:
            if root.exists():
                for pattern in ('*.etl', '*.xml'):
                    used += sum(p.stat().st_size for p in root.rglob(pattern) if p.is_file())
        if shutil.disk_usage(self.output).free < MIN_FREE or used >= TRACE_BUDGET:
            raise RuntimeError('ETW storage budget reached; existing evidence retained')

    def _arm(self):
        if self.cancel.is_set():
            return
        self._storage()
        root = self.output/'light-etw'/('run-'+uuid.uuid4().hex)
        root.mkdir(parents=True)
        self.session = self.factory(root, command_timeout=5, baseline_timeout=10)
        self.session.save(owner_pid=os.getpid(), owner_role='flatline watcher',
                          created_utc=datetime.now(timezone.utc).isoformat())
        self.session.start()
        self.update(state='running', session=self.session.name, folder=str(self.session.folder),
                    armed_qpc=qpc(), error=None)

    def _run(self):
        try:
            # An interrupted watcher can leave an ETW session behind. Refuse a
            # duplicate rather than guessing that an old controller is dead.
            for path in (self.output/'light-etw').glob('run-*/process-trace/session.json'):
                previous = json.loads(path.read_text(encoding='utf-8'))
                if previous.get('status') in ('running','stop_pending','preflight'):
                    raise RuntimeError(f'unresolved prior ETW session {previous.get("session")}; inspect {path}')
            self._arm()
            while not self.cancel.is_set():
                try:
                    folder, ready, record = self.jobs.get(timeout=.2)
                except queue.Empty:
                    continue
                try:
                    self.update(state='saving')
                    record['stop_requested_qpc'] = qpc()
                    self.session.stop()
                    record['stopped_qpc'] = qpc()
                    # Surviving long-lived parents can be identified after a
                    # circular overwrite from a fresh creation-time snapshot.
                    # Its absence degrades identity coverage, not raw ETL saving.
                    try:
                        self.session.baseline(final=True)
                    except Exception as exc:
                        self.session.save(end_baseline_error=repr(exc))
                        record['end_baseline_error'] = repr(exc)
                    destination = folder/'process-trace'
                    if destination.exists():
                        raise RuntimeError('incident trace destination already exists')
                    # Same output volume: rename preserves the original ETL bytes.
                    self.session.folder.rename(destination)
                    record.update(status='preserved', preserved_qpc=qpc(), trace=str(destination))
                    save(folder/'trace-capture.json', record)
                except Exception as exc:
                    record.update(status='failed', error=repr(exc))
                    save(folder/'trace-capture.json', record)
                    raise
                finally:
                    ready.set()
                    self.jobs.task_done()
                # A stopped recorder must never rearm after a late snapshot.
                self._arm()
                record['rearmed_qpc'] = None if self.cancel.is_set() else self.status().get('armed_qpc')
                record['gap_seconds'] = ((record['rearmed_qpc']-record['stopped_qpc']) /
                                         self.session.record['qpc_frequency']) if record['rearmed_qpc'] else None
                save(folder/'trace-capture.json', record)
        except Exception as exc:
            self.update(state='error', error=repr(exc))
        finally:
            if self.session and self.session.active:
                try:
                    self.session.stop()
                except Exception as exc:
                    self.update(state='stop_pending', error=repr(exc), session=self.session.name)
            while True:
                try:
                    folder, ready, record = self.jobs.get_nowait()
                except queue.Empty:
                    break
                try:
                    record.update(status='unavailable', error=self.status().get('error') or 'collector stopped')
                    save(folder/'trace-capture.json', record)
                finally:
                    ready.set()
                    self.jobs.task_done()
            if self.status()['state'] not in ('error', 'stop_pending'):
                self.update(state='stopped')
