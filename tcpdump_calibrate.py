#!/usr/bin/env python3
"""
tcpdump-based passive calibration for Antigravity token estimator.
Captures actual TCP packet sizes to/from Google Cloud Code API.
Runs in background — zero keyboard/mouse control.

Usage:
    sudo python3 tcpdump_calibrate.py             # Start capture
    sudo python3 tcpdump_calibrate.py --duration 3600  # Run for 1 hour
    python3 tcpdump_calibrate.py --analyze         # Analyze captured data (no sudo)
"""
import json
import os
import re
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

LOG_DIR = Path.home() / ".config" / "anti-tracker"
CAP_LOG = LOG_DIR / "tcpdump_capture.jsonl"
CAL_RESULT = LOG_DIR / "calibration_result.json"
TARGET_HOST = "daily-cloudcode-pa.googleapis.com"


def capture(duration=None):
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    
    print(f"📡 tcpdump Calibration — Passive Packet Capture")
    print(f"   Target: {TARGET_HOST}")
    print(f"   Duration: {f'{duration}s' if duration else 'until Ctrl+C'}")
    print(f"   Log: {CAP_LOG}")
    print(f"   PID: {os.getpid()}")
    print(f"\n   Just use Antigravity normally. Every chat message = calibration data point.\n")

    cmd = [
        "sudo", "tcpdump", "-i", "any", "-q", "-l",
        f"host {TARGET_HOST}",
    ]

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, bufsize=1)

    # Aggregate packets into request/response bursts
    burst = {"out_bytes": 0, "in_bytes": 0, "out_pkts": 0, "in_pkts": 0, 
             "start": None, "last": None}
    burst_count = 0
    start_time = time.time()

    try:
        for line in proc.stdout:
            line = line.strip()
            if not line or "listening" in line or "packets" in line:
                continue

            # Parse: "15:57:46.385875 IP src.port > dst.port: tcp BYTES"
            # Direction: if src is local (198.18.x or 10.x or 192.168.x) → outbound
            m = re.match(
                r'[\d:.]+\s+IP\s+(\S+)\s+>\s+(\S+):\s+tcp\s+(\d+)',
                line
            )
            if not m:
                continue

            src, dst, size = m.group(1), m.group(2), int(m.group(3))
            if size == 0:
                continue  # TCP control packet

            now = time.time()
            is_outbound = not dst.endswith(".https") or src.split(".")[-1].isdigit()
            # Simpler: if destination port is https → outbound (we're sending TO the server)
            is_outbound = dst.endswith(".https") or dst.endswith(":443")

            # Start new burst if idle >3s
            if burst["last"] and (now - burst["last"]) > 3.0:
                if burst["out_bytes"] > 0 or burst["in_bytes"] > 0:
                    burst_count += 1
                    entry = {
                        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "burst": burst_count,
                        "out_bytes": burst["out_bytes"],
                        "in_bytes": burst["in_bytes"],
                        "out_pkts": burst["out_pkts"],
                        "in_pkts": burst["in_pkts"],
                        "duration_s": round(burst["last"] - burst["start"], 1) if burst["start"] else 0,
                    }
                    with open(CAP_LOG, "a") as f:
                        f.write(json.dumps(entry) + "\n")

                    # Quick estimate: protobuf ≈ 3-5 bytes per token
                    est_in_tok = burst["out_bytes"] // 4
                    est_out_tok = burst["in_bytes"] // 4
                    print(
                        f"  🔹 Burst #{burst_count}: "
                        f"sent={burst['out_bytes']:>10,}B ({burst['out_pkts']}pkts) "
                        f"recv={burst['in_bytes']:>10,}B ({burst['in_pkts']}pkts) "
                        f"~{est_in_tok:,}in/{est_out_tok:,}out tokens"
                    )

                burst = {"out_bytes": 0, "in_bytes": 0, "out_pkts": 0, "in_pkts": 0,
                         "start": now, "last": now}

            if burst["start"] is None:
                burst["start"] = now
            burst["last"] = now

            if is_outbound:
                burst["out_bytes"] += size
                burst["out_pkts"] += 1
            else:
                burst["in_bytes"] += size
                burst["in_pkts"] += 1

            # Check duration
            if duration and (time.time() - start_time) > duration:
                break

    except KeyboardInterrupt:
        pass

    proc.terminate()
    print(f"\n✅ Captured {burst_count} bursts")
    analyze()


