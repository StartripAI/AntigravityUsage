#!/usr/bin/env python3
"""
DTrace-based SSL probe for Antigravity language_server.
Captures SSL_write/SSL_read sizes to get exact request/response byte counts.

Usage:
    sudo python3 dtrace_probe.py           # Auto-detect language_server PID
    sudo python3 dtrace_probe.py --pid PID # Specific PID
    python3 dtrace_probe.py --analyze      # Analyze captured data
"""
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

LOG_DIR = Path.home() / ".config" / "anti-tracker"
PROBE_LOG = LOG_DIR / "dtrace_ssl.jsonl"
CAL_RESULT = LOG_DIR / "calibration_result.json"


def find_language_server_pid():
    result = subprocess.run(["pgrep", "-f", "language_server_macos_arm"],
                            capture_output=True, text=True)
    pids = result.stdout.strip().split("\n")
    return int(pids[0]) if pids and pids[0] else None


def run_probe(pid):
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    print(f"🔬 DTrace SSL Probe — language_server PID {pid}")
    print(f"   Log: {PROBE_LOG}")
    print(f"   Press Ctrl+C to stop\n")

    # DTrace script: capture SSL_write and SSL_read sizes
    # The Go runtime uses BoringSSL. Function names: SSL_write, SSL_read
    dtrace_script = f"""
    pid{pid}::SSL_write:entry
    {{
        self->write_ts = timestamp;
        self->write_size = arg2;
    }}
    pid{pid}::SSL_write:return
    /self->write_ts/
    {{
        printf("W %d %d\\n", self->write_size, (timestamp - self->write_ts) / 1000);
        self->write_ts = 0;
    }}
    pid{pid}::SSL_read:entry
    {{
        self->read_ts = timestamp;
        self->read_buf = arg1;
        self->read_size = arg2;
    }}
    pid{pid}::SSL_read:return
    /self->read_ts && arg1 > 0/
    {{
        printf("R %d %d\\n", arg1, (timestamp - self->read_ts) / 1000);
        self->read_ts = 0;
    }}
    """

    proc = subprocess.Popen(
        ["sudo", "dtrace", "-qn", dtrace_script],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1
    )

    # Aggregate into request/response pairs
    current_request = {"writes": 0, "write_bytes": 0, "reads": 0, "read_bytes": 0, "start": None}
    idle_since = time.time()
    request_count = 0

    try:
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue

            parts = line.split()
            if len(parts) < 3:
                continue

            direction = parts[0]  # W or R
            size = int(parts[1])
            latency_us = int(parts[2])

            now = time.time()

            # If we've been idle for >2s, this is a new request
            if now - idle_since > 2.0 and (current_request["writes"] > 0 or current_request["reads"] > 0):
                # Save previous request
                if current_request["start"]:
                    request_count += 1
                    entry = {
                        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "req": request_count,
                        "ssl_write_bytes": current_request["write_bytes"],
                        "ssl_write_calls": current_request["writes"],
                        "ssl_read_bytes": current_request["read_bytes"],
                        "ssl_read_calls": current_request["reads"],
                        "duration_s": round(now - current_request["start"], 1),
                    }
                    with open(PROBE_LOG, "a") as f:
                        f.write(json.dumps(entry) + "\n")

                    est_in_tok = entry["ssl_write_bytes"] // 4   # ~4 bytes per token in protobuf
                    est_out_tok = entry["ssl_read_bytes"] // 4
                    print(
                        f"  #{request_count}: "
                        f"write={entry['ssl_write_bytes']:>10,}B ({entry['ssl_write_calls']} calls) "
                        f"read={entry['ssl_read_bytes']:>10,}B ({entry['ssl_read_calls']} calls) "
                        f"~{est_in_tok:,} in_tok ~{est_out_tok:,} out_tok "
                        f"({entry['duration_s']}s)"
                    )

                current_request = {"writes": 0, "write_bytes": 0, "reads": 0, "read_bytes": 0, "start": now}

            if current_request["start"] is None:
                current_request["start"] = now

            if direction == "W":
                current_request["writes"] += 1
                current_request["write_bytes"] += size
            elif direction == "R":
                current_request["reads"] += 1
                current_request["read_bytes"] += size

            idle_since = now

    except KeyboardInterrupt:
        print(f"\n\n✅ Captured {request_count} request/response pairs")
        proc.terminate()

    analyze()


def analyze():
    if not PROBE_LOG.exists():
        print("❌ No probe data")
        return

    entries = []
    with open(PROBE_LOG) as f:
        for line in f:
            if line.strip():
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

    if not entries:
        print("❌ No entries")
        return

    print(f"\n📊 DTrace SSL Analysis ({len(entries)} requests)")
    print("=" * 60)

    writes = [e["ssl_write_bytes"] for e in entries]
    reads = [e["ssl_read_bytes"] for e in entries]

    avg_w = sum(writes) / len(writes)
    avg_r = sum(reads) / len(reads)

    print(f"  Avg SSL_write per request: {avg_w:,.0f} bytes ({avg_w/1024:.0f}KB)")
    print(f"  Avg SSL_read per request:  {avg_r:,.0f} bytes ({avg_r/1024:.0f}KB)")

    # At SSL level: much less overhead than nettop (no TCP/TLS frame headers)
    # Protobuf text: ~3-4 bytes per token
    bytes_per_input_token = round(avg_w / max(1, avg_w // 4), 2) if avg_w > 0 else 4.0
    bytes_per_output_token = round(avg_r / max(1, avg_r // 4), 2) if avg_r > 0 else 4.0

    print(f"\n  Estimated bytes/input_token:  {bytes_per_input_token:.2f}")
    print(f"  Estimated bytes/output_token: {bytes_per_output_token:.2f}")

    cal = {
        "bytes_out_per_input_token": bytes_per_input_token,
        "bytes_in_per_output_token": bytes_per_output_token,
        "source": "dtrace_ssl",
        "samples": len(entries),
        "avg_ssl_write": round(avg_w),
        "avg_ssl_read": round(avg_r),
        "calibrated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    with open(CAL_RESULT, "w") as f:
        json.dump(cal, f, indent=2)
    print(f"\n✅ Saved to {CAL_RESULT}")


if __name__ == "__main__":
    if "--analyze" in sys.argv:
        analyze()
    else:
        pid = None
        if "--pid" in sys.argv:
            idx = sys.argv.index("--pid")
            pid = int(sys.argv[idx + 1])
        else:
            pid = find_language_server_pid()

        if not pid:
            print("❌ language_server not found")
            sys.exit(1)

        run_probe(pid)
