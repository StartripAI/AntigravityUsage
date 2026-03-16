#!/usr/bin/env python3
"""
Antigravity Token Estimator — nettop-based passive monitor
Tracks language_server_macos_arm network traffic and estimates token usage.

Model: Claude Opus 4.6 (Thinking)
Pricing: $5/M input, $25/M output

Usage:
  python3 anti_estimator.py start   # Start daemon
  python3 anti_estimator.py stop    # Stop daemon
  python3 anti_estimator.py status  # Show current session stats
  python3 anti_estimator.py report  # Daily report
"""
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# === Config ===
PROCESS_NAME = "language_server"
LOG_DIR = Path.home() / ".config" / "anti-tracker"
USAGE_LOG = LOG_DIR / "nettop_usage.jsonl"
PID_FILE = LOG_DIR / "estimator.pid"
SNAPSHOT_FILE = LOG_DIR / "last_snapshot.json"
POLL_INTERVAL = 30  # seconds

# Claude Opus 4.6 Thinking pricing ($/M tokens)
MODEL = "claude-opus-4-6-thinking"
INPUT_PRICE_PER_M = 5.00
OUTPUT_PRICE_PER_M = 25.00

# Calibration: bytes-to-token ratio (TCP payload level)
# Calibrated via tcpdump: 328 bursts, 290+ real pairs, B/tok=4.0
# Auto-loads from calibration_result.json if available
CAL_FILE = LOG_DIR / "calibration_result.json"
_DEFAULT_OUT = 4.0  # outbound bytes per input token (tcpdump calibrated)
_DEFAULT_IN = 4.0   # inbound bytes per output token (tcpdump calibrated)

def _load_calibration():
    if CAL_FILE.exists():
        try:
            with open(CAL_FILE) as f:
                cal = json.load(f)
            return cal.get("bytes_out_per_input_token", _DEFAULT_OUT), cal.get("bytes_in_per_output_token", _DEFAULT_IN)
        except Exception:
            pass
    return _DEFAULT_OUT, _DEFAULT_IN

BYTES_OUT_PER_INPUT_TOKEN, BYTES_IN_PER_OUTPUT_TOKEN = _load_calibration()


def ensure_dir():
    LOG_DIR.mkdir(parents=True, exist_ok=True)


def get_nettop_snapshot():
    """Get current network stats for language_server processes via nettop."""
    try:
        result = subprocess.run(
            ["nettop", "-P", "-L", "1", "-n", "-J", "bytes_in,bytes_out"],
            capture_output=True, text=True, timeout=15
        )
        total_in = 0
        total_out = 0
        pids = []
        for line in result.stdout.strip().split("\n"):
            if PROCESS_NAME in line.lower():
                parts = line.split(",")
                if len(parts) >= 3:
                    name_pid = parts[0]
                    try:
                        bytes_in = int(parts[1])
                        bytes_out = int(parts[2])
                        total_in += bytes_in
                        total_out += bytes_out
                        # Extract PID from "language_server.12345"
                        pid = name_pid.split(".")[-1] if "." in name_pid else "?"
                        pids.append(pid)
                    except (ValueError, IndexError):
                        continue
        return {
            "bytes_in": total_in,
            "bytes_out": total_out,
            "pids": pids,
            "time": time.time(),
        }
    except Exception as e:
        return None


def estimate_tokens(delta_bytes_in, delta_bytes_out):
    """Estimate token counts from network byte deltas."""
    input_tokens = max(0, int(delta_bytes_out / BYTES_OUT_PER_INPUT_TOKEN))
    output_tokens = max(0, int(delta_bytes_in / BYTES_IN_PER_OUTPUT_TOKEN))
    input_cost = input_tokens * INPUT_PRICE_PER_M / 1_000_000
    output_cost = output_tokens * OUTPUT_PRICE_PER_M / 1_000_000
    return {
        "input_tokens_est": input_tokens,
        "output_tokens_est": output_tokens,
        "total_tokens_est": input_tokens + output_tokens,
        "input_cost_est": round(input_cost, 4),
        "output_cost_est": round(output_cost, 4),
        "total_cost_est": round(input_cost + output_cost, 4),
    }


def load_last_snapshot():
    if SNAPSHOT_FILE.exists():
        with open(SNAPSHOT_FILE) as f:
            return json.load(f)
    return None


def save_snapshot(snap):
    with open(SNAPSHOT_FILE, "w") as f:
        json.dump(snap, f)


