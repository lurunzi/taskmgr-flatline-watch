"""Owned, bounded ETW process/thread session using inbox Microsoft tools.

No global kernel/WPR session, persistent collector, command lines or stacks.
The decoder uses raw QPC timestamps: tracerpt's rendered timezone is unreliable
on the validation workstation. Query markers are host receipt times, not exact
target execution times; correlation alone is never attribution.
"""
import ctypes
import gzip
import json
import os
import re
import shutil
import struct
import subprocess
import time
import uuid
from pathlib import Path

PROVIDER = 'Microsoft-Windows-Kernel-Process'
GUID = '{22fb2cd6-0e7b-422b-a0c7-2fad1fd0e716}'
TASKMGR_PROVIDER = 'Microsoft.Windows.Taskmgr'
TASKMGR_GUID = '{2e635d8e-1107-4555-9319-32eeb895aaae}'
LIMIT_MB = 128
XML_LIMIT = 256*1024**2
COMPACT_XML_LIMIT = 1024*1024**2
DIRECT_DECODE_JSON_LIMIT = 512*1024**2
DIRECT_DECODE_EVENT_LIMIT = 2_000_000


def session_exists(name):
    """Read-only query of our exact UUID session; errors are never absence.

    EVENT_TRACE_PROPERTIES/ControlTraceW layouts follow the Windows SDK.
    Control code 0 is QUERY; this function cannot stop or modify a session.
    """
    if not re.fullmatch(r'TaskmgrCorrelation-[0-9a-f]{32}',name or ''):
        raise ValueError('unrecognised ETW session identity')
    ulong = ctypes.c_uint32
    class Wnode(ctypes.Structure):
        _fields_ = [('BufferSize',ulong),('ProviderId',ulong),
                    ('HistoricalContext',ctypes.c_uint64),('TimeStamp',ctypes.c_uint64),
                    ('Guid',ctypes.c_byte*16),('ClientContext',ulong),('Flags',ulong)]
    class Properties(ctypes.Structure):
        _fields_ = [('Wnode',Wnode)]+[(n,ulong) for n in
                    ('BufferSize','MinimumBuffers','MaximumBuffers','MaximumFileSize',
                     'LogFileMode','FlushTimer','EnableFlags','AgeLimit','NumberOfBuffers',
                     'FreeBuffers','EventsLost','BuffersWritten','LogBuffersLost','RealTimeBuffersLost')]+[
                    ('LoggerThreadId',ctypes.c_void_p),('LogFileNameOffset',ulong),('LoggerNameOffset',ulong)]
    buffer = ctypes.create_string_buffer(ctypes.sizeof(Properties)+4096)
    props = Properties.from_buffer(buffer)
    props.Wnode.BufferSize = len(buffer)
    props.LoggerNameOffset = ctypes.sizeof(Properties)
    props.LogFileNameOffset = ctypes.sizeof(Properties)+2048
    query = ctypes.WinDLL('advapi32',use_last_error=True).ControlTraceW
    query.argtypes = [ctypes.c_uint64,ctypes.c_wchar_p,ctypes.c_void_p,ulong]
    query.restype = ulong
    status = query(0,name,buffer,0)
    if status==4201:  # ERROR_WMI_INSTANCE_NOT_FOUND
        return False
    if status:
        raise OSError(status,'ETW session query failed; ownership unresolved')
    return True


def boot_time():
    command = "(Get-CimInstance Win32_OperatingSystem).LastBootUpTime.ToUniversalTime().ToString('o')"
    result = subprocess.run(['powershell.exe','-NoProfile','-Command',command],capture_output=True,
                            timeout=10,creationflags=subprocess.CREATE_NO_WINDOW,check=True)
    return result.stdout.decode('ascii').strip()


def qpc():
    value = ctypes.c_int64()
    if not ctypes.windll.kernel32.QueryPerformanceCounter(ctypes.byref(value)):
        raise ctypes.WinError()
    return value.value


def frequency():
    value = ctypes.c_int64()
    if not ctypes.windll.kernel32.QueryPerformanceFrequency(ctypes.byref(value)):
        raise ctypes.WinError()
    return value.value


