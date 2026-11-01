#!/usr/bin/env python3
"""Backfill daily flatline and native error counts into daily-monitoring.json.

Idempotent and safe to run repeatedly.
Re-calculates flatlines_confirmed and native_0x8007139f_count for all dates
present in daily-monitoring.json from authoritative incident folders (restart-*).
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_OUTPUT = Path(r"D:\TaskmgrFreezeCaptures")


def parse_utc_date(ts_str: str) -> str | None:
    if not ts_str:
        return None
    try:
        dt = datetime.fromisoformat(ts_str).astimezone(timezone.utc)
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return None


def collect_daily_incident_counts(output_dir: Path) -> dict[str, dict[str, int]]:
    """Inspects restart-* folders and returns per-UTC-date counts:
    {
        'YYYY-MM-DD': {
            'flatlines': int,
            'native': int
        }
    }
    """
    counts: dict[str, dict[str, int]] = {}

    folders = sorted([
        p for p in output_dir.iterdir()
        if p.is_dir() and p.name.startswith("restart-")
        and not p.name.startswith("verification-")
        and "-reanalysis" not in p.name
    ])

    for folder in folders:
        event_file = folder / "event.json"
        if not event_file.exists():
            continue
        try:
            event = json.loads(event_file.read_text(encoding="utf-8"))
        except Exception:
            continue

        date_str = parse_utc_date(event.get("timestamp"))
        if not date_str:
            continue

        if date_str not in counts:
            counts[date_str] = {"flatlines": 0, "native": 0}

        counts[date_str]["flatlines"] += 1

        # Check native 0x8007139F: event.json native_event_time or incident-analysis.json native_signature
        has_native = bool(event.get("native_event_time"))
        if not has_native:
            ia_file = folder / "incident-analysis.json"
            if ia_file.exists():
                try:
                    ia = json.loads(ia_file.read_text(encoding="utf-8"))
                    has_native = bool(ia.get("native_signature"))
                except Exception:
                    pass

        if has_native:
            counts[date_str]["native"] += 1

    # Also check n1-summary.json for outside flatlines
    n1_file = output_dir / "n1-summary.json"
    if n1_file.exists():
        try:
            n1_data = json.loads(n1_file.read_text(encoding="utf-8"))
            outside = n1_data.get("outside_flatlines", [])
            for item in outside:
                ts = item.get("timestamp_utc") or item.get("timestamp")
                date_str = parse_utc_date(ts)
                if date_str:
                    if date_str not in counts:
                        counts[date_str] = {"flatlines": 0, "native": 0}
                    counts[date_str]["native"] += 1
        except Exception:
            pass

    return counts


def backfill_daily_monitoring(output_dir: Path, backfill_time: str = "2026-11-01T22:30:00Z") -> list[dict]:
    daily_file = output_dir / "daily-monitoring.json"
    if not daily_file.exists():
        print(f"Error: {daily_file} does not exist.", file=sys.stderr)
        return []

    history = json.loads(daily_file.read_text(encoding="utf-8"))
    if not isinstance(history, list):
        print(f"Error: {daily_file} does not contain a JSON list.", file=sys.stderr)
        return []

    incident_counts = collect_daily_incident_counts(output_dir)

    for entry in history:
        date_str = entry.get("date")
        if not date_str:
            continue

        counts = incident_counts.get(date_str, {"flatlines": 0, "native": 0})
        entry["flatlines_confirmed"] = counts["flatlines"]
        entry["native_0x8007139f_count"] = counts["native"]
        entry["native_0x8007139f_events"] = None  # null in JSON

        if "backfilled" not in entry or not entry["backfilled"]:
            entry["backfilled"] = f"{backfill_time} counter fix"

        # Update note text if it contains the old undercounting wording
        note = entry.get("note")
        if note and "统计口径有误，数值偏低" in note:
            entry["note"] = note.replace("统计口径有误，数值偏低", "冻结与原生错误计数已回填")

    temp_file = daily_file.with_suffix(".tmp")
    text = json.dumps(history, indent=2, ensure_ascii=False) + "\n"
    temp_file.write_text(text, encoding="utf-8")
    temp_file.replace(daily_file)

    return history


def main():
    parser = argparse.ArgumentParser(description="Backfill daily flatline and native error counts.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT,
                        help=f"Path to captures folder (default: {DEFAULT_OUTPUT})")
    parser.add_argument("--backfill-time", type=str, default="2026-11-01T22:30:00Z",
                        help="Backfill timestamp tag (default: 2026-11-01T22:30:00Z)")
    args = parser.parse_args()

    updated = backfill_daily_monitoring(args.output, args.backfill_time)
    print(f"Backfilled {len(updated)} entries in {args.output / 'daily-monitoring.json'}:")
    for e in updated:
        print(f"  {e.get('date')}: flatlines={e.get('flatlines_confirmed')}, "
              f"native={e.get('native_0x8007139f_count')}, "
              f"events={e.get('native_0x8007139f_events')}, "
              f"backfilled={e.get('backfilled')}")


if __name__ == "__main__":
    main()
