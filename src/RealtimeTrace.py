"""Real-time ETW consumer for Task Manager native error events and data layer heartbeat (N3, N4).

Consumes provider Microsoft.Windows.Taskmgr {2e635d8e-1107-4555-9319-32eeb895aaae}
via Win32 real-time ETW API. Filters specifically for:
- hresult=0x8007139F and module=TaskManagerDataLayer.dll on the target Taskmgr PID (Level 2 Error)
- CurrentUpdateCycle (UpdateStarted/UpdateFinalized) heartbeat (Level 5 Verbose)
Excludes high-volume NetworkUtilizationRefresh events using EVENT_FILTER_TYPE_EVENT_NAME.
"""
import ctypes
from collections import deque
from ctypes import wintypes
import os
import re
import struct
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from ProcessTrace import qpc, frequency, session_exists

TASKMGR_GUID = '{2e635d8e-1107-4555-9319-32eeb895aaae}'
SIGNATURE_HRESULT_BYTES = b'\x9f\x13\x07\x80'  # 0x8007139F in little-endian
SIGNATURE_MODULE_BYTES = b'TaskManagerDataLayer.dll'
DEFAULT_SESSION_NAME = 'TaskmgrRealtime-Live'
EVENT_FILTER_TYPE_EVENT_NAME = 0x80000400

advapi32 = ctypes.WinDLL('advapi32', use_last_error=True)

class GUID(ctypes.Structure):
    _fields_ = [
        ('Data1', wintypes.ULONG),
        ('Data2', wintypes.USHORT),
        ('Data3', wintypes.USHORT),
        ('Data4', wintypes.BYTE * 8)
    ]
    def __str__(self):
        d4_1 = ''.join(f'{b:02x}' for b in self.Data4[:2])
        d4_2 = ''.join(f'{b:02x}' for b in self.Data4[2:])
        return f'{{{self.Data1:08x}-{self.Data2:04x}-{self.Data3:04x}-{d4_1}-{d4_2}}}'

def parse_guid(guid_str):
    guid_str = guid_str.strip('{}')
    parts = guid_str.split('-')
    g = GUID()
    g.Data1 = int(parts[0], 16)
    g.Data2 = int(parts[1], 16)
    g.Data3 = int(parts[2], 16)
    d4_bytes = bytes.fromhex(parts[3] + parts[4])
    for i in range(8):
        g.Data4[i] = d4_bytes[i]
    return g

class WNODE_HEADER(ctypes.Structure):
    _fields_ = [
        ('BufferSize', wintypes.ULONG),
        ('ProviderId', wintypes.ULONG),
        ('HistoricalContext', ctypes.c_uint64),
        ('TimeStamp', ctypes.c_int64),
        ('Guid', GUID),
        ('ClientContext', wintypes.ULONG),
        ('Flags', wintypes.ULONG)
    ]

class EVENT_TRACE_PROPERTIES(ctypes.Structure):
    _fields_ = [
        ('Wnode', WNODE_HEADER),
        ('BufferSize', wintypes.ULONG),
        ('MinimumBuffers', wintypes.ULONG),
        ('MaximumBuffers', wintypes.ULONG),
        ('MaximumFileSize', wintypes.ULONG),
        ('LogFileMode', wintypes.ULONG),
        ('FlushTimer', wintypes.ULONG),
        ('EnableFlags', wintypes.ULONG),
        ('AgeLimit', wintypes.LONG),
        ('NumberOfBuffers', wintypes.ULONG),
        ('FreeBuffers', wintypes.ULONG),
        ('EventsLost', wintypes.ULONG),
        ('BuffersWritten', wintypes.ULONG),
        ('LogBuffersLost', wintypes.ULONG),
        ('RealTimeBuffersLost', wintypes.ULONG),
        ('LoggerThreadId', wintypes.HANDLE),
        ('LogFileNameOffset', wintypes.ULONG),
        ('LoggerNameOffset', wintypes.ULONG),
    ]

class EVENT_FILTER_DESCRIPTOR(ctypes.Structure):
    _fields_ = [
        ('Ptr', ctypes.c_uint64),
        ('Size', wintypes.ULONG),
        ('Type', wintypes.ULONG)
    ]

