"""Trace Task Manager's process query up to the aggregation error, in the same process.

QueryProcessInformation retries NtQuerySystemInformation three times. After three
length mismatches its retry counter is 4, not 3, so the check at RVA 0x46325 falls
through to the success return and publishes the partial buffer. This mode records
each query's NtQSI statuses and lengths, walks the tail of every exhausted buffer,
and writes one full dump at the first 0x8007139F aggregation report.

Build-specific: offsets are valid only for TaskManagerDataLayer.dll with DLL_SHA256
(.local/native-query-20261004/RESULTS.md). On a hash or in-memory byte mismatch no
breakpoint is set and the caller falls back to the ProcDump origin monitor.

cdb is the only debugger attached. Logging breakpoints resume at once, but they
widen the gap between NtQSI calls, so counts are not natural failure rates. cdb
runs with -pd and is never terminated: detach clears breakpoints, resumes, then
quits with `qd`. Each command group ends in an echoed marker; DebugBreakProcess is
issued only when that marker does not come back, i.e. cdb is not reading input.
A break-in that lands after detach would crash Task Manager.
"""
import ctypes
import hashlib
import json
import os
import re
import subprocess
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

DLL = Path(os.environ.get('SystemRoot', r'C:\Windows'))/'System32/TaskManagerDataLayer.dll'
DLL_SHA256 = '829263f58610a9ff013b41f32ecad6a21e9de1631074039de6c20548d3515dce'
# Local symbol cache only; tracing must never wait on the network.
SYMBOLS = r'srv*C:\tmp\TaskmgrCpuDiag-20260928-194418\Symbols'
LOG_LIMIT = 50*1024**2
WRITE_INTERVAL = 30
STUCK_SECONDS = 3
PROMPT = re.compile(rb'\d+:\d+> $')
# Commands read from a pipe are not echoed, so their output follows the prompts
# on the same line: "0:004> 0:004> QT G".
PROMPTS = re.compile(r'^(?:\d+:\d+> )+')
MARKER = re.compile(r'^QT (\w+)(?: (.*))?$')

# One command per line: .dump consumes the rest of its line. Script paths use
# forward slashes because cdb unescapes backslashes inside quoted commands.
INIT = '''.echo QT ATTACHED
sxn av
sxn eh
.if ((qwo(TaskManagerDataLayer+0x46158)==0x74894808245c8948) & (dwo(TaskManagerDataLayer+0x46325)==0x03fe8341) & (wo(TaskManagerDataLayer+0x46329)==0x1b75)) {{ .echo QT VERIFIED }} .else {{ .echo QT MISMATCH; qd }}
bp TaskManagerDataLayer+0x46158 "$$><{d}/enter.cdb"
bp TaskManagerDataLayer+0x461ec "$$><{d}/ntqsi.cdb"
bp TaskManagerDataLayer+0x46325 "$$><{d}/exit.cdb"
bp TaskManagerDataLayer+0x4638c "$$><{d}/return.cdb"
sxe -c "$$<{d}/aggregation.cdb" out:*8007139F*
g
'''
SCRIPTS = {
    'enter.cdb': '.printf "QT E %x\\n", @$tid; gc\n',
    # After the call ebx still holds the provided length; [rsp+20] is ReturnLength.
    'ntqsi.cdb': '.printf "QT N %x %x %x %x\\n", @$tid, @eax, @ebx, dwo(@rsp+0x20); gc\n',
    # rcx/rbx are the buffer begin/end. With r14d==4 the partial buffer is about to
    # be published: walk NextEntryOffset (bounded) and report the final record.
    'exit.cdb': '.printf "QT X %x %x %p %p\\n", @$tid, @r14d, @rcx, @rbx; '
                '.if (@r14d==4) { r $t0=@rcx; r $t1=1; '
                '.while ((dwo(@$t0)!=0) & (@$t0+dwo(@$t0)+0x100 <= @rbx) & (@$t1 < 0x100000)) '
                '{ r $t0=@$t0+dwo(@$t0); r $t1=@$t1+1 }; '
                '.printf "QT T %x %x %x %I64x %I64x\\n", @$tid, @$t1, @$t0-@rcx, qwo(@$t0+0x50), qwo(@$t0+0x20) }; gc\n',
    'return.cdb': '.printf "QT R %x %x\\n", @$tid, @eax; gc\n',
}
# sxi out first: later 0x8007139F reports (e.g. Taskmgr's own downstream
# aggregation) must not rerun this script while detaching.
AGGREGATION = '''.echo QT A
sxi out
bc *
kn 40
.dump /ma {folder}/Taskmgr-origin.dmp
.echo QT D
'''

def timestamp():
    return datetime.now().astimezone().isoformat()

_dll_cache = {}

