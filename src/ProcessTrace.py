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
        # retain lossless gzip, with a bounded staging file and the same deadline.
        output = self.folder/('events.decode.xml' if compact else 'events.xml')
        limit = COMPACT_XML_LIMIT if compact else XML_LIMIT
        compressed = self.folder/'events.xml.gz'
        if compact and (output.exists() or compressed.exists()):
            raise RuntimeError('decode output exists; reanalyse into a new directory')
        if compact and shutil.disk_usage(self.folder).free < limit+512*1024**2:
            raise RuntimeError('insufficient free space for bounded offline decoding')
        decode_started = time.monotonic()
        deadline = decode_started+timeout
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