def decode_etl_file(etl_path, timeout=90, event_limit=DIRECT_DECODE_EVENT_LIMIT, session_record=None):
    """Direct ETW file consumer via OpenTraceW + ProcessTrace (PROCESS_TRACE_MODE_RAW_TIMESTAMP).

    Extracts Microsoft-Windows-Kernel-Process events (1, 2, 3, 4) and Taskmgr FallbackError
    events (0x8007139F), discarding high-frequency Taskmgr noise without generating full XML.
    Returns (header, events, errors) on success, or None if Win32 ETW cannot open the file.
    """
    try:
        import RealtimeTrace as RT
    except ImportError:
        return None

    if not Path(etl_path).exists():
        return None

    deadline = time.monotonic() + timeout
    timed_out = [False]
    event_limit_exceeded = [False]
    events = []
    native_events = []
    header_box = [None]
    first_clock = [None]
    last_clock = [None]
    scanned_count = [0]

    def on_event(record_ptr):
        if time.monotonic() > deadline:
            timed_out[0] = True
            return
        scanned_count[0] += 1
        if scanned_count[0] > event_limit:
            event_limit_exceeded[0] = True
            return

        record = record_ptr.contents
        hdr = record.EventHeader
        prov = str(hdr.ProviderId).lower()
        stamp = int(hdr.TimeStamp)

        # 1. Trace Header Event
        if prov in ('{68fdd900-4a3e-11d1-84f4-0000f80464e3}', '{9e814aad-3204-11d2-9a82-006008a86939}') and not header_box[0]:
            u_len = record.UserDataLength
            if u_len >= 288 and record.UserData:
                raw = bytes((ctypes.c_char * u_len).from_address(record.UserData))
                buf_size, = struct.unpack('<I', raw[0:4])
                end_time, = struct.unpack('<q', raw[16:24])
                max_file_size, = struct.unpack('<I', raw[28:32])
                buffers_written, = struct.unpack('<I', raw[36:40])
                events_lost, = struct.unpack('<I', raw[48:52])
                boot_time, = struct.unpack('<q', raw[248:256])
                perf_freq, = struct.unpack('<q', raw[256:264])
                start_time, = struct.unpack('<q', raw[264:272])
                reserved_flags, = struct.unpack('<I', raw[272:276])
                buffers_lost, = struct.unpack('<I', raw[276:280])
                header_box[0] = {
                    'PerfFreq': perf_freq,
                    'StartTime': start_time,
                    'EndTime': end_time,
                    'EventsLost': events_lost,
                    'BuffersLost': buffers_lost,
                    'BuffersWritten': buffers_written,
                    'BufferSize': buf_size,
                    'MaxFileSize': max_file_size,
                    'raw_start': stamp,
                    'clock_type': reserved_flags
                }
            return

        # 2. Taskmgr Error Events (Filter: only 0x8007139F from TaskManagerDataLayer.dll)
        if prov == RT.TASKMGR_GUID.lower():
            u_len = record.UserDataLength
            if u_len and record.UserData:
                raw = bytes((ctypes.c_char * u_len).from_address(record.UserData))
                if RT.SIGNATURE_HRESULT_BYTES in raw and RT.SIGNATURE_MODULE_BYTES in raw:
                    native_events.append({
                        'raw': stamp,
                        'system_time': None,
                        'task': 'FallbackError',
                        'pid': hdr.ProcessId,
                        'tid': hdr.ThreadId,
                        'hresult': 2147947423,
                        'module': 'TaskManagerDataLayer.dll',
                        'file': '',
                        'line': 0,
                        'failure_type': 2,
                        'message': ''
                    })
            return

        # 3. Kernel-Process Events
        if prov == '{22fb2cd6-0e7b-422b-a0c7-2fad1fd0e716}':
            eid = hdr.EventDescriptor.Id
            u_len = record.UserDataLength
            if not u_len or not record.UserData:
                return
            raw = bytes((ctypes.c_char * u_len).from_address(record.UserData))

            first_clock[0] = stamp if first_clock[0] is None else min(first_clock[0], stamp)
            last_clock[0] = stamp if last_clock[0] is None else max(last_clock[0], stamp)

            if eid == 1:
                pid, seq, create_time, ppid = struct.unpack('<I4xQQI', raw[:28])
                image = None
                if len(raw) > 60:
                    rem = raw[60:]
                    idx = rem.find(b'\x00\x00')
                    if idx != -1:
                        image = rem[:idx + (idx % 2)].decode('utf-16le', errors='replace')
                events.append({
                    'qpc': stamp,
                    'kind': 'process_start',
                    'pid': pid,
                    'tid': hdr.ThreadId,
                    'parent_pid': ppid,
                    'create_time': create_time,
                    'image': image,
                    'sequence': seq
                })
            elif eid == 2:
                pid, seq, create_time, exit_time = struct.unpack('<I4xQQQ', raw[:32])
                s = raw.rstrip(b'\x00')
                last_null = s.rfind(b'\x00')
                image = s[last_null+1:].decode('utf-8', errors='replace') if last_null != -1 else None
                events.append({
                    'qpc': stamp,
                    'kind': 'process_stop',
                    'pid': pid,
                    'tid': hdr.ThreadId,
                    'parent_pid': 0,
                    'create_time': create_time,
                    'image': image,
                    'sequence': seq
                })
            elif eid == 3:
                pid, tid = struct.unpack('<II', raw[:8])
                events.append({
                    'qpc': stamp,
                    'kind': 'thread_start',
                    'pid': pid,
                    'tid': tid,
                    'parent_pid': 0,
                    'create_time': None,
                    'image': None,
                    'sequence': None
                })
            elif eid == 4:
                pid, tid = struct.unpack('<II', raw[:8])
                events.append({
                    'qpc': stamp,
                    'kind': 'thread_stop',
                    'pid': pid,
                    'tid': tid,
                    'parent_pid': 0,
                    'create_time': None,
                    'image': None,
                    'sequence': None
                })

    cb = RT.PEVENT_RECORD_CALLBACK(on_event)
    logfile = RT.EVENT_TRACE_LOGFILEW()
    logfile.LogFileName = str(etl_path)
    logfile.mode_union.ProcessTraceMode = 0x10000000 | 0x00001000
    logfile.callback_union.EventRecordCallback = cb

    hTrace = RT.advapi32.OpenTraceW(ctypes.byref(logfile))
    if hTrace == 0 or hTrace == 0xFFFFFFFFFFFFFFFF:
        return None

    try:
        handle_arr = (ctypes.c_uint64 * 1)(hTrace)
        RT.advapi32.ProcessTrace(handle_arr, 1, None, None)
    finally:
        RT.advapi32.CloseTrace(hTrace)

    if timed_out[0]:
        raise RuntimeError('ETW decoder bound exceeded; ETL retained')
    if event_limit_exceeded[0]:
        raise RuntimeError('event count bound exceeded; raw ETL retained')

    header = header_box[0]
    if not header:
        freq = (session_record.get('qpc_frequency') if session_record else None) or 10_000_000
        raw_start = (session_record.get('started_qpc') if session_record else None) or first_clock[0] or 0
        header = {
            'PerfFreq': freq,
            'StartTime': 0,
            'EndTime': 0,
            'EventsLost': 0,
            'BuffersLost': 0,
            'BuffersWritten': 1,
            'BufferSize': 65536,
            'MaxFileSize': 128,
            'raw_start': raw_start,
            'clock_type': 1
        }

    errors = []
    if header.get('EventsLost') or header.get('BuffersLost'):
        errors.append('ETW lost events or buffers')
    if header.get('BuffersWritten', 0) * header.get('BufferSize', 0) >= header.get('MaxFileSize', 0) * 1024**2:
        errors.append('circular trace may have overwritten earlier buffers')

    events.sort(key=lambda e: e['qpc'])
    header.update(
        retained_first_qpc=first_clock[0],
        retained_last_qpc=last_clock[0],
        scanned_events=scanned_count[0],
        selected_events=len(events),
        native_events=native_events
    )

    return header, events, errors


