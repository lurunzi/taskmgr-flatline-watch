#!/usr/bin/env python3
"""Provoked-reproduction experiment: alternate quiet and process-churn blocks.

The running watcher detects and recovers freezes as usual; this tool only
creates short-lived processes during "load" blocks, records the schedule, and
afterwards attributes each recovery folder to the block it fell in. It never
touches Task Manager, the watcher or any ETW session.

Stop early by creating the file STOP in the output folder, or press Ctrl+C.
"""
import argparse
import ctypes
import json
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

EVIDENCE = Path(r'D:\TaskmgrFreezeCaptures')
CREATE_NO_WINDOW = 0x08000000


def utc_now():
    return datetime.now(timezone.utc)


def qpc():
    value = ctypes.c_int64()
    ctypes.windll.kernel32.QueryPerformanceCounter(ctypes.byref(value))
    return value.value


def block_plan(blocks, block_minutes, first='quiet'):
    """Alternating schedule; returns [(index, kind, offset_s, length_s)]."""
    kinds = ('quiet', 'load') if first == 'quiet' else ('load', 'quiet')
    length = block_minutes * 60.0
    return [(i, kinds[i % 2], i * length, length) for i in range(blocks)]


def read_status(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def preflight(status):
    """Problems that make the experiment meaningless; empty list means ready."""
    problems = []
    if not status:
        problems.append('status.json unreadable')
        return problems
    if status.get('dump_running'):
        problems.append('watcher is capturing (dump_running)')
    if status.get('state') != 'normal':
        problems.append(f"watcher state is {status.get('state')!r}, not 'normal'")
    if status.get('native_coverage') not in (None, 'ok'):
        problems.append(f"native_coverage is {status.get('native_coverage')!r}")
    realtime = status.get('realtime_consumer') or {}
    if realtime and realtime.get('state') != 'running':
        problems.append(f"realtime consumer is {realtime.get('state')!r}")
    return problems


def folder_time(name):
    """UTC time encoded in restart-YYYYMMDD-HHMMSS-ffffff folder names (local time is GMT here)."""
    parts = name.split('-')
    if len(parts) < 4 or parts[0] != 'restart':
        return None
    try:
        stamp = datetime.strptime(parts[1] + parts[2], '%Y%m%d%H%M%S')
    except ValueError:
        return None
    return stamp.replace(tzinfo=timezone.utc)


def attribute(incidents, blocks, start):
    """Count incidents per block. incidents: [(name, utc_datetime, extra)]."""
    counts = {index: [] for index, *_ in blocks}
    outside = []
    for name, when, extra in incidents:
        offset = (when - start).total_seconds()
        for index, kind, block_offset, length in blocks:
            if block_offset <= offset < block_offset + length:
                counts[index].append({'incident': name, 'offset_s': round(offset, 1), **extra})
                break
        else:
            outside.append(name)
    return counts, outside


class Churn:
    def __init__(self, threads):
        self.threads = threads
        self.stop = threading.Event()
        self.started = 0
        self.failed = 0
        self.lock = threading.Lock()
        self.workers = []

    def _work(self):
        while not self.stop.is_set():
            try:
                subprocess.run(['cmd.exe', '/d', '/c', 'exit'], creationflags=CREATE_NO_WINDOW,
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=10)
                with self.lock:
                    self.started += 1
            except Exception:
                with self.lock:
                    self.failed += 1
                time.sleep(0.05)

    def start(self):
        self.stop.clear()
        self.workers = [threading.Thread(target=self._work, daemon=True) for _ in range(self.threads)]
        for worker in self.workers:
            worker.start()

    def halt(self):
        self.stop.set()
        for worker in self.workers:
            worker.join(15)
        self.workers = []


def collect_incidents(evidence, start, end):
    found = []
    for folder in sorted(evidence.glob('restart-*')):
        if folder.name.endswith(('-reanalysis', '-reanalysis2')) or '-retry' in folder.name:
            continue
        when = folder_time(folder.name)
        if not when or not (start <= when <= end):
            continue
        extra = {}
        try:
            event = json.loads((folder/'event.json').read_text(encoding='utf-8'))
            extra['trigger_kind'] = event.get('trigger_kind')
            extra['native_event_time'] = event.get('native_event_time')
        except (OSError, ValueError):
            pass
        found.append((folder.name, when, extra))
    return found


def run(args):
    evidence = Path(args.evidence)
    output = Path(args.output) if args.output else evidence/f"stress-{utc_now().strftime('%Y%m%d-%H%M%S')}"
    output.mkdir(parents=True, exist_ok=False)
    status_path = evidence/'status.json'
    blocks = block_plan(args.blocks, args.block_minutes, args.first)

    deadline = time.monotonic() + args.preflight_wait * 60
    while True:
        problems = preflight(read_status(status_path))
        if not problems:
            break
        if time.monotonic() >= deadline:
            receipt = {'status': 'aborted_preflight', 'problems': problems, 'finished_utc': utc_now().isoformat()}
            (output/'receipt.json').write_text(json.dumps(receipt, indent=2), encoding='utf-8')
            print(json.dumps(receipt, indent=2))
            return 2
        time.sleep(10)

    start = utc_now()
    start_qpc = qpc()
    t0 = time.monotonic()
    records, samples = [], []
    churn = Churn(args.threads)
    stopped_early = None
    try:
        for index, kind, offset, length in blocks:
            while time.monotonic() - t0 < offset:
                time.sleep(0.5)
            begin = utc_now()
            before = churn.started
            if kind == 'load':
                churn.start()
            block_end = t0 + offset + length
            while time.monotonic() < block_end:
                if (output/'STOP').exists():
                    stopped_early = 'STOP file'
                    break
                status = read_status(status_path)
                realtime = status.get('realtime_consumer') or {}
                samples.append({'utc': utc_now().isoformat(), 'block': index, 'state': status.get('state'),
                                'system_cpu_percent': status.get('system_cpu_percent'),
                                'native_coverage': status.get('native_coverage'),
                                'dump_running': status.get('dump_running'),
                                'provider_event_count': realtime.get('provider_event_count')})
                time.sleep(5)
            if kind == 'load':
                churn.halt()
            end = utc_now()
            started = churn.started - before
            records.append({'block': index, 'kind': kind, 'start_utc': begin.isoformat(), 'end_utc': end.isoformat(),
                            'seconds': round((end - begin).total_seconds(), 1), 'process_starts': started,
                            'process_starts_per_second': round(started / max((end - begin).total_seconds(), 1), 1)})
            print(f"block {index} {kind}: {records[-1]['seconds']} s, {started} process starts", flush=True)
            (output/'progress.json').write_text(json.dumps({'blocks': records}, indent=2), encoding='utf-8')
            if stopped_early:
                break
    except KeyboardInterrupt:
        stopped_early = 'Ctrl+C'
        churn.halt()
    finish = utc_now()

    # Give the watcher time to finish a recovery that started at the end.
    time.sleep(args.settle)
    incidents = collect_incidents(evidence, start, finish)
    actual = [(r['block'], r['kind'], (datetime.fromisoformat(r['start_utc']) - start).total_seconds(),
               r['seconds']) for r in records]
    counts, outside = attribute(incidents, actual, start)
    for record in records:
        record['incidents'] = counts.get(record['block'], [])
    totals = {}
    for kind in ('quiet', 'load'):
        chosen = [r for r in records if r['kind'] == kind]
        hours = sum(r['seconds'] for r in chosen) / 3600
        found = sum(len(r['incidents']) for r in chosen)
        totals[kind] = {'blocks': len(chosen), 'hours': round(hours, 3), 'incidents': found,
                        'incidents_per_hour': round(found / hours, 2) if hours else None,
                        'process_starts': sum(r['process_starts'] for r in chosen)}
    receipt = {'status': 'stopped_early' if stopped_early else 'complete', 'stopped_early': stopped_early,
               'tool': 'tools/churn-stress.py', 'start_utc': start.isoformat(), 'start_qpc': start_qpc,
               'finish_utc': finish.isoformat(), 'threads': args.threads, 'block_minutes': args.block_minutes,
               'blocks': records, 'totals': totals, 'incidents_outside_blocks': outside,
               'limits': 'Attribution by recovery-folder time; a freeze can start in one block and be confirmed in the next. '
                         'Load is cmd.exe process churn only. Temporal association, not proof of mechanism.'}
    (output/'receipt.json').write_text(json.dumps(receipt, indent=2), encoding='utf-8')
    (output/'samples.json').write_text(json.dumps(samples, indent=1), encoding='utf-8')
    print(json.dumps(totals, indent=2))
    print(f'receipt: {output/"receipt.json"}')
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--blocks', type=int, default=6, help='alternating blocks (default 6)')
    parser.add_argument('--block-minutes', type=float, default=10.0, help='minutes per block (default 10)')
    parser.add_argument('--threads', type=int, default=8, help='churn threads during load blocks (default 8)')
    parser.add_argument('--first', choices=('quiet', 'load'), default='quiet')
    parser.add_argument('--preflight-wait', type=float, default=10.0, help='minutes to wait for a healthy watcher')
    parser.add_argument('--settle', type=float, default=90.0, help='seconds to wait for a final recovery')
    parser.add_argument('--evidence', default=str(EVIDENCE))
    parser.add_argument('--output', help='new folder for the receipt (default evidence/stress-<time>)')
    return run(parser.parse_args(argv))


if __name__ == '__main__':
    sys.exit(main())
