"""Owned, bounded ETW process/thread session using inbox Microsoft tools.

No global kernel/WPR session, persistent collector, command lines or stacks.
The decoder uses raw QPC timestamps: tracerpt's rendered timezone is unreliable
on the validation workstation. Query markers are host receipt times, not exact
target execution times; correlation alone is never attribution.
"""
import ctypes
import json
import os
import subprocess
import time
import uuid
from pathlib import Path

PROVIDER = 'Microsoft-Windows-Kernel-Process'
GUID = '{22fb2cd6-0e7b-422b-a0c7-2fad1fd0e716}'
LIMIT_MB = 64


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
    def __init__(self, folder):
        self.folder = Path(folder)/'process-trace'
        self.folder.mkdir()
        self.name = 'TaskmgrCorrelation-'+uuid.uuid4().hex
        self.system = Path(os.environ.get('SystemRoot',r'C:\Windows'))/'System32'
        self.active = False
        self.record = {'session':self.name,'provider':PROVIDER,'keywords':'0x30',
                       'max_etl_mb':LIMIT_MB,'clock':'raw QPC','qpc_frequency':frequency(),
                       'attribution':'unproven','status':'preflight'}

    def save(self, **values):
        self.record.update(values)
        (self.folder/'session.json').write_text(json.dumps(self.record,indent=2)+'\n',encoding='utf-8')

    def command(self, args, label):
        result = subprocess.run(args,capture_output=True,timeout=30,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        (self.folder/(label+'.log')).write_bytes(result.stdout+result.stderr)
        if result.returncode:
            raise RuntimeError(f'{label} exit {result.returncode}; see process-trace/{label}.log')

    def start(self):
        self.save(start_requested_qpc=qpc())
        # A timed-out logman may still have started our uniquely named session.
        # Keep ownership set so the runner attempts cleanup even on that path.
        self.active = True
        self.command([str(self.system/'logman.exe'),'create','trace',self.name,
                      '-o',str(self.folder/'process.etl'),'-p',PROVIDER,'0x30','4',
                      '-f','bincirc','-max',str(LIMIT_MB),'-bs','64','-nb','16','64',
                      '-ct','perf','-ets'],'start')
        self.save(status='running',started_qpc=qpc())
        # Baseline identifies pre-existing processes. Creation time prevents PID
        # reuse; missing paths remain unknown. Never collect command-line secrets.
        command = "[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); " \
            "@(Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,ExecutablePath,Name," \
            "@{n='CreateTime';e={$_.CreationDate.ToUniversalTime().ToString('o')}}) | ConvertTo-Json -Depth 3"
        self.save(baseline_begin_qpc=qpc())
        result = subprocess.run(['powershell.exe','-NoProfile','-Command',command],
                                capture_output=True,timeout=30,creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode:
            raise RuntimeError('process baseline failed')
        data = json.loads(result.stdout.decode('utf-8-sig'))
        (self.folder/'baseline.json').write_text(json.dumps(data,indent=2)+'\n',encoding='utf-8')
        self.save(baseline_end_qpc=qpc())

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

    def decode(self):
        if self.active:
            raise RuntimeError('refusing to decode an active session')
        output = self.folder/'events.xml'
        with (self.folder/'decode.log').open('wb') as log:
            child = subprocess.Popen([str(self.system/'tracerpt.exe'),str(self.folder/'process.etl'),
                                      '-o',str(output),'-of','XML','-rts','-y'],
                                     stdout=log,stderr=subprocess.STDOUT,
                                     creationflags=subprocess.CREATE_NO_WINDOW)
            deadline = time.monotonic()+90
            while child.poll() is None:
                if time.monotonic()>deadline or (output.exists() and output.stat().st_size>256*1024**2):
                    child.terminate()  # Only our offline decoder, never a debugger.
                    child.wait()
                    self.save(decode='incomplete: decoder time/output bound')
                    raise RuntimeError('ETW decoder bound exceeded; ETL retained')
                time.sleep(.2)
        self.save(decode_exit=child.returncode)
        if child.returncode:
            raise RuntimeError('ETW decode failed; ETL retained')