class Session:
    @classmethod
    def open_stopped(cls, folder):
        """Open retained evidence for offline decoding, never start an ETW session."""
        obj = cls.__new__(cls)
        obj.folder = Path(folder)/'process-trace'
        obj.record = json.loads((obj.folder/'session.json').read_text(encoding='utf-8'))
        if obj.record.get('status') != 'stopped':
            raise ValueError('ETW stop not verified; retain raw files without decoding')
        obj.name = obj.record['session']
        obj.system = Path(os.environ.get('SystemRoot', r'C:\Windows'))/'System32'
        obj.active = False
        return obj

    def __init__(self, folder, command_timeout=30, baseline_timeout=30):
        self.folder = Path(folder)/'process-trace'
        self.folder.mkdir()
        self.name = 'TaskmgrCorrelation-'+uuid.uuid4().hex
        self.system = Path(os.environ.get('SystemRoot',r'C:\Windows'))/'System32'
        self.active = False
        self.command_timeout = command_timeout
        self.baseline_timeout = baseline_timeout
        self.record = {'session':self.name,'provider':PROVIDER,'keywords':'0x30',
                       'taskmgr_provider':TASKMGR_PROVIDER,'taskmgr_guid':TASKMGR_GUID,
                       'providers':[
                           {'name':PROVIDER,'guid':GUID,'keywords':'0x30','level':4},
                           {'name':TASKMGR_PROVIDER,'guid':TASKMGR_GUID,'keywords':'0x0','level':2}
                       ],
                       'max_etl_mb':LIMIT_MB,'clock':'raw QPC','qpc_frequency':frequency(),
                       'attribution':'unproven','status':'preflight'}

    def save(self, **values):
        self.record.update(values)
        (self.folder/'session.json').write_text(json.dumps(self.record,indent=2)+'\n',encoding='utf-8')

    def command(self, args, label):
        result = subprocess.run(args,capture_output=True,timeout=self.command_timeout,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        (self.folder/(label+'.log')).write_bytes(result.stdout+result.stderr)
        if result.returncode:
            raise RuntimeError(f'{label} exit {result.returncode}; see process-trace/{label}.log')

    def start(self):
        self.save(start_requested_qpc=qpc())
        # A timed-out logman may still have started our uniquely named session.
        # Keep ownership set so the runner attempts cleanup even on that path.
        self.active = True
        providers_file = self.folder/'providers.guid'
        providers_file.write_text(f'{GUID} 0x30 4\n{TASKMGR_GUID} 0x0 2\n', encoding='ascii')
        self.command([str(self.system/'logman.exe'),'create','trace',self.name,
                      '-o',str(self.folder/'process.etl'),'-pf',str(providers_file),
                      '-f','bincirc','-max',str(LIMIT_MB),'-bs','64','-nb','16','64',
                      '-ct','perf','-ets'],'start')
        self.save(status='running',started_qpc=qpc())
        self.baseline()

    def baseline(self, final=False):
        # Baseline identifies pre-existing processes. Creation time prevents PID
        # reuse; missing paths remain unknown. Never collect command-line secrets.
        command = "[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); " \
            "@(Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,ExecutablePath,Name," \
            "@{n='CreateTime';e={$_.CreationDate.ToUniversalTime().ToString('o')}}) | ConvertTo-Json -Depth 3"
        prefix = 'end_baseline' if final else 'baseline'
        self.save(**{prefix+'_begin_qpc':qpc()})
        result = subprocess.run(['powershell.exe','-NoProfile','-Command',command],
                                capture_output=True,timeout=self.baseline_timeout,creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode:
            raise RuntimeError('process baseline failed')
        data = json.loads(result.stdout.decode('utf-8-sig'))
        name = 'end-baseline.json' if final else 'baseline.json'
        (self.folder/name).write_text(json.dumps(data,indent=2)+'\n',encoding='utf-8')
        self.save(**{prefix+'_end_qpc':qpc()})

    def stop(self):
        if self.active:
            for attempt in range(3):
                try:
                    self.command([str(self.system/'logman.exe'),'stop',self.name,'-ets'],f'stop-{attempt+1}')
                    self.active = False
                    self.save(status='stopped',stopped_qpc=qpc())
                    return
                except (RuntimeError, subprocess.TimeoutExpired) as exc:
                    self.save(status='stop_pending',stop_error=repr(exc),stop_attempts=attempt+1)
                    if attempt==2:
                        raise
                    time.sleep(.5*(attempt+1))

    def decode(self, compact=False, timeout=90):
        if self.active:
            raise RuntimeError('refusing to decode an active session')
        # The light recorder can span hours. XML expands far beyond ETL size;
        # prefer streaming ETW consumer reading process.etl directly into JSON,
        # retaining lossless events without bloating into unconstrained XML.
        json_output = self.folder/('events.json.gz' if compact else 'events.json')
        output = self.folder/('events.decode.xml' if compact else 'events.xml')
        limit = COMPACT_XML_LIMIT if compact else XML_LIMIT
        compressed = self.folder/'events.xml.gz'
        if compact and (output.exists() or compressed.exists() or json_output.exists()):
            raise RuntimeError('decode output exists; reanalyse into a new directory')
        if compact and shutil.disk_usage(self.folder).free < limit+512*1024**2:
            raise RuntimeError('insufficient free space for bounded offline decoding')

        decode_started = time.monotonic()
        deadline = decode_started+timeout

        # Attempt 1: Direct Win32 ETW file consumer
        etl_file = self.folder/'process.etl'
        if etl_file.exists():
            try:
                direct_res = decode_etl_file(etl_file, timeout=timeout, session_record=self.record)
                if direct_res is not None:
                    header, evts, errs = direct_res
                    payload = json.dumps({'header': header, 'events': evts, 'errors': errs})
                    if len(payload) > DIRECT_DECODE_JSON_LIMIT:
                        decode_dur = round(time.monotonic() - decode_started, 3)
                        self.save(decode='incomplete: decoder time/output bound', decode_duration_seconds=decode_dur)
                        raise RuntimeError('ETW decoder bound exceeded; ETL retained')
                    if compact:
                        part = self.folder/'events.json.gz.part'
                        with gzip.open(part, 'wb', compresslevel=1) as stream:
                            stream.write(payload.encode('utf-8'))
                        part.replace(json_output)
                        out_target = json_output
                    else:
                        json_output.write_text(payload, encoding='utf-8')
                        out_target = json_output
                    decode_dur = round(time.monotonic() - decode_started, 3)
                    self.save(decode='complete', decode_exit=0, decode_file=out_target.name,
                              decode_bytes=out_target.stat().st_size,
                              decode_uncompressed_bytes=len(payload),
                              decode_duration_seconds=decode_dur)
                    return
            except Exception as exc:
                if 'bound exceeded' in str(exc):
                    self.save(decode='incomplete: decoder time/output bound')
                    raise

        # Attempt 2: Fallback to tracerpt for synthetic test fixtures / mock environments
        self.save(decode='running', decode_exit=None, decode_file=None,
                  decode_limit_bytes=limit, decode_limit_seconds=timeout)
        with (self.folder/'decode.log').open('wb') as log:
            child = subprocess.Popen([str(self.system/'tracerpt.exe'),str(self.folder/'process.etl'),
                                      '-o',str(output),'-of','XML','-rts','-y'],
                                     stdout=log,stderr=subprocess.STDOUT,
                                     creationflags=subprocess.CREATE_NO_WINDOW)
            while child.poll() is None:
                if time.monotonic()>deadline or (output.exists() and output.stat().st_size>limit):
                    child.terminate()  # Only our offline decoder, never a debugger.
                    child.wait()
                    decode_dur = round(time.monotonic()-decode_started, 3)
                    self.save(decode='incomplete: decoder time/output bound', decode_duration_seconds=decode_dur)
                    raise RuntimeError('ETW decoder bound exceeded; ETL retained')
                time.sleep(.2)
        decode_dur = round(time.monotonic()-decode_started, 3)
        self.save(decode_duration_seconds=decode_dur)
        if child.returncode:
            self.save(decode='failed', decode_exit=child.returncode)
            raise RuntimeError('ETW decode failed; ETL retained')
        if time.monotonic()>deadline or output.stat().st_size>limit:
            self.save(decode='incomplete: decoder time/output bound')
            raise RuntimeError('ETW decoder bound exceeded; ETL retained')
        if compact:
            compress_started = time.monotonic()
            partial = self.folder/'events.xml.gz.part'
            self.save(decode_uncompressed_bytes=output.stat().st_size)
            try:
                with output.open('rb') as source, gzip.open(partial,'xb',compresslevel=1) as target:
                    while block := source.read(1024*1024):
                        if time.monotonic()>deadline:
                            raise RuntimeError('ETW compression deadline exceeded')
                        target.write(block)
                partial.replace(compressed)
                # Only our newly created staging output; original ETL is retained.
                output.unlink()
                output = compressed
                compress_dur = round(time.monotonic()-compress_started, 3)
                self.save(compress_duration_seconds=compress_dur)
            except Exception as exc:
                self.save(decode='incomplete: compression', decode_error=repr(exc))
                raise
        self.save(decode='complete', decode_exit=0, decode_file=output.name,
                  decode_bytes=output.stat().st_size)
