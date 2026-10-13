#!/usr/bin/env python3
"""CLI tool for exact kernel buffer delta attribution.

Answers: Who or what causes SystemProcessInformation buffer expansion?
Calculates exact byte contributions (threads added * 80B, new processes)
and categorizes whether expansions are single-culprit or multi-process entropy.
"""
import argparse
import json
import sys
from pathlib import Path

# Ensure src/ is on PYTHONPATH
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ChurnAttribution import (
    query_process_snapshot,
    parse_process_snapshot,
    diff_snapshots,
    sample_expansion_bursts
)


def main():
    parser = argparse.ArgumentParser(description="Attribute NtQuerySystemInformation buffer expansion.")
    parser.add_argument("--duration", type=float, default=10.0, help="Observation duration in seconds (default: 10.0)")
    parser.add_argument("--interval-ms", type=float, default=20.0, help="Sampling interval in ms (default: 20.0)")
    parser.add_argument("--once", action="store_true", help="Compare two back-to-back snapshots immediately")
    parser.add_argument("--json", action="store_true", help="Output raw JSON result")
    args = parser.parse_args()

    if args.once:
        _, s1, l1 = query_process_snapshot()
        import time
        time.sleep(args.interval_ms / 1000.0)
        _, s2, l2 = query_process_snapshot()
        result = diff_snapshots(s1, s2, l1, l2)
        if args.json:
            print(json.dumps(result, indent=2))
            return
        print("=== Single Differential Snapshot ===")
        print(f"Net Buffer Delta: {result['net_buffer_delta']} bytes")
        print(f"Positive Expansion: {result['positive_expansion_bytes']} bytes")
        print(f"Pattern: {result['pattern']} (CR1: {result['cr1']*100:.1f}%, CR3: {result['cr3']*100:.1f}%)")
        print("\nTop Contributors:")
        for c in result['top_contributors']:
            print(f"  PID {c['pid']:<6} {c['name']:<25} {c['type']:<15} +{c['thread_delta']} threads (+{c['bytes']}B, {c.get('share',0)*100:.1f}%)")
        return

    print(f"Sampling SystemProcessInformation expansion for {args.duration}s (interval: {args.interval_ms}ms)...")
    res = sample_expansion_bursts(duration_seconds=args.duration, interval_ms=args.interval_ms)

    if args.json:
        print(json.dumps(res, indent=2))
        return

    print("\n" + "="*50)
    print("=== Kernel Buffer Churn Attribution Report ===")
    print("="*50)
    print(f"Duration: {res['duration_seconds']}s | Samples: {res['total_samples']} | Expansion Bursts: {res['expansion_bursts_count']} ({res['expansion_rate']*100:.1f}%)")
    print(f"\nObserved Patterns Distribution:")
    for pat, count in res['patterns'].items():
        pct = count / max(1, res['expansion_bursts_count']) * 100
        print(f"  - {pat:<25}: {count} times ({pct:.1f}%)")

    print(f"\nTop Accumulated Byte Contributors Across All Bursts:")
    for img in res['top_accumulated_images']:
        print(f"  - {img['name']:<30}: +{img['total_positive_bytes']} bytes")

    print("\nConclusion:")
    single_dominant = res['patterns'].get('single_dominant_source', 0)
    total_bursts = max(1, res['expansion_bursts_count'])
    if single_dominant / total_bursts >= 0.50:
        top_culprit = res['top_accumulated_images'][0]['name'] if res['top_accumulated_images'] else 'Unknown'
        print(f"  => [SINGLE CULPRIT LIKELY]: Dominant single-source expansions occurred in {single_dominant/total_bursts*100:.1f}% of bursts.")
        print(f"     Primary suspect: {top_culprit}")
    else:
        print(f"  => [DIFFUSED SYSTEM ENTROPY]: Most bursts ({100 - single_dominant/total_bursts*100:.1f}%) are driven by multi-process concurrency, not a single rogue app.")
    print("="*50)


if __name__ == "__main__":
    main()
