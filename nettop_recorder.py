#!/usr/bin/env python3
"""
High-frequency nettop recorder for self-calibration.
Records bytes every 2 seconds. Run in background while using Antigravity normally.
Each nettop spike maps to a request/response pair.

Usage:
    python3 nettop_recorder.py start    # Start recording (background)
    python3 nettop_recorder.py stop     # Stop recording
    python3 nettop_recorder.py analyze  # Analyze spikes
"""
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

LOG_DIR = Path.home() / ".config" / "anti-tracker"
REC_LOG = LOG_DIR / "nettop_hf.jsonl"
REC_PID = LOG_DIR / "recorder.pid"
PROCESS_NAME = "language_server"
INTERVAL = 2  # seconds


def get_nettop():
    try:
        result = subprocess.run(
            ["nettop", "-P", "-L", "1", "-n", "-J", "bytes_in,bytes_out"],
            capture_output=True, text=True, timeout=15
        )
        total_in = total_out = 0
        for line in result.stdout.strip().split("\n"):
            if PROCESS_NAME in line.lower():
                parts = line.split(",")
                if len(parts) >= 3:
                    try:
                        total_in += int(parts[1])
                        total_out += int(parts[2])
                    except ValueError:
                        continue
        return total_in, total_out
    except Exception:
        return None, None


def record():
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with open(REC_PID, "w") as f:
        f.write(str(os.getpid()))
    
    print(f"📡 High-frequency nettop recorder started (every {INTERVAL}s)")
    print(f"   PID: {os.getpid()}")
    print(f"   Log: {REC_LOG}")
    
    prev_in, prev_out = get_nettop()
    if prev_in is None:
        print("❌ nettop failed")
        sys.exit(1)
    
    while True:
        time.sleep(INTERVAL)
        cur_in, cur_out = get_nettop()
        if cur_in is None:
            continue
        
        d_in = max(0, cur_in - prev_in)
        d_out = max(0, cur_out - prev_out)
        
        if cur_in < prev_in or cur_out < prev_out:
            prev_in, prev_out = cur_in, cur_out
            continue
        
        entry = {
            "t": datetime.now().strftime("%H:%M:%S"),
            "ts": time.time(),
            "d_in": d_in,
            "d_out": d_out,
            "cum_in": cur_in,
            "cum_out": cur_out,
        }
        with open(REC_LOG, "a") as f:
            f.write(json.dumps(entry) + "\n")
        
        if d_in > 1000 or d_out > 1000:
            print(f"  [{entry['t']}] Δin={d_in:>10,}B  Δout={d_out:>10,}B {'🔥' if d_out > 500000 else ''}")
        
        prev_in, prev_out = cur_in, cur_out


def stop():
    if REC_PID.exists():
        try:
            pid = int(REC_PID.read_text().strip())
            os.kill(pid, signal.SIGTERM)
            print(f"✅ Stopped recorder (PID {pid})")
        except (ProcessLookupError, ValueError):
            print("Already stopped")
        REC_PID.unlink(missing_ok=True)
    else:
        print("Not running")


def analyze():
    if not REC_LOG.exists():
        print("❌ No recording data")
        return
    
    entries = []
    with open(REC_LOG) as f:
        for line in f:
            if line.strip():
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    
    # Find request/response "spikes" — clusters of non-zero deltas
    spikes = []
    current_spike = None
    
    for e in entries:
        if e["d_in"] > 1000 or e["d_out"] > 1000:
            if current_spike is None:
                current_spike = {"start": e["ts"], "t_start": e["t"], "bytes_in": 0, "bytes_out": 0, "samples": 0}
            current_spike["bytes_in"] += e["d_in"]
            current_spike["bytes_out"] += e["d_out"]
            current_spike["samples"] += 1
            current_spike["end"] = e["ts"]
            current_spike["t_end"] = e["t"]
        else:
            if current_spike and current_spike["samples"] >= 1:
                current_spike["duration"] = current_spike.get("end", current_spike["start"]) - current_spike["start"]
                spikes.append(current_spike)
            current_spike = None
    
    if current_spike and current_spike["samples"] >= 1:
        current_spike["duration"] = current_spike.get("end", current_spike["start"]) - current_spike["start"]
        spikes.append(current_spike)
    
    print(f"📊 Spike Analysis ({len(spikes)} request/response pairs from {len(entries)} samples)")
    print(f"{'='*70}")
    
    if not spikes:
        print("No spikes detected")
        return
    
    for i, s in enumerate(spikes[-20:]):  # Show last 20
        est_input = s["bytes_out"] // 15
        est_output = max(0, s["bytes_in"] - 80000) // 4  # subtract overhead
        print(
            f"  {s.get('t_start','?')}-{s.get('t_end','?')} | "
            f"out={s['bytes_out']:>10,}B in={s['bytes_in']:>10,}B | "
            f"~{est_input:>8,} in_tok ~{est_output:>6,} out_tok | "
            f"{s['duration']:.0f}s"
        )
    
    # Aggregate
    total_out = sum(s["bytes_out"] for s in spikes)
    total_in = sum(s["bytes_in"] for s in spikes)
    avg_out = total_out / len(spikes)
    avg_in = total_in / len(spikes)
    
    print(f"\n{'='*70}")
    print(f"  Total spikes:    {len(spikes)}")
    print(f"  Avg bytes_out:   {avg_out:,.0f} ({avg_out/1024/1024:.1f}MB)")
    print(f"  Avg bytes_in:    {avg_in:,.0f} ({avg_in/1024:.0f}KB)")
    print(f"  Estimated ratio: {avg_out/max(1,(avg_out//15)):.1f} B/input_tok, {avg_in/max(1,(max(0,avg_in-80000)//4)):.1f} B/output_tok")
    
    # Save calibration
    cal = {
        "bytes_out_per_input_token": 15.0,
        "bytes_in_per_output_token": round(avg_in / max(1, (max(0, avg_in - 80000) // 4)), 3) if avg_in > 80000 else 300.0,
        "per_request_overhead_bytes_in": 80000,
        "samples": len(spikes),
        "calibrated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    cal_path = LOG_DIR / "calibration_result.json"
    with open(cal_path, "w") as f:
        json.dump(cal, f, indent=2)
    print(f"\n✅ Saved calibration to {cal_path}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "start"
    if cmd == "start":
        record()
    elif cmd == "stop":
        stop()
    elif cmd == "analyze":
        analyze()
    else:
        print(f"Usage: {sys.argv[0]} [start|stop|analyze]")