class ENABLE_TRACE_PARAMETERS(ctypes.Structure):
    _fields_ = [
        ('Version', wintypes.ULONG),
        ('EnableProperty', wintypes.ULONG),
        ('ControlFlags', wintypes.ULONG),
        ('SourceId', GUID),
        ('EnableFilterDesc', ctypes.POINTER(EVENT_FILTER_DESCRIPTOR)),
        ('FilterDescCount', wintypes.ULONG)
    ]

class EVENT_DESCRIPTOR(ctypes.Structure):
    _fields_ = [
        ('Id', wintypes.USHORT),
        ('Version', ctypes.c_ubyte),
        ('Channel', ctypes.c_ubyte),
        ('Level', ctypes.c_ubyte),
        ('Opcode', ctypes.c_ubyte),
        ('Task', wintypes.USHORT),
        ('Keyword', ctypes.c_uint64)
    ]

class EVENT_HEADER(ctypes.Structure):
    _fields_ = [
        ('Size', wintypes.USHORT),
        ('HeaderType', wintypes.USHORT),
        ('Flags', wintypes.USHORT),
        ('EventProperty', wintypes.USHORT),
        ('ThreadId', wintypes.ULONG),
        ('ProcessId', wintypes.ULONG),
        ('TimeStamp', wintypes.LARGE_INTEGER),
        ('ProviderId', GUID),
        ('EventDescriptor', EVENT_DESCRIPTOR),
        ('ProcessorTime', ctypes.c_uint64),
        ('ActivityId', GUID),
    ]

class ETW_BUFFER_CONTEXT(ctypes.Structure):
    _fields_ = [
        ('ProcessorNumber', ctypes.c_ubyte),
        ('Alignment', ctypes.c_ubyte),
        ('LoggerId', wintypes.USHORT),
    ]

class EVENT_RECORD(ctypes.Structure):
    _fields_ = [
        ('EventHeader', EVENT_HEADER),
        ('BufferContext', ETW_BUFFER_CONTEXT),
        ('ExtendedDataCount', wintypes.USHORT),
        ('UserDataLength', wintypes.USHORT),
        ('ExtendedData', ctypes.c_void_p),
        ('UserData', ctypes.c_void_p),
        ('UserContext', ctypes.c_void_p),
    ]

PEVENT_RECORD = ctypes.POINTER(EVENT_RECORD)
PEVENT_RECORD_CALLBACK = ctypes.WINFUNCTYPE(None, PEVENT_RECORD)

class _EVENT_TRACE_LOGFILE_CALLBACK_UNION(ctypes.Union):
    _fields_ = [
        ('EventCallback', ctypes.c_void_p),
        ('EventRecordCallback', PEVENT_RECORD_CALLBACK),
    ]

class _EVENT_TRACE_LOGFILE_MODE_UNION(ctypes.Union):
    _fields_ = [
        ('LogFileMode', wintypes.ULONG),
        ('ProcessTraceMode', wintypes.ULONG),
    ]

class EVENT_TRACE(ctypes.Structure):
    _fields_ = [('Dummy', ctypes.c_byte * 88)]

class _TRACE_LOGFILE_HEADER(ctypes.Structure):
    _fields_ = [('Dummy', ctypes.c_byte * 280)]

class EVENT_TRACE_LOGFILEW(ctypes.Structure):
    _fields_ = [
        ('LogFileName', wintypes.LPWSTR),
        ('LoggerName', wintypes.LPWSTR),
        ('CurrentTime', wintypes.LARGE_INTEGER),
        ('BuffersRead', wintypes.ULONG),
        ('mode_union', _EVENT_TRACE_LOGFILE_MODE_UNION),
        ('CurrentEvent', EVENT_TRACE),
        ('LogfileHeader', _TRACE_LOGFILE_HEADER),
        ('BufferCallback', ctypes.c_void_p),
        ('BufferSize', wintypes.ULONG),
        ('Filled', wintypes.ULONG),
        ('EventsLost', wintypes.ULONG),
        ('callback_union', _EVENT_TRACE_LOGFILE_CALLBACK_UNION),
        ('IsKernelTrace', wintypes.ULONG),
        ('Context', ctypes.c_void_p),
    ]

# Win32 API configurations
advapi32.StartTraceW.argtypes = [ctypes.POINTER(ctypes.c_uint64), wintypes.LPCWSTR, ctypes.c_void_p]
advapi32.StartTraceW.restype = wintypes.ULONG