def dll_matches():
    stat = DLL.stat()
    key = (stat.st_size, stat.st_mtime_ns)
    if key not in _dll_cache:
        _dll_cache.clear()
        _dll_cache[key] = hashlib.sha256(DLL.read_bytes()).hexdigest() == DLL_SHA256
    return _dll_cache[key]

def unavailable(cdb):
    """Return why tracing cannot run, or None."""
    if not cdb:
        return 'cdb_not_found'
    try:
        if not dll_matches():
            return 'dll_hash_mismatch'
    except OSError as exc:
        return f'dll_unreadable: {exc}'
    return None

def write_scripts(folder):
    path = folder.resolve().as_posix()
    if any(c in path for c in ' "\'`;'):
        raise ValueError(f'cdb script path must not contain spaces or quotes: {path}')
    scripts = folder/'cdb'
    scripts.mkdir(exist_ok=True)
    for name, text in SCRIPTS.items():
        (scripts/name).write_text(text, encoding='ascii')
    (scripts/'aggregation.cdb').write_text(AGGREGATION.format(folder=path), encoding='ascii')
    init = scripts/'init.cdb'
    init.write_text(INIT.format(d=f'{path}/cdb'), encoding='ascii')
    return init

def classify(invocation):
    calls = invocation['calls']
    if invocation.get('r14') == 4:
        return 'exhausted'
    if len(calls) == 3 and calls[-1]['status'] == 0:
        return 'third_success'
    return 'normal' if invocation.get('hr') == 0 else 'error'

def break_in(pid):
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_bool, ctypes.c_uint32]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.DebugBreakProcess.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.OpenProcess(0x1F0FFF, False, pid)
    if not handle:
        return False
    try:
        return bool(kernel.DebugBreakProcess(handle))
    finally:
        kernel.CloseHandle(handle)

