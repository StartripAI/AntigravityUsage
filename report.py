#!/usr/bin/env python3
"""
Antigravity Token Tracker — Report Generator
Reads usage.jsonl and generates a summary report.
"""
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

LOG_FILE = Path.home() / ".config" / "anti-tracker" / "usage.jsonl"


def load_entries():
    if not LOG_FILE.exists():
        print(f"❌ No log file found at {LOG_FILE}")
        print("   Run start.sh first, then use Antigravity for a while.")
        sys.exit(1)

    entries = []
    with open(LOG_FILE) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return entries


def summarize(entries):
    if not entries:
        print("📭 No entries in log file yet.")
        return

    daily = defaultdict(lambda: {
        "requests": 0,
        "total_bytes": 0,
        "token_entries": 0,
        "paths": defaultdict(int),
        "token_data_samples": [],
    })

    for e in entries:
        local_time = e.get("local_time", "")
        date = local_time[:10] if local_time else "unknown"
        day = daily[date]
        day["requests"] += 1
        day["total_bytes"] += e.get("response_size", 0)
        path = e.get("path", "unknown")
        day["paths"][path] += 1

        if "token_data" in e:
            day["token_entries"] += 1
            day["token_data_samples"].append(e["token_data"])

    print(f"📊 Antigravity Token Tracker Report")
    print(f"{'=' * 60}")
    print(f"Log file: {LOG_FILE}")
    print(f"Total entries: {len(entries)}")
    print()

    for date in sorted(daily.keys()):
        day = daily[date]
        print(f"📅 {date}")
        print(f"   Requests: {day['requests']}")
        print(f"   Response data: {day['total_bytes']:,} bytes")
        print(f"   Token data entries: {day['token_entries']}")
        print(f"   Endpoints:")
        for path, count in sorted(day["paths"].items(), key=lambda x: -x[1]):
            short = path.split("/")[-1] if "/" in path else path
            print(f"     {short}: {count}x")

        if day["token_data_samples"]:
            print(f"   Token data samples:")
            # Show unique keys found in token data
            all_keys = set()
            for td in day["token_data_samples"]:
                all_keys.update(td.keys())
            for key in sorted(all_keys):
                vals = [
                    td[key] for td in day["token_data_samples"]
                    if key in td
                ]
                if vals:
                    # Show first and last value
                    if len(vals) == 1:
                        print(f"     {key}: {vals[0]}")
                    else:
                        print(f"     {key}: {vals[0]} → {vals[-1]} ({len(vals)} samples)")
        print()


def dump_raw(entries, n=10):
    """Dump the last N raw entries for debugging."""
    print(f"\n🔍 Last {n} raw entries:")
    print("-" * 60)
    for e in entries[-n:]:
        print(json.dumps(e, indent=2, ensure_ascii=False))
        print("-" * 40)


if __name__ == "__main__":
    entries = load_entries()

    if "--raw" in sys.argv:
        n = 10
        for arg in sys.argv:
            if arg.isdigit():
                n = int(arg)
        dump_raw(entries, n)
    else:
        summarize(entries)

    if "--raw" not in sys.argv and entries:
        print(f"💡 Tip: Use 'python3 report.py --raw' to see raw entries")