advapi32.EnableTraceEx2.argtypes = [
    ctypes.c_uint64, ctypes.POINTER(GUID), wintypes.ULONG,
    ctypes.c_ubyte, ctypes.c_uint64, ctypes.c_uint64, wintypes.ULONG, ctypes.c_void_p
]
advapi32.EnableTraceEx2.restype = wintypes.ULONG

advapi32.ControlTraceW.argtypes = [ctypes.c_uint64, wintypes.LPCWSTR, ctypes.c_void_p, wintypes.ULONG]
advapi32.ControlTraceW.restype = wintypes.ULONG

advapi32.OpenTraceW.argtypes = [ctypes.POINTER(EVENT_TRACE_LOGFILEW)]
advapi32.OpenTraceW.restype = ctypes.c_uint64

advapi32.ProcessTrace.argtypes = [ctypes.POINTER(ctypes.c_uint64), wintypes.ULONG, ctypes.c_void_p, ctypes.c_void_p]
advapi32.ProcessTrace.restype = wintypes.DWORD

advapi32.CloseTrace.argtypes = [ctypes.c_uint64]
advapi32.CloseTrace.restype = wintypes.DWORD


def re_enable_session_provider(session_name, provider_guid_str=TASKMGR_GUID, level=2, keywords=0):
    """Idempotently re-enables a provider on a named ETW session via ControlTraceW QUERY handle.

    CRITICAL: Never modifies sessions outside TaskmgrRealtime-* or TaskmgrCorrelation-*.
    """
    if is_test_environment() or not session_name:
        return 0
    if not (re.fullmatch(r'TaskmgrRealtime-.*', session_name) or
            re.fullmatch(r'TaskmgrCorrelation-[0-9a-f]{32}', session_name)):
        return 87  # ERROR_INVALID_PARAMETER
    buf_size = ctypes.sizeof(EVENT_TRACE_PROPERTIES) + 1024
    buf = ctypes.create_string_buffer(buf_size)
    props = EVENT_TRACE_PROPERTIES.from_buffer(buf)
    props.Wnode.BufferSize = buf_size
    props.LoggerNameOffset = ctypes.sizeof(EVENT_TRACE_PROPERTIES)
    st = advapi32.ControlTraceW(0, session_name, buf, 0)
    if st != 0:
        return st
    handle = props.Wnode.HistoricalContext
    if not handle:
        return 6  # ERROR_INVALID_HANDLE
    pguid = parse_guid(provider_guid_str)
    return advapi32.EnableTraceEx2(handle, ctypes.byref(pguid), 1, level, keywords, 0, 0, None)


def stop_session_by_name(name):
    """Stop named ETW session safely via ControlTraceW, with logman fallback.

    CRITICAL: Never modifies or stops sessions outside TaskmgrRealtime-*.
    """
    if not re.fullmatch(r'TaskmgrRealtime-.*', name):
        return False
    buf_size = ctypes.sizeof(EVENT_TRACE_PROPERTIES) + 1024
    buf = ctypes.create_string_buffer(buf_size)
    props = EVENT_TRACE_PROPERTIES.from_buffer(buf)
    props.Wnode.BufferSize = buf_size
    props.LoggerNameOffset = ctypes.sizeof(EVENT_TRACE_PROPERTIES)
    status = advapi32.ControlTraceW(0, name, buf, 1)  # 1 = EVENT_TRACE_CONTROL_STOP
    if status == 0:
        return True
    try:
        res = subprocess.run(['logman.exe', 'stop', name, '-ets'],
                             capture_output=True, timeout=5,
                             creationflags=subprocess.CREATE_NO_WINDOW)
        return res.returncode == 0
    except Exception:
        return False


def clean_all_realtime_sessions():
    """Clean up any leaked or previous TaskmgrRealtime-* sessions.

    CRITICAL: Never touches TaskmgrWilProbe or TaskmgrCorrelation-*.
    """
    try:
        res = subprocess.run(['logman.exe', 'query', '-ets'],
                             capture_output=True, text=True, timeout=10,
                             creationflags=subprocess.CREATE_NO_WINDOW)
        if res.returncode == 0:
            for line in res.stdout.splitlines():
                parts = line.strip().split()
                if not parts:
                    continue
                name = parts[0]
                if re.fullmatch(r'TaskmgrRealtime-.*', name):
                    stop_session_by_name(name)
    except Exception:
        pass


