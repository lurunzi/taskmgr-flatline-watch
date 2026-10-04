"""Capture the first wil report of the aggregation error, before the stack unwinds.

With a debugger attached, TaskManagerDataLayer's wil::details::LogFailure sends
each failure report through OutputDebugStringW. ProcDump stops on the first
debug string containing the HRESULT and writes one full dump from a PSS clone;
the reporting thread is still inside OutputDebugStringW with its callers intact.

ProcDump is a debugger here: force-killing it also terminates the target.
Always detach with `procdump -cancel <pid>`.

With query_trace enabled, cdb replaces ProcDump for each instance (see
QueryTrace.py): one debugger per process, so never both on the same PID.
"""
import glob
import json
import subprocess
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

import QueryTrace

FILTER = '*8007139F*'
SYMBOLS = r'srv*C:\tmp\TaskmgrCpuDiag-20260928-194418\Symbols*https://msdl.microsoft.com/download/symbols'
CDB_GLOB = r'C:\Program Files\WindowsApps\Microsoft.WinDbg_*_x64__8wekyb3d8bbwe\amd64\cdb.exe'
STACK_COMMANDS = '.lastevent; kn 80; ~*kn 40; lmv m TaskManagerDataLayer; q'

def timestamp():
    return datetime.now().astimezone().isoformat()

CDB_KNOWN = (r'C:\Program Files\WindowsApps\Microsoft.WinDbg_1.2606.22001.0_x64__8wekyb3d8bbwe\amd64\cdb.exe',
             r'C:\Program Files (x86)\Windows Kits\10\Debuggers\x64\cdb.exe')

def find_cdb():
    # WindowsApps cannot be listed by ordinary users, so try exact paths first.
    for path in CDB_KNOWN:
        if Path(path).exists():
            return path
    found = sorted(glob.glob(CDB_GLOB))
    return found[-1] if found else None

class OriginWatch:
    def __init__(self, procdump, output, validate, write_json, query_trace=False):
        self.procdump, self.output = procdump, output
        self.validate, self.write_json = validate, write_json
        self.query_trace = query_trace
        self.sessions = {}
        self.attempted = set()
        self.lock = threading.Lock()

    def new_folder(self, pid, suffix=''):
        folder = self.output/f"origin-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{pid}{suffix}"
        folder.mkdir()
        return folder

    def attach(self, pid):
        """Monitor one Taskmgr instance once; a later instance gets its own session."""
        with self.lock:
            if pid in self.attempted or not self.procdump.exists():
                return False
            self.attempted.add(pid)
        fallback = None
        if self.query_trace:
            fallback = QueryTrace.unavailable(find_cdb())
            if fallback is None:
                session = QueryTrace.Session(find_cdb(), pid, self.new_folder(pid, '-trace'), self.write_json, self.trace_finished)
                self.sessions[pid] = session
                session.start()
                return True
        return self.start_procdump(pid, fallback)

    def trace_finished(self, session):
        record, folder = session.record, session.folder
        # After pause/resume a newer session may already own this PID.
        if self.sessions.get(session.pid) is session:
            self.sessions.pop(session.pid)
        dumps = sorted(folder.glob('*.dmp'))
        if dumps:
            record['dump'] = str(dumps[0])
            try:
                record['validation'] = self.validate(dumps[0])
                record['status'] = 'captured'
            except Exception as exc:
                record.update(status='dump_invalid', error=str(exc))
            self.write_json(folder/'origin.json', record)
            if record['status'] == 'captured':
                self.extract_stacks(dumps[0], folder, record)
        elif record['status'] == 'module_mismatch' and not record.get('cancelled'):
            self.start_procdump(session.pid, 'query_trace_module_mismatch')

    def start_procdump(self, pid, trace_fallback=None):
        folder = self.new_folder(pid)
        record = {'classification':'origin_error_monitor', 'taskmgr_pid':pid, 'filter':FILTER,
                  'started':timestamp(), 'status':'monitoring', 'debug_strings':0}
        if trace_fallback:
            record['query_trace_unavailable'] = trace_fallback
        self.write_json(folder/'origin.json', record)
        process = subprocess.Popen([str(self.procdump), '-accepteula', '-ma', '-r', '-n', '1', '-e', '-l',
            '-f', FILTER, str(pid), str(folder)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW)
        record['procdump_pid'] = process.pid
        thread = threading.Thread(target=self.follow, args=(pid, process, folder, record), daemon=True)
        self.sessions[pid] = (process, folder, record)
        thread.start()
        return True

    def follow(self, pid, process, folder, record):
        tail = deque(maxlen=200)
        pending = b''
        # ProcDump mixes an ASCII banner with UTF-16LE messages; drop NULs.
        for chunk in iter(lambda: process.stdout.read1(4096), b''):
            pending += chunk.replace(b'\x00', b'')
            *lines, pending = pending.split(b'\n')
            for line in lines:
                text = line.decode('utf-8', errors='replace').rstrip('\r')
                tail.append(text)
                if 'Debug String:' in text:
                    record['debug_strings'] += 1
        process.wait()
        self.sessions.pop(pid, None)
        record.update(procdump_exit=process.returncode, finished=timestamp())
        (folder/'procdump.txt').write_text('\n'.join(tail), encoding='utf-8')
        dumps = sorted(folder.glob('*.dmp'))
        if not dumps:
            record['status'] = 'cancelled' if record.get('cancelled') else 'no_match'
            self.write_json(folder/'origin.json', record)
            return
        record['dump'] = str(dumps[0])
        try:
            record['validation'] = self.validate(dumps[0])
            record['status'] = 'captured'
        except Exception as exc:
            record.update(status='dump_invalid', error=str(exc))
        self.write_json(folder/'origin.json', record)
        if record['status'] == 'captured':
            self.extract_stacks(dumps[0], folder, record)

    def extract_stacks(self, dump, folder, record):
        cdb = find_cdb()
        if not cdb:
            record['stacks'] = 'cdb_not_found'
        else:
            try:
                with (folder/'stacks.txt').open('w', encoding='utf-8', errors='replace') as stream:
                    completed = subprocess.run([cdb, '-z', str(dump), '-y', SYMBOLS, '-c', STACK_COMMANDS],
                        stdout=stream, stderr=subprocess.STDOUT, timeout=900,
                        creationflags=subprocess.CREATE_NO_WINDOW | subprocess.BELOW_NORMAL_PRIORITY_CLASS)
                record['stacks'] = 'written' if completed.returncode == 0 else f'cdb_exit_{completed.returncode}'
            except Exception as exc:
                record['stacks'] = f'failed: {exc}'
        self.write_json(folder/'origin.json', record)

    def cancel_all(self):
        """Detach gracefully; never terminate ProcDump or cdb (that could kill Taskmgr)."""
        traces = []
        for pid, session in list(self.sessions.items()):
            self.attempted.discard(pid)
            if isinstance(session, QueryTrace.Session):
                session.record['cancelled'] = True
                traces.append(threading.Thread(target=session.detach, daemon=True))
                traces[-1].start()
                continue
            process, folder, record = session
            record['cancelled'] = True
            subprocess.run([str(self.procdump), '-cancel', str(pid)], capture_output=True, timeout=30,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        for thread in traces:
            thread.join(timeout=25)
        deadline = time.monotonic()+15
        while self.sessions and time.monotonic() < deadline:
            time.sleep(.2)