def analyze():
    if not CAP_LOG.exists():
        print("❌ No capture data")
        return

    entries = []
    with open(CAP_LOG) as f:
        for line in f:
            if line.strip():
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

    # Filter out tiny bursts (keepalives, heartbeats)
    real = [e for e in entries if e["out_bytes"] > 5000 or e["in_bytes"] > 5000]

    if not real:
        print(f"⚠️ {len(entries)} bursts captured but none large enough for calibration")
        return

    print(f"\n📊 tcpdump Calibration Analysis")
    print(f"   Total bursts: {len(entries)}")
    print(f"   Real request/response pairs: {len(real)}")
    print(f"{'='*65}")

    # Per-burst breakdown
    for e in real[-15:]:
        est_in = e["out_bytes"] // 4
        est_out = e["in_bytes"] // 4
        print(
            f"  {e.get('ts','?')} | "
            f"sent={e['out_bytes']:>10,}B recv={e['in_bytes']:>10,}B | "
            f"~{est_in:>8,} in_tok ~{est_out:>6,} out_tok"
        )

    # Stats
    out_sizes = [e["out_bytes"] for e in real]
    in_sizes = [e["in_bytes"] for e in real]

    avg_out = sum(out_sizes) / len(out_sizes)
    avg_in = sum(in_sizes) / len(in_sizes)

    # At TCP level, less overhead than nettop:
    # Protobuf-encoded text: ~3-5 bytes per token
    # But context includes system prompt, conversation history, etc.
    # The KEY insight from nettop calibration: avg_out ≈ 3MB = context
    # At TCP level (no TLS record headers) it should be slightly less

    # Better ratio estimation using packet-level data
    # TLS record overhead: ~20-40 bytes per record
    # Protobuf + gRPC framing: ~50-100 bytes per message
    # Actual token encoding: ~3-4 bytes per token

    print(f"\n{'='*65}")
    print(f"  Avg sent per request:     {avg_out:>10,.0f}B ({avg_out/1024/1024:.2f}MB)")
    print(f"  Avg received per request: {avg_in:>10,.0f}B ({avg_in/1024:.0f}KB)")

    # Derive: bytes_out / 4 ≈ total input tokens per request  
    # bytes_in / 4 ≈ total output tokens per request
    bpt_out = 4.0  # bytes per token at TCP payload level
    bpt_in = 4.0

    # For the nettop estimator: nettop adds TLS record layer + TCP headers
    # TLS adds ~5 bytes per record, records are ~16KB max
    # TCP headers: 20-60 bytes per packet
    # For 3MB of data: ~200 TLS records × 5B = 1KB overhead + ~400 packets × 40B = 16KB
    # So nettop bytes ≈ TCP payload × 1.005 (negligible)
    # The ratio for nettop should be nearly the same as TCP payload
    nettop_bpt_out = bpt_out * 1.01
    nettop_bpt_in = bpt_in * 1.01

    cal = {
        "bytes_out_per_input_token": round(nettop_bpt_out, 3),
        "bytes_in_per_output_token": round(nettop_bpt_in, 3),
        "tcp_bytes_per_token": 4.0,
        "source": "tcpdump",
        "samples": len(real),
        "avg_tcp_out": round(avg_out),
        "avg_tcp_in": round(avg_in),
        "calibrated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "notes": f"Based on {len(real)} real request/response pairs captured via tcpdump. "
                 f"Avg request sends {avg_out/1024/1024:.1f}MB (full context), "
                 f"avg response receives {avg_in/1024:.0f}KB.",
    }
    with open(CAL_RESULT, "w") as f:
        json.dump(cal, f, indent=2)
    print(f"\n✅ Calibration saved to {CAL_RESULT}")
    print(f"   bytes_out_per_input_token: {nettop_bpt_out:.3f}")
    print(f"   bytes_in_per_output_token: {nettop_bpt_in:.3f}")


if __name__ == "__main__":
    if "--analyze" in sys.argv:
        analyze()
    else:
        dur = None
        if "--duration" in sys.argv:
            idx = sys.argv.index("--duration")
            dur = int(sys.argv[idx + 1])
        capture(dur)
