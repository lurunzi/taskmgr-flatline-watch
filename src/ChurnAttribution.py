"""Exact kernel buffer delta attribution for NtQuerySystemInformation expansion.

Measures the exact physical byte contributors (new processes, thread growth)
between consecutive SystemProcessInformation snapshots, quantifying whether
buffer expansions are driven by single dominant processes or diffused system entropy.
"""
import ctypes as C
import struct
import time
from typing import Dict, Any, List, Tuple

SYSTEM_PROCESS_INFORMATION = 5
THREAD_INFO_SIZE = 80
DEFAULT_BUFFER_SIZE = 8 * 1024 * 1024  # 8 MiB ensures no length mismatch during audit

ntdll = C.WinDLL('ntdll')
NtQuerySystemInformation = ntdll.NtQuerySystemInformation
NtQuerySystemInformation.argtypes = [C.c_ulong, C.c_void_p, C.c_ulong, C.POINTER(C.c_ulong)]
NtQuerySystemInformation.restype = C.c_long


def query_process_snapshot(buffer_size: int = DEFAULT_BUFFER_SIZE) -> Tuple[int, Dict[int, Dict[str, Any]], int]:
    """Query raw SystemProcessInformation and parse snapshot while buffer is in scope."""
    buf = (C.c_char * buffer_size)()
    retlen = C.c_ulong(0)
    status = NtQuerySystemInformation(SYSTEM_PROCESS_INFORMATION, buf, buffer_size, C.byref(retlen))
    if status != 0:
        return status, {}, retlen.value
    base_addr = C.addressof(buf)
    snapshot = parse_process_snapshot(bytes(buf[:min(retlen.value, buffer_size)]), base_addr=base_addr)
    return status, snapshot, retlen.value


