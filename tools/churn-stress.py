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
from datetime import datetime, timedelta, timezone
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


def native_time(value):
    if not value:
        return None
    try:
        when = datetime.fromisoformat(value)
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


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


class ThreadChurn:
    """Child processes that keep creating batches of short-lived threads.

    Each thread adds and removes a SYSTEM_THREAD_INFORMATION entry, so the
    process snapshot size changes far more often than with cmd.exe churn.
    Children stop when their stdin is closed and report their thread count.
    """
    def __init__(self, workers, batch):
        self.workers, self.batch = workers, batch
        self.children = []
        self.started = 0

    def start(self):
        self.children = [subprocess.Popen([sys.executable, '-X', 'utf8', str(Path(__file__).resolve()),
                                           '--thread-worker', str(self.batch)],
                                          stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                          stderr=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW)
                         for _ in range(self.workers)]

    def halt(self):
        for child in self.children:
            try:
                child.stdin.close()
            except OSError:
                pass
        for child in self.children:
            try:
                out, _ = child.communicate(timeout=30)
                self.started += json.loads(out.decode('utf-8').strip().splitlines()[-1]).get('thread_starts', 0)
            except Exception:
                pass
        self.children = []


def thread_worker(batch):
    import random
    stop = threading.Event()
    threading.Thread(target=lambda: (sys.stdin.read(), stop.set()), daemon=True).start()
    started = 0
    while not stop.is_set():
        batch_threads = [threading.Thread(target=time.sleep, args=(random.uniform(0.001, 0.02),))
                         for _ in range(batch)]
        for thread in batch_threads:
            thread.start()
        for thread in batch_threads:
            thread.join()
        started += batch
    print(json.dumps({'thread_starts': started}), flush=True)
    return 0


def collect_incidents(evidence, start, end):
    found = []
    for folder in sorted(evidence.glob('restart-*')):
        if folder.name.endswith(('-reanalysis', '-reanalysis2')) or '-retry' in folder.name:
            continue
        when = folder_time(folder.name)
        # Recovery can lag the native error by minutes while the watcher is blind.
        if not when or not (start <= when <= end + timedelta(minutes=30)):
            continue
        extra = {'recovery_utc': when.isoformat()}
        try:
            event = json.loads((folder/'event.json').read_text(encoding='utf-8'))
            extra['trigger_kind'] = event.get('trigger_kind')
            extra['native_event_time'] = event.get('native_event_time')
        except (OSError, ValueError):
            pass
        # A freeze belongs to the block where the native error occurred, not where
        # recovery happened; a blind watcher can recover minutes later.
        onset = native_time(extra.get('native_event_time'))
        if onset and start <= onset <= end:
            when = onset
        extra['attributed_by'] = 'native_event_time' if onset else 'recovery_folder_time'
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
    loads = []
    if args.load in ('cmd', 'mixed'):
        loads.append(Churn(args.threads))
    if args.load in ('threads', 'mixed'):
        loads.append(ThreadChurn(args.thread_workers, args.thread_batch))
    cmd_load = next((load for load in loads if isinstance(load, Churn)), None)
    thread_load = next((load for load in loads if isinstance(load, ThreadChurn)), None)
    stopped_early = None
    try:
        for index, kind, offset, length in blocks:
            while time.monotonic() - t0 < offset:
                time.sleep(0.5)
            begin = utc_now()
            before = cmd_load.started if cmd_load else 0
            threads_before = thread_load.started if thread_load else 0
            if kind == 'load':
                for load in loads:
                    load.start()
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
                for load in loads:
                    load.halt()
            end = utc_now()
            started = (cmd_load.started if cmd_load else 0) - before
            thread_starts = (thread_load.started if thread_load else 0) - threads_before
            seconds = max((end - begin).total_seconds(), 1)
            records.append({'block': index, 'kind': kind, 'load': args.load if kind == 'load' else None,
                            'start_utc': begin.isoformat(), 'end_utc': end.isoformat(),
                            'seconds': round((end - begin).total_seconds(), 1), 'process_starts': started,
                            'process_starts_per_second': round(started / seconds, 1),
                            'thread_starts': thread_starts, 'thread_starts_per_second': round(thread_starts / seconds, 1)})
            print(f"block {index} {kind}: {records[-1]['seconds']} s, {started} cmd starts, "
                  f"{thread_starts} thread starts", flush=True)
            (output/'progress.json').write_text(json.dumps({'blocks': records}, indent=2), encoding='utf-8')
            if stopped_early:
                break
    except KeyboardInterrupt:
        stopped_early = 'Ctrl+C'
        for load in loads:
            load.halt()
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
                        'process_starts': sum(r['process_starts'] for r in chosen),
                        'thread_starts': sum(r.get('thread_starts', 0) for r in chosen)}
    receipt = {'status': 'stopped_early' if stopped_early else 'complete', 'stopped_early': stopped_early,
               'tool': 'tools/churn-stress.py', 'start_utc': start.isoformat(), 'start_qpc': start_qpc,
               'finish_utc': finish.isoformat(), 'load': args.load, 'threads': args.threads,
               'thread_workers': args.thread_workers, 'thread_batch': args.thread_batch,
               'block_minutes': args.block_minutes,
               'blocks': records, 'totals': totals, 'incidents_outside_blocks': outside,
               'limits': 'Attribution by recovery-folder time; a freeze can start in one block and be confirmed in the next. '
                         'Load is synthetic cmd.exe and/or thread churn. Temporal association, not proof of mechanism.'}
    (output/'receipt.json').write_text(json.dumps(receipt, indent=2), encoding='utf-8')
    (output/'samples.json').write_text(json.dumps(samples, indent=1), encoding='utf-8')
    print(json.dumps(totals, indent=2))
    print(f'receipt: {output/"receipt.json"}')
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ['--thread-worker']:
        return thread_worker(int(argv[1]) if len(argv) > 1 else 64)
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--blocks', type=int, default=6, help='alternating blocks (default 6)')
    parser.add_argument('--block-minutes', type=float, default=10.0, help='minutes per block (default 10)')
    parser.add_argument('--load', choices=('cmd', 'threads', 'mixed'), default='cmd',
                        help='cmd.exe process churn, short-lived thread churn, or both')
    parser.add_argument('--threads', type=int, default=8, help='cmd churn threads during load blocks (default 8)')
    parser.add_argument('--thread-workers', type=int, default=4, help='thread-churn child processes (default 4)')
    parser.add_argument('--thread-batch', type=int, default=64, help='threads created per batch in each child')
    parser.add_argument('--first', choices=('quiet', 'load'), default='quiet')
    parser.add_argument('--preflight-wait', type=float, default=10.0, help='minutes to wait for a healthy watcher')
    parser.add_argument('--settle', type=float, default=90.0, help='seconds to wait for a final recovery')
    parser.add_argument('--evidence', default=str(EVIDENCE))
    parser.add_argument('--output', help='new folder for the receipt (default evidence/stress-<time>)')
    return run(parser.parse_args(argv))


if __name__ == '__main__':
    sys.exit(main())