def log_entry(entry):
    with open(USAGE_LOG, "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def daemon_loop():
    """Main monitoring loop."""
    ensure_dir()
    print(f"🔍 Antigravity Token Estimator started")
    print(f"   Model: {MODEL}")
    print(f"   Polling every {POLL_INTERVAL}s")
    print(f"   Log: {USAGE_LOG}")
    print(f"   PID: {os.getpid()}")

    # Save PID
    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))

    # Get initial snapshot
    prev = get_nettop_snapshot()
    if not prev:
        print("❌ Cannot get nettop data. Is language_server running?")
        sys.exit(1)
    save_snapshot(prev)
    print(f"   Initial: {prev['bytes_in']:,} bytes in / {prev['bytes_out']:,} bytes out")
    print(f"   Tracking PIDs: {prev['pids']}")

    session_in = 0
    session_out = 0

    while True:
        time.sleep(POLL_INTERVAL)

        curr = get_nettop_snapshot()
        if not curr:
            continue

        # Calculate delta (nettop gives cumulative totals for process lifetime)
        delta_in = max(0, curr["bytes_in"] - prev["bytes_in"])
        delta_out = max(0, curr["bytes_out"] - prev["bytes_out"])

        # Detect process restart (cumulative bytes reset)
        if curr["bytes_in"] < prev["bytes_in"] or curr["bytes_out"] < prev["bytes_out"]:
            print(f"⚡ Process restart detected, resetting baseline")
            prev = curr
            save_snapshot(curr)
            continue

        # Skip if no traffic
        if delta_in == 0 and delta_out == 0:
            prev = curr
            continue

        session_in += delta_in
        session_out += delta_out

        # Estimate tokens
        est = estimate_tokens(delta_in, delta_out)

        entry = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "model": MODEL,
            "delta_bytes_in": delta_in,
            "delta_bytes_out": delta_out,
            "session_bytes_in": session_in,
            "session_bytes_out": session_out,
            **est,
        }
        log_entry(entry)
        save_snapshot(curr)

        session_est = estimate_tokens(session_in, session_out)
        print(
            f"[{entry['timestamp']}] "
            f"+{delta_in:,}B in / +{delta_out:,}B out → "
            f"~{est['total_tokens_est']:,} tokens (${est['total_cost_est']:.4f}) | "
            f"Session: ~{session_est['total_tokens_est']:,} tokens (${session_est['total_cost_est']:.4f})"
        )

        prev = curr


def cmd_start():
    """Start the estimator daemon."""
    ensure_dir()
    if PID_FILE.exists():
        pid = int(PID_FILE.read_text().strip())
        try:
            os.kill(pid, 0)
            print(f"⚠️  Already running (PID {pid}). Use 'stop' first.")
            return
        except ProcessLookupError:
            PID_FILE.unlink()

    # Check if language_server is running
    snap = get_nettop_snapshot()
    if not snap or snap["bytes_in"] == 0 and snap["bytes_out"] == 0:
        print("❌ No language_server_macos_arm process found.")
        print("   Make sure Antigravity is running.")
        return

    # Fork to background
    if "--foreground" in sys.argv or "-f" in sys.argv:
        daemon_loop()
    else:
        pid = os.fork()
        if pid > 0:
            print(f"🔍 Estimator started in background (PID {pid})")
            print(f"   Log: {USAGE_LOG}")
            print(f"   Use 'status' to check, 'stop' to stop")
        else:
            # Child process
            os.setsid()
            sys.stdout = open(LOG_DIR / "estimator_stdout.log", "a")
            sys.stderr = sys.stdout
            daemon_loop()


def cmd_stop():
    """Stop the estimator daemon."""
    if not PID_FILE.exists():
        print("ℹ️  Not running")
        return
    pid = int(PID_FILE.read_text().strip())
    try:
        os.kill(pid, signal.SIGTERM)
        print(f"✅ Stopped (PID {pid})")
    except ProcessLookupError:
        print("ℹ️  Process already gone")
    PID_FILE.unlink(missing_ok=True)