def parse_process_snapshot(data: bytes, base_addr: int = None) -> Dict[int, Dict[str, Any]]:
    """Parse raw SystemProcessInformation bytes into a structured map of PID -> process info."""
    offset = 0
    snapshot = {}
    total_len = len(data)

    while offset + 0x100 <= total_len and len(snapshot) < 100000:
        step, num_threads = struct.unpack_from('<II', data, offset)
        create_time = struct.unpack_from('<Q', data, offset + 0x20)[0]
        name_len = struct.unpack_from('<H', data, offset + 0x38)[0]
        name_ptr = struct.unpack_from('<Q', data, offset + 0x40)[0]
        pid = struct.unpack_from('<Q', data, offset + 0x50)[0]

        name = ''
        if name_ptr and name_len > 0:
            if base_addr is not None and base_addr <= name_ptr < base_addr + total_len:
                name_off = name_ptr - base_addr
                try:
                    name = data[name_off:name_off + name_len].decode('utf-16le', errors='replace')
                except Exception:
                    name = 'unknown'
            else:
                try:
                    name = C.wstring_at(name_ptr, name_len // 2)
                except Exception:
                    name = 'unknown'
        elif pid == 0:
            name = 'System Idle Process'
        elif pid == 4:
            name = 'System'

        snapshot[pid] = {
            'pid': pid,
            'name': name or 'unknown',
            'threads': num_threads,
            'create_time': create_time,
            'step': step,
            'offset': offset,
        }

        if step == 0:
            break
        if step < 0x100:
            # Corrupted step offset; stop walk safely
            break
        offset += step

    return snapshot


def diff_snapshots(
    snap1: Dict[int, Dict[str, Any]],
    snap2: Dict[int, Dict[str, Any]],
    len1: int,
    len2: int
) -> Dict[str, Any]:
    """Compute exact byte delta attribution between two snapshots.

    Attributes positive byte growth to specific PIDs via:
    1. New processes: process header + thread structures + name alignment
    2. Existing processes: thread count delta * sizeof(SYSTEM_THREAD_INFORMATION) [80 bytes]
    """
    positive_contributors = []
    negative_contributors = []
    total_positive_bytes = 0

    # Check new processes and thread increases in snap2
    for pid, p2 in snap2.items():
        if pid not in snap1:
            # Entirely new process
            p_bytes = p2['step'] if p2['step'] > 0 else (0x100 + p2['threads'] * THREAD_INFO_SIZE)
            total_positive_bytes += p_bytes
            positive_contributors.append({
                'pid': pid,
                'name': p2['name'],
                'type': 'new_process',
                'thread_delta': p2['threads'],
                'bytes': p_bytes
            })
        else:
            p1 = snap1[pid]
            t_delta = p2['threads'] - p1['threads']
            if t_delta > 0:
                p_bytes = t_delta * THREAD_INFO_SIZE
                total_positive_bytes += p_bytes
                positive_contributors.append({
                    'pid': pid,
                    'name': p2['name'],
                    'type': 'thread_growth',
                    'thread_delta': t_delta,
                    'bytes': p_bytes
                })
            elif t_delta < 0:
                negative_contributors.append({
                    'pid': pid,
                    'name': p2['name'],
                    'type': 'thread_shrink',
                    'thread_delta': t_delta,
                    'bytes': t_delta * THREAD_INFO_SIZE
                })

    # Check exited processes
    for pid, p1 in snap1.items():
        if pid not in snap2:
            p_bytes = p1['step'] if p1['step'] > 0 else (0x100 + p1['threads'] * THREAD_INFO_SIZE)
            negative_contributors.append({
                'pid': pid,
                'name': p1['name'],
                'type': 'exited_process',
                'thread_delta': -p1['threads'],
                'bytes': -p_bytes
            })

    # Sort positive contributors by byte growth descending
    positive_contributors.sort(key=lambda x: x['bytes'], reverse=True)

    cr1 = 0.0
    cr3 = 0.0
    if total_positive_bytes > 0:
        for c in positive_contributors:
            c['share'] = round(c['bytes'] / total_positive_bytes, 4)
        cr1 = positive_contributors[0]['share']
        cr3 = sum(c['share'] for c in positive_contributors[:3])

    if cr1 >= 0.70:
        pattern = 'single_dominant_source'
    elif cr3 >= 0.70:
        pattern = 'few_clustered_sources'
    elif total_positive_bytes > 0:
        pattern = 'diffused_system_entropy'
    else:
        pattern = 'no_growth'

    return {
        'net_buffer_delta': len2 - len1,
        'positive_expansion_bytes': total_positive_bytes,
        'pattern': pattern,
        'cr1': round(cr1, 4),
        'cr3': round(cr3, 4),
        'positive_contributors_count': len(positive_contributors),
        'top_contributors': positive_contributors[:5],
    }


def sample_expansion_bursts(duration_seconds: float = 10.0, interval_ms: float = 20.0) -> Dict[str, Any]:
    """Continuously sample SystemProcessInformation, detecting and attributing every expansion burst."""
    end_time = time.monotonic() + duration_seconds
    delay = interval_ms / 1000.0

    status, prev_snap, prev_len = query_process_snapshot()
    if status != 0:
        raise RuntimeError(f"NtQuerySystemInformation failed: 0x{status & 0xffffffff:08x}")

    bursts = []
    image_totals: Dict[str, int] = {}
    total_samples = 0
    expansion_samples = 0

    while time.monotonic() < end_time:
        time.sleep(delay)
        total_samples += 1
        status, cur_snap, cur_len = query_process_snapshot()
        if status != 0:
            continue

        diff = diff_snapshots(prev_snap, cur_snap, prev_len, cur_len)
        if diff['positive_expansion_bytes'] > 0:
            expansion_samples += 1
            bursts.append({
                'timestamp_qpc': time.perf_counter_ns(),
                'diff': diff
            })
            for c in diff['top_contributors']:
                image_totals[c['name']] = image_totals.get(c['name'], 0) + c['bytes']

        prev_len, prev_snap = cur_len, cur_snap

    pattern_counts = {}
    for b in bursts:
        pat = b['diff']['pattern']
        pattern_counts[pat] = pattern_counts.get(pat, 0) + 1

    sorted_images = sorted(image_totals.items(), key=lambda x: x[1], reverse=True)

    return {
        'duration_seconds': duration_seconds,
        'total_samples': total_samples,
        'expansion_bursts_count': expansion_samples,
        'expansion_rate': round(expansion_samples / max(1, total_samples), 4),
        'patterns': pattern_counts,
        'top_accumulated_images': [{'name': img, 'total_positive_bytes': b} for img, b in sorted_images[:10]],
        'bursts_summary': [b['diff'] for b in bursts[:20]]
    }
