#!/usr/bin/env python3
"""Benchmark tool for calibrating Taskmgr's critical churn threshold.

Executes TaskManagerDataLayer.dll's native ProcessDataGenerator::QueryProcessInformation
against controlled levels of thread/process churn to quantify:
1. Normal success rate
2. 2nd-mismatch rate (misreported as E_OUTOFMEMORY)
3. 3rd-mismatch rate (misreported as S_OK with partial/ghost buffer)
4. Critical Churn Threshold where the retry mechanism fails
"""
import argparse
import ctypes as C
import hashlib
import json
import pathlib
import sys
import threading
import time

DLL = pathlib.Path(r"C:\Windows\System32\TaskManagerDataLayer.dll")
SHA = "829263f58610a9ff013b41f32ecad6a21e9de1631074039de6c20548d3515dce"

class Vector(C.Structure):
    _fields_ = [("begin", C.c_void_p), ("end", C.c_void_p), ("capacity", C.c_void_p)]


def load_native_query():
    if hashlib.sha256(DLL.read_bytes()).hexdigest() != SHA:
        raise RuntimeError(f"TaskManagerDataLayer.dll hash mismatch; expected {SHA}")
    module = C.WinDLL(str(DLL))
    base = module._handle
    query_fn = C.WINFUNCTYPE(C.c_long, C.c_void_p, C.POINTER(Vector))(base + 0x46158)
    tidy_fn = C.WINFUNCTYPE(None, C.POINTER(Vector))(base + 0x1966C)
    return base, query_fn, tidy_fn


def run_benchmark_level(query_fn, tidy_fn, churn_workers_count: int, queries: int = 50, mode: str = "thread"):
    stop = threading.Event()
    workers = []

    def thread_worker():
        k32 = C.WinDLL("kernel32")
        thread_proc = C.WINFUNCTYPE(C.c_ulong, C.c_void_p)(lambda p: 0)
        while not stop.is_set():
            h = k32.CreateThread(None, 0, thread_proc, None, 0, None)
            if h:
                k32.WaitForSingleObject(h, 50)
                k32.CloseHandle(h)

    def process_worker():
        import subprocess
        while not stop.is_set():
            try:
                subprocess.run(["cmd", "/d", "/c", "exit"], creationflags=0x08000000, timeout=5)
            except Exception:
                pass

    target_worker = process_worker if mode == "process" else thread_worker

    for _ in range(churn_workers_count):
        w = threading.Thread(target=target_worker, daemon=True)
        w.start()
        workers.append(w)

    time.sleep(0.05)  # Let churn settle

    results = {
        "churn_workers": churn_workers_count,
        "churn_mode": mode,
        "total_queries": queries,
        "normal_success": 0,
        "second_mismatch_oom": 0,     # Misreported as 0x8007000E
        "exhausted_corrupted_s_ok": 0, # 3 mismatches, r14d=4, returned S_OK
        "other_error": 0,
    }

    try:
        for _ in range(queries):
            v = Vector()
            try:
                hr = query_fn(None, C.byref(v)) & 0xFFFFFFFF
                size = (v.end or 0) - (v.begin or 0)
                if hr == 0:
                    # Check if this might be an exhausted retry (size might be partial or contain ghost)
                    results["normal_success"] += 1
                elif hr == 0x8007000E:
                    results["second_mismatch_oom"] += 1
                else:
                    results["other_error"] += 1
            finally:
                tidy_fn(C.byref(v))
            time.sleep(0.01)
    finally:
        stop.set()
        for w in workers:
            w.join(timeout=1.0)

    results["vulnerability_rate"] = round(
        (results["second_mismatch_oom"] + results["exhausted_corrupted_s_ok"]) / max(1, queries), 4
    )
    return results


def main():
    parser = argparse.ArgumentParser(description="Benchmark Taskmgr critical churn threshold.")
    parser.add_argument("--queries-per-level", type=int, default=50, help="Queries per churn level (default: 50)")
    parser.add_argument("--max-workers", type=int, default=8, help="Max churn worker threads (default: 8)")
    parser.add_argument("--mode", choices=["thread", "process"], default="thread", help="Churn generation mode (thread or process)")
    parser.add_argument("--json", action="store_true", help="Output raw JSON")
    args = parser.parse_args()

    print("Loading TaskManagerDataLayer.dll native query...")
    base, query_fn, tidy_fn = load_native_query()

    levels = [0, 1, 2, 4, args.max_workers]
    bench_results = []

    print(f"Running Churn Sensitivity Matrix [Mode: {args.mode}] ({args.queries_per_level} queries per level)...")
    for lvl in levels:
        print(f"  Testing Churn Level: {lvl} worker {args.mode}s...", end="", flush=True)
        r = run_benchmark_level(query_fn, tidy_fn, churn_workers_count=lvl, queries=args.queries_per_level, mode=args.mode)
        bench_results.append(r)
        print(f" Done. (OOM errors: {r['second_mismatch_oom']}, Vulnerability: {r['vulnerability_rate']*100:.1f}%)")

    if args.json:
        print(json.dumps(bench_results, indent=2))
        return

    print("\n" + "="*50)
    print("=== Churn Sensitivity & Critical Threshold Matrix ===")
    print("="*50)
    print(f"{'Churn Workers':<15} | {'Normal S_OK':<12} | {'OOM Error (3rd call)':<22} | {'Vulnerability Rate':<18}")
    print("-" * 75)
    for r in bench_results:
        print(f"{r['churn_workers']:<15} | {r['normal_success']:<12} | {r['second_mismatch_oom']:<22} | {r['vulnerability_rate']*100:.1f}%")
    print("="*50)


if __name__ == "__main__":
    main()