def cmd_status():
    """Show current session stats."""
    ensure_dir()
    if not USAGE_LOG.exists():
        print("📭 No data yet. Start the estimator first.")
        return

    entries = []
    with open(USAGE_LOG) as f:
        for line in f:
            if line.strip():
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

    if not entries:
        print("📭 No entries yet")
        return

    today = datetime.now().strftime("%Y-%m-%d")
    today_entries = [e for e in entries if e.get("timestamp", "").startswith(today)]

    total_in = sum(e.get("input_tokens_est", 0) for e in today_entries)
    total_out = sum(e.get("output_tokens_est", 0) for e in today_entries)
    total_cost = sum(e.get("total_cost_est", 0) for e in today_entries)
    total_bytes_in = sum(e.get("delta_bytes_in", 0) for e in today_entries)
    total_bytes_out = sum(e.get("delta_bytes_out", 0) for e in today_entries)

    running = False
    if PID_FILE.exists():
        try:
            os.kill(int(PID_FILE.read_text().strip()), 0)
            running = True
        except (ProcessLookupError, ValueError):
            pass

    print(f"📊 Antigravity Token Estimator — {today}")
    print(f"   Status: {'🟢 Running' if running else '🔴 Stopped'}")
    print(f"   Model: {MODEL}")
    print(f"   ──────────────────────────────────")
    print(f"   Network: {total_bytes_in:,} bytes in / {total_bytes_out:,} bytes out")
    print(f"   ──────────────────────────────────")
    print(f"   Input tokens (est):  {total_in:>12,}")
    print(f"   Output tokens (est): {total_out:>12,}")
    print(f"   Total tokens (est):  {total_in + total_out:>12,}")
    print(f"   ──────────────────────────────────")
    print(f"   Input cost (est):    ${total_in * INPUT_PRICE_PER_M / 1_000_000:>10.2f}")
    print(f"   Output cost (est):   ${total_out * OUTPUT_PRICE_PER_M / 1_000_000:>10.2f}")
    print(f"   Total cost (est):    ${total_cost:>10.2f}")
    print(f"   ──────────────────────────────────")
    print(f"   Entries today: {len(today_entries)}")
    if today_entries:
        print(f"   First: {today_entries[0]['timestamp']}")
        print(f"   Last:  {today_entries[-1]['timestamp']}")


def cmd_report():
    """Daily usage report."""
    ensure_dir()
    if not USAGE_LOG.exists():
        print("📭 No data yet")
        return

    from collections import defaultdict
    daily = defaultdict(lambda: {"in": 0, "out": 0, "cost": 0, "bytes_in": 0, "bytes_out": 0, "entries": 0})

    with open(USAGE_LOG) as f:
        for line in f:
            if not line.strip():
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            date = e.get("timestamp", "")[:10]
            d = daily[date]
            d["in"] += e.get("input_tokens_est", 0)
            d["out"] += e.get("output_tokens_est", 0)
            d["cost"] += e.get("total_cost_est", 0)
            d["bytes_in"] += e.get("delta_bytes_in", 0)
            d["bytes_out"] += e.get("delta_bytes_out", 0)
            d["entries"] += 1

    print(f"📊 Antigravity Estimated Token Usage ({MODEL})")
    print(f"{'─' * 75}")
    print(f"{'Date':<12} {'Input Tok':>12} {'Output Tok':>12} {'Traffic':>12} {'Cost (est)':>12}")
    print(f"{'─' * 75}")

    grand_in = grand_out = grand_cost = 0
    for date in sorted(daily.keys()):
        d = daily[date]
        total_bytes = d["bytes_in"] + d["bytes_out"]
        mb = total_bytes / 1_000_000
        print(f"{date:<12} {d['in']:>12,} {d['out']:>12,} {mb:>10.1f}MB ${d['cost']:>10.2f}")
        grand_in += d["in"]
        grand_out += d["out"]
        grand_cost += d["cost"]

    print(f"{'─' * 75}")
    print(f"{'TOTAL':<12} {grand_in:>12,} {grand_out:>12,} {'':>12} ${grand_cost:>10.2f}")
    print(f"\n⚠️  Estimates based on ~{BYTES_OUT_PER_INPUT_TOKEN:.1f} bytes/input token, "
          f"~{BYTES_IN_PER_OUTPUT_TOKEN:.1f} bytes/output token (tcpdump calibrated). Actual may vary ±15%.")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    cmds = {
        "start": cmd_start,
        "stop": cmd_stop,
        "status": cmd_status,
        "report": cmd_report,
    }
    if cmd in cmds:
        cmds[cmd]()
    else:
        print(f"Usage: {sys.argv[0]} [start|stop|status|report]")
        print(f"  start  — Start monitoring daemon")
        print(f"  stop   — Stop monitoring daemon")
        print(f"  status — Show today's stats")
        print(f"  report — Daily report")