def is_test_environment():
    if os.environ.get('TASKMGR_WATCH_TEST') == '1':
        return True
    if 'unittest' in sys.modules:
        return True
    return False


class RealtimeConsumer:
    """Manages real-time ETW session for Microsoft.Windows.Taskmgr Level 2 errors and Level 5 heartbeat.

    Filters specifically for:
    - Target Taskmgr PID
    - Provider {2e635d8e-1107-4555-9319-32eeb895aaae}
    - HRESULT 0x8007139F and module TaskManagerDataLayer.dll (Level 2)
    - CurrentUpdateCycle (UpdateStarted/UpdateFinalized) heartbeat (Level 5)
    Excludes high-volume NetworkUtilizationRefresh using EVENT_FILTER_TYPE_EVENT_NAME.
    """
    def __init__(self, target_pid=None, name=DEFAULT_SESSION_NAME, enabled=True):
        self.target_pid = target_pid
        self.name = name or DEFAULT_SESSION_NAME
        self.enabled = bool(enabled and not is_test_environment())
        self.session_handle = ctypes.c_uint64(0)
        self.consumer_handle = ctypes.c_uint64(0)
        self.thread = None
        self.lock = threading.Lock()
        self.active = False
        self.last_error = None
        self.pending_native_error = None
        self.heartbeat_events = deque(maxlen=3600)
        self.last_provider_event_utc = None
        self.last_provider_event_qpc = None
        self.last_provider_event_mono = None
        self.provider_event_count = 0
        self._filter_buf = None
        self.state = {'state': 'stopped' if self.enabled else 'disabled', 'session': self.name}
        self._callback_ref = None

    def set_target_pid(self, pid):
        with self.lock:
            if self.target_pid != pid:
                self.target_pid = pid
                self.pending_native_error = None
                self.heartbeat_events.clear()
                self.provider_event_count = 0
                if pid is not None:
                    now_mono = time.monotonic()
                    self.last_provider_event_mono = now_mono
                    self.last_provider_event_utc = datetime.now(timezone.utc).isoformat()
                    self.last_provider_event_qpc = qpc()
                else:
                    self.last_provider_event_mono = None
                    self.last_provider_event_utc = None
                    self.last_provider_event_qpc = None

    def record_provider_event(self, qpc_val=None, utc_str=None, mono_val=None):
        with self.lock:
            self.last_provider_event_mono = time.monotonic() if mono_val is None else mono_val
            self.last_provider_event_utc = datetime.now(timezone.utc).isoformat() if utc_str is None else utc_str
            self.last_provider_event_qpc = qpc() if qpc_val is None else qpc_val
            self.provider_event_count += 1

    def re_enable_provider(self):
        """Idempotently re-enables Microsoft.Windows.Taskmgr on this real-time session."""
        with self.lock:
            if not self.enabled or self.session_handle.value == 0:
                return 0
            provider_guid = parse_guid(TASKMGR_GUID)
            if self._filter_buf:
                filter_desc = EVENT_FILTER_DESCRIPTOR()
                filter_desc.Ptr = ctypes.cast(self._filter_buf, ctypes.c_void_p).value
                filter_desc.Size = len(self._filter_buf)
                filter_desc.Type = EVENT_FILTER_TYPE_EVENT_NAME

                params = ENABLE_TRACE_PARAMETERS()
                params.Version = 2
                params.EnableFilterDesc = ctypes.pointer(filter_desc)
                params.FilterDescCount = 1

                st = advapi32.EnableTraceEx2(self.session_handle, ctypes.byref(provider_guid), 1, 5, 0, 0, 0, ctypes.byref(params))
                if st != 0:
                    st = advapi32.EnableTraceEx2(self.session_handle, ctypes.byref(provider_guid), 1, 2, 0, 0, 0, None)
                return st
            else:
                return advapi32.EnableTraceEx2(self.session_handle, ctypes.byref(provider_guid), 1, 2, 0, 0, 0, None)

    def start(self):
        with self.lock:
            if not self.enabled:
                return
            if self.active:
                return
            clean_all_realtime_sessions()
            buf_size = ctypes.sizeof(EVENT_TRACE_PROPERTIES) + 1024
            buf = ctypes.create_string_buffer(buf_size)
            props = EVENT_TRACE_PROPERTIES.from_buffer(buf)
            props.Wnode.BufferSize = buf_size
            props.Wnode.Flags = 0x00020000  # WNODE_FLAG_TRACED_GUID
            props.Wnode.ClientContext = 1   # QPC clock
            props.LogFileMode = 0x00000100  # EVENT_TRACE_REAL_TIME_MODE
            props.LoggerNameOffset = ctypes.sizeof(EVENT_TRACE_PROPERTIES)

            st = advapi32.StartTraceW(ctypes.byref(self.session_handle), self.name, buf)
            if st != 0:
                self.last_error = f'StartTraceW error {st}'
                self.state = {'state': 'error', 'error': self.last_error, 'session': self.name}
                return

            provider_guid = parse_guid(TASKMGR_GUID)
            # Level 5 (Verbose) filtered specifically for CurrentUpdateCycle (heartbeat) and FallbackError (errors)
            # Excludes high-volume NetworkUtilizationRefresh events
            name_bytes = b'CurrentUpdateCycle\x00FallbackError\x00'
            header_bytes = struct.pack('<QQBBH', 0, 0, 5, 1, 2)
            filter_data = header_bytes + name_bytes
            self._filter_buf = ctypes.create_string_buffer(filter_data)

            filter_desc = EVENT_FILTER_DESCRIPTOR()
            filter_desc.Ptr = ctypes.cast(self._filter_buf, ctypes.c_void_p).value
            filter_desc.Size = len(filter_data)
            filter_desc.Type = EVENT_FILTER_TYPE_EVENT_NAME

            params = ENABLE_TRACE_PARAMETERS()
            params.Version = 2  # ENABLE_TRACE_PARAMETERS_VERSION_2
            params.EnableFilterDesc = ctypes.pointer(filter_desc)
            params.FilterDescCount = 1

            en_st = advapi32.EnableTraceEx2(self.session_handle, ctypes.byref(provider_guid), 1, 5, 0, 0, 0, ctypes.byref(params))
            if en_st != 0:
                # Fallback to level 2 without filter if name filter unsupported
                en_st = advapi32.EnableTraceEx2(self.session_handle, ctypes.byref(provider_guid), 1, 2, 0, 0, 0, None)
            if en_st != 0:
                self.stop()
                self.last_error = f'EnableTraceEx2 error {en_st}'
                self.state = {'state': 'error', 'error': self.last_error, 'session': self.name}
                return

            self._callback_ref = PEVENT_RECORD_CALLBACK(self._on_event)

            logfile = EVENT_TRACE_LOGFILEW()
            logfile.LoggerName = self.name
            logfile.mode_union.ProcessTraceMode = 0x00000100 | 0x10000000  # REAL_TIME | EVENT_RECORD
            logfile.callback_union.EventRecordCallback = self._callback_ref

            hConsumer = advapi32.OpenTraceW(ctypes.byref(logfile))
            if hConsumer == 0 or hConsumer == 0xFFFFFFFFFFFFFFFF:
                self.stop()
                self.last_error = 'OpenTraceW failed'
                self.state = {'state': 'error', 'error': self.last_error, 'session': self.name}
                return

            self.consumer_handle = ctypes.c_uint64(hConsumer)
            self.active = True
            self.state = {'state': 'running', 'session': self.name, 'target_pid': self.target_pid}
            self.thread = threading.Thread(target=self._run_consumer, name='realtime-etw', daemon=True)
            self.thread.start()

    def _on_event(self, record_ptr):
        try:
            record = record_ptr.contents if hasattr(record_ptr, 'contents') else record_ptr
            hdr = record.EventHeader
            pid = hdr.ProcessId
            with self.lock:
                current_target = self.target_pid

            if not current_target or pid != current_target:
                return

            provider_str = str(hdr.ProviderId).lower()
            if provider_str != TASKMGR_GUID.lower():
                return

            now_mono = time.monotonic()
            now_utc = datetime.now(timezone.utc).isoformat()
            now_qpc = qpc()
            self.record_provider_event(now_qpc, now_utc, now_mono)

            u_len = record.UserDataLength
            if not u_len or not record.UserData:
                return

            raw = bytes((ctypes.c_char * u_len).from_address(record.UserData))

            # 1. Check for 0x8007139F native freeze error
            if SIGNATURE_HRESULT_BYTES in raw and SIGNATURE_MODULE_BYTES in raw:
                event_info = {
                    'hresult': '0x8007139F',
                    'module': 'TaskManagerDataLayer.dll',
                    'taskmgr_pid': pid,
                    'thread_id': hdr.ThreadId,
                    'timestamp_utc': now_utc,
                    'timestamp_qpc': now_qpc,
                    'timestamp_monotonic': now_mono,
                }
                with self.lock:
                    if self.pending_native_error is None:
                        self.pending_native_error = event_info

            # 2. Check for CurrentUpdateCycle heartbeat (UpdateStarted / UpdateFinalized)
            if b'UpdateStarted' in raw or b'UpdateFinalized' in raw:
                try:
                    idx1 = raw.find(b'\x00')
                    layer = raw[:idx1].decode('utf-8', errors='ignore') if idx1 > 0 else 'DataModel'
                    idx2 = raw.find(b'\x00', idx1 + 1)
                    op = raw[idx1 + 1:idx2].decode('utf-8', errors='ignore') if idx2 > idx1 else (
                        'UpdateStarted' if b'UpdateStarted' in raw else 'UpdateFinalized')
                    tab = 1
                    if len(raw) >= idx2 + 9:
                        tab = struct.unpack('<Q', raw[idx2 + 1:idx2 + 9])[0]
                    idx3 = raw.find(b'\x00', idx2 + 9)
                    cid = raw[idx2 + 9:idx3].decode('utf-8', errors='ignore') if idx3 > idx2 + 9 else ''
                    hb_entry = {
                        'qpc': now_qpc,
                        'utc': now_utc,
                        'pid': pid,
                        'tid': hdr.ThreadId,
                        'layer': layer,
                        'operation': op,
                        'tab': tab,
                        'correlation_id': cid
                    }
                    with self.lock:
                        self.heartbeat_events.append(hb_entry)
                except Exception:
                    pass
        except Exception:
            pass

    def _run_consumer(self):
        try:
            handle_arr = (ctypes.c_uint64 * 1)(self.consumer_handle.value)
            advapi32.ProcessTrace(handle_arr, 1, None, None)
        except Exception:
            pass

    def poll_native_error(self):
        """Return pending native error if any; does not clear it."""
        with self.lock:
            return dict(self.pending_native_error) if self.pending_native_error else None

    def clear_native_error(self):
        with self.lock:
            err = self.pending_native_error
            self.pending_native_error = None
            return err

    def get_heartbeat_events(self):
        with self.lock:
            return list(self.heartbeat_events)

    def stop(self, timeout=3.0):
        with self.lock:
            if not self.enabled:
                return
            if not self.active and self.session_handle.value == 0:
                return
            self.active = False
            buf_size = ctypes.sizeof(EVENT_TRACE_PROPERTIES) + 1024
            buf = ctypes.create_string_buffer(buf_size)
            props = EVENT_TRACE_PROPERTIES.from_buffer(buf)
            props.Wnode.BufferSize = buf_size
            props.LoggerNameOffset = ctypes.sizeof(EVENT_TRACE_PROPERTIES)
            if self.session_handle.value:
                try:
                    advapi32.ControlTraceW(self.session_handle, None, buf, 1)
                except Exception:
                    pass
                self.session_handle = ctypes.c_uint64(0)
            if self.consumer_handle.value:
                try:
                    advapi32.CloseTrace(self.consumer_handle.value)
                except Exception:
                    pass
                self.consumer_handle = ctypes.c_uint64(0)
            self.state = {'state': 'stopped', 'session': self.name}

        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=timeout)
        clean_all_realtime_sessions()

    def status(self):
        with self.lock:
            res = dict(self.state)
            res['has_pending_error'] = bool(self.pending_native_error)
            res['target_pid'] = self.target_pid
            res['heartbeat_count'] = len(self.heartbeat_events)
            res['last_heartbeat_utc'] = self.heartbeat_events[-1]['utc'] if self.heartbeat_events else None
            res['provider_event_count'] = self.provider_event_count
            res['last_provider_event_utc'] = self.last_provider_event_utc
            res['last_provider_event_qpc'] = self.last_provider_event_qpc
            res['last_provider_event_mono'] = self.last_provider_event_mono
            return res