class Session:
    ACK_FAST = .5
    # Long enough for a full Taskmgr dump already in progress to finish.
    ACK_SLOW = 120

    def __init__(self, cdb, pid, folder, write_json, finished):
        self.cdb, self.pid, self.folder = cdb, pid, folder
        self.write_json, self.finished = write_json, finished
        self.record = {'classification':'query_trace_monitor', 'taskmgr_pid':pid, 'started':timestamp(),
                       'status':'attaching', 'dll_sha256':DLL_SHA256, 'callback_changes_timing':True,
                       'query_counts':{}, 'events':0}
        self.process = None
        self.open = {}
        self.tail = deque(maxlen=400)
        self.lock = threading.Lock()
        self.save_lock = threading.Lock()
        self.at_prompt = False
        self.prompt_since = None
        self.stopping = False
        self.markers = {'G':threading.Event(), 'Q':threading.Event()}
        self.log_bytes = 0
        self.last_write = 0

    def start(self):
        init = write_scripts(self.folder)
        self.save()
        self.process = subprocess.Popen([self.cdb, '-pd', '-p', str(self.pid), '-y', SYMBOLS, '-cf', str(init)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW)
        self.record['cdb_pid'] = self.process.pid
        threading.Thread(target=self.follow, daemon=True).start()
        threading.Thread(target=self.watchdog, daemon=True).start()

    def save(self):
        with self.save_lock:
            self.last_write = time.monotonic()
            self.write_json(self.folder/'origin.json', self.record)

    def follow(self):
        pending = b''
        for chunk in iter(lambda: self.process.stdout.read1(4096), b''):
            pending += chunk
            *lines, pending = pending.split(b'\n')
            for line in lines:
                self.handle_line(line.decode('utf-8', errors='replace').rstrip('\r'))
            with self.lock:
                prompt = bool(PROMPT.search(pending))
                if prompt and not self.at_prompt:
                    self.prompt_since = time.monotonic()
                self.at_prompt = prompt
        self.process.wait()
        self.record.update(cdb_exit=self.process.returncode, finished=timestamp())
        if self.record['status'] in ('attaching', 'tracing', 'aggregation_detected'):
            self.record['status'] = 'cancelled' if self.record.get('cancelled') else 'ended'
        (self.folder/'cdb-tail.txt').write_text('\n'.join(self.tail), encoding='utf-8')
        self.save()
        self.finished(self)

    def handle_line(self, text):
        with self.lock:
            self.at_prompt = False
        match = MARKER.match(PROMPTS.sub('', text))
        if not match:
            # The debug string arrives at once; cdb flushes the script's own
            # output (QT A) only after the dump, seconds later. Echoed commands
            # start with a prompt and are not reports.
            if '8007139F' in text and not PROMPTS.match(text) and 'aggregation_report_at' not in self.record:
                self.record.update(aggregation_report_at=timestamp(), aggregation_report=text)
            self.tail.append(text)
            self.log(text)
            return
        kind, rest = match.group(1), (match.group(2) or '').split()
        if kind in ('E', 'N', 'X', 'T', 'R'):
            self.query_event(kind, [int(v, 16) for v in rest])
            return
        if kind in self.markers:
            self.markers[kind].set()
        self.tail.append(text)
        self.log(text)
        if kind == 'VERIFIED':
            self.record['status'] = 'tracing'
        elif kind == 'MISMATCH':
            self.record['status'] = 'module_mismatch'
        elif kind == 'A':
            self.record.update(status='aggregation_detected',
                               aggregation_at=self.record.get('aggregation_report_at', timestamp()))
        elif kind == 'D':
            self.record['dump_written_at'] = timestamp()
            threading.Thread(target=self.detach, daemon=True).start()
        self.save()

    def query_event(self, kind, values):
        tid = values[0]
        if kind == 'E':
            self.open[tid] = {'started':timestamp(), 'tid':tid, 'calls':[]}
            return
        invocation = self.open.setdefault(tid, {'started':timestamp(), 'tid':tid, 'calls':[], 'partial':True})
        if kind == 'N':
            invocation['calls'].append({'status':values[1], 'provided':values[2], 'required':values[3]})
        elif kind == 'X':
            invocation.update(r14=values[1], buffer=[hex(values[2]), hex(values[3])])
        elif kind == 'T':
            invocation['tail'] = {'records':values[1], 'offset':values[2], 'pid':values[3], 'create_time':values[4]}
        elif kind == 'R':
            self.open.pop(tid, None)
            invocation['hr'] = values[1]
            self.finish_invocation(invocation)

    def finish_invocation(self, invocation):
        kind = classify(invocation)
        tail = invocation.get('tail')
        ghost = bool(tail and tail['pid'] == 0 and tail['create_time'] == 0)
        key = kind + (';ghost' if ghost else '')
        counts = self.record['query_counts']
        counts[key] = counts.get(key, 0)+1
        if kind != 'normal':
            self.record['events'] += 1
            event = {'time':timestamp(), 'kind':kind, 'ghost':ghost, **invocation,
                     'hr':f"0x{invocation['hr']:08x}",
                     'calls':[{**c, 'status':f"0x{c['status']:08x}"} for c in invocation['calls']]}
            with (self.folder/'query-trace.jsonl').open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(event)+'\n')
        if time.monotonic()-self.last_write > WRITE_INTERVAL:
            self.save()

    def log(self, text):
        if self.log_bytes > LOG_LIMIT:
            return
        line = f'{timestamp()} {text}\n'
        self.log_bytes += len(line)
        with (self.folder/'cdb.log').open('a', encoding='utf-8') as stream:
            stream.write(line if self.log_bytes <= LOG_LIMIT else '[cdb.log size limit reached]\n')

    def watchdog(self):
        """An unexpected stop would freeze Task Manager: record it and detach."""
        while self.process.poll() is None and not self.stopping:
            time.sleep(.5)
            with self.lock:
                stuck = self.at_prompt and time.monotonic()-self.prompt_since >= STUCK_SECONDS
            if stuck and not self.stopping:
                self.record.update(unexpected_break=timestamp(), unexpected_context=list(self.tail)[-30:])
                self.save()
                self.detach()

    def send(self, text):
        try:
            self.process.stdin.write(text.encode('ascii'))
            self.process.stdin.flush()
            return True
        except OSError:
            return False

    def handshake(self, commands, marker):
        """Send commands ending in `.echo QT <marker>`; break in only if cdb is not reading.

        A prompt left in the output buffer goes stale once `g` resumes the target, so
        the echoed marker, not the prompt text, proves cdb consumed the commands.
        """
        event = self.markers[marker]
        event.clear()
        if not self.send(commands):
            return False
        if event.wait(self.ACK_FAST):
            return True
        if self.process.poll() is None:
            break_in(self.pid)
        return event.wait(self.ACK_SLOW)

    def detach(self, timeout=20):
        with self.lock:
            if self.stopping:
                return
            self.stopping = True
        if self.process.poll() is not None:
            return
        # Clear breakpoints and run briefly so no breakpoint event is still queued
        # when the debugger leaves; then quit-detach. Stage two only follows a
        # confirmed stage one: a second break-in still queued behind an unread
        # stage one could land after detach and crash Task Manager.
        if not self.handshake('bc *\nsxi out\n.echo QT G\ng\n', 'G'):
            if self.process.poll() is None:
                self.record['detach'] = 'stalled before clearing breakpoints; cdb left attached, never terminated'
                self.save()
            return
        time.sleep(1)
        if self.process.poll() is None:
            self.handshake('.echo QT Q\nqd\n', 'Q')
        try:
            self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.record['detach'] = 'timeout; cdb left running, never terminated'
            self.save()
