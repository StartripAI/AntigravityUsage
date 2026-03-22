#!/usr/bin/env python3
"""
Antigravity Token Estimator — nettop-based passive monitor
Tracks language_server_macos_arm network traffic and estimates token usage.

Supports 6 Antigravity models with individual pricing.
Applies API traffic ratio (nettop captures ~6x more than actual API calls).

Usage:
  python3 anti_estimator.py start   # Start daemon
  python3 anti_estimator.py stop    # Stop daemon
  python3 anti_estimator.py status  # Show current session stats
  python3 anti_estimator.py report  # Daily report
  python3 anti_estimator.py model   # Show/switch model
  python3 anti_estimator.py model 3 # Quick switch to model #3
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
MODEL_FILE = LOG_DIR / "current_model.json"
POLL_INTERVAL = 30  # seconds

# === Model Catalog ===
# Pricing from public API rates ($/M tokens)
MODELS = {
    "gemini-3.1-pro-high": {
        "name": "Gemini 3.1 Pro (High)",
        "input_price": 1.25,
        "output_price": 10.00,
        "shortcut": "1",
    },
    "gemini-3.1-pro-low": {
        "name": "Gemini 3.1 Pro (Low)",
        "input_price": 0.30,
        "output_price": 2.50,
        "shortcut": "2",
    },
    "gemini-3-flash": {
        "name": "Gemini 3 Flash",
        "input_price": 0.10,
        "output_price": 0.40,
        "shortcut": "3",
    },
    "claude-sonnet-4.6-thinking": {
        "name": "Claude Sonnet 4.6 (Thinking)",
        "input_price": 3.00,
        "output_price": 15.00,
        "shortcut": "4",
    },
    "claude-opus-4.6-thinking": {
        "name": "Claude Opus 4.6 (Thinking)",
        "input_price": 5.00,
        "output_price": 25.00,
        "shortcut": "5",
    },
    "gpt-oss-120b-medium": {
        "name": "GPT-OSS 120B (Medium)",
        "input_price": 1.00,
        "output_price": 4.00,
        "shortcut": "6",
    },
}
DEFAULT_MODEL = "gemini-3.1-pro-high"

# OUTBOUND: calibrated against user's reported quota usage (120-180%/day workday)
#   Old: tcpdump 228MB / nettop 757MB = 30.1% → produced $889/day (too high)
#   New: back-calculated from user's ~150% avg workday quota ($375 target)
#   nettop captures extensions, file sync, telemetry, code index — mostly non-API
API_RATIO_OUT = 0.10   # 10% of outbound nettop traffic is actual API requests
API_RATIO_IN  = 0.005  # 0.5% of inbound nettop traffic is API responses
API_TRAFFIC_RATIO = (API_RATIO_OUT + API_RATIO_IN) / 2  # avg for display

# === Noise Filter ===
# Idle traffic (heartbeats, telemetry, indexing) generates ~2MB/hour.
# Real API requests are 200KB-1.6MB per 30s interval.
# Skip any delta below this threshold (combined in+out bytes per poll).
MIN_DELTA_BYTES = 100_000  # 100KB — below this is idle noise

# === Quota-Based Cost Model ===
# Antigravity quotas only move in 20% increments: 0/20/40/60/80/100
# Each 20% tier is 1/5 of the 5-hour window
# We log quota snapshots to compute daily usage
QUOTA_LOG = LOG_DIR / "quota_snapshots.jsonl"

# Configurable cost-per-20% tier (subscription value, not API retail)
# User can set this based on their plan price
QUOTA_CONFIG_FILE = LOG_DIR / "quota_price.json"
DEFAULT_COST_PER_20PCT = 50.0  # $50 per 20% tier → $250 for 100%

def _load_quota_price():
    if QUOTA_CONFIG_FILE.exists():
        try:
            with open(QUOTA_CONFIG_FILE) as f:
                return json.load(f).get("cost_per_20pct", DEFAULT_COST_PER_20PCT)
        except Exception:
            pass
    return DEFAULT_COST_PER_20PCT

def poll_quota():
    """Poll tu antigravity --json and log quota snapshot if changed."""
    import re
    import ssl
    import urllib.request
    try:
        # 1. Find language_server PID, CSRF token, and random port from ps
        result = subprocess.run(["ps", "aux"], capture_output=True, text=True, timeout=5)
        servers = []
        for line in result.stdout.split("\n"):
            if "language_server_macos" in line and "--csrf_token" in line:
                csrf = re.search(r"--csrf_token\s+(\S+)", line)
                if csrf:
                    servers.append(csrf.group(1))
        if not servers:
            return None

        # 2. Find random port (language_server listens on HTTPS)
        #    Get PID of language_server and check its ports
        result2 = subprocess.run(["pgrep", "-f", "language_server_macos"], capture_output=True, text=True, timeout=5)
        pids = result2.stdout.strip().split("\n")
        ls_port = None
        for pid in pids:
            if not pid.strip():
                continue
            lsof = subprocess.run(["lsof", "-i", "-P", "-n", "-p", pid.strip()],
                                  capture_output=True, text=True, timeout=5)
            for line in lsof.stdout.split("\n"):
                if "LISTEN" in line and "language_" in line:
                    m = re.search(r":(\d+)\s", line)
                    if m:
                        ls_port = int(m.group(1))
                        break
            if ls_port:
                break

        if not ls_port:
            return None

        csrf_token = servers[0]

        # 3. Call GetUserStatus API
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        url = f"https://127.0.0.1:{ls_port}/exa.language_server_pb.LanguageServerService/GetUserStatus"
        req = urllib.request.Request(url, data=b'{}', method='POST',  headers={
            "Content-Type": "application/json",
            "x-codeium-csrf-token": csrf_token,
        })
        with urllib.request.urlopen(req, context=ctx, timeout=10) as resp:
            data = json.loads(resp.read().decode())

        # 4. Extract quota info from response
        user_status = data.get("userStatus", {})
        plan_status = user_status.get("planStatus", {})
        plan_info = plan_status.get("planInfo", {})
        cascade = user_status.get("cascadeModelConfigData", {})
        models_raw = cascade.get("clientModelConfigs", [])

        models = []
        max_used_pct = 0.0
        for m in models_raw:
            qi = m.get("quotaInfo", {})
            remaining = qi.get("remainingFraction", 1.0)
            used_pct = round((1.0 - remaining) * 100, 1)
            if used_pct > max_used_pct:
                max_used_pct = used_pct
            models.append({
                "label": m.get("label", "Unknown"),
                "remaining": round(remaining * 100, 1),
                "remaining_fraction": remaining,
                "reset_time": qi.get("resetTime", ""),
            })

        snapshot = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "plan": plan_info.get("planName", user_status.get("userTier", {}).get("name", "Unknown")),
            "primary_used_pct": max_used_pct,
            "prompt_credits": plan_status.get("availablePromptCredits", 0),
            "flow_credits": plan_status.get("availableFlowCredits", 0),
            "models": models,
        }
        # Log to file
        ensure_dir()
        with open(QUOTA_LOG, "a") as f:
            f.write(json.dumps(snapshot, ensure_ascii=False) + "\n")
        print(f"  📊 Quota: {max_used_pct:.0f}% used | {len(models)} models | credits: {snapshot['prompt_credits']}P/{snapshot['flow_credits']}F")
        return snapshot
    except Exception as e:
        print(f"  ⚠ Quota poll failed: {e}")
        return None

# === Calibration ===
# bytes-to-token ratio at TCP payload level
# Calibrated via tcpdump: 1006 bursts, 740 real pairs
CAL_FILE = LOG_DIR / "calibration_result.json"
_DEFAULT_BPT = 4.0  # bytes per token (both directions)


def _load_calibration():
    if CAL_FILE.exists():
        try:
            with open(CAL_FILE) as f:
                cal = json.load(f)
            return (
                cal.get("bytes_out_per_input_token", _DEFAULT_BPT),
                cal.get("bytes_in_per_output_token", _DEFAULT_BPT),
            )
        except Exception:
            pass
    return _DEFAULT_BPT, _DEFAULT_BPT


def _load_model():
    if MODEL_FILE.exists():
        try:
            with open(MODEL_FILE) as f:
                data = json.load(f)
            model_id = data.get("model", DEFAULT_MODEL)
            if model_id in MODELS:
                return model_id
        except Exception:
            pass
    return DEFAULT_MODEL


def _save_model(model_id):
    ensure_dir()
    with open(MODEL_FILE, "w") as f:
        json.dump({"model": model_id, "changed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}, f)


BYTES_OUT_PER_INPUT_TOKEN, BYTES_IN_PER_OUTPUT_TOKEN = _load_calibration()
CURRENT_MODEL_ID = _load_model()
CURRENT_MODEL = MODELS[CURRENT_MODEL_ID]
INPUT_PRICE_PER_M = CURRENT_MODEL["input_price"]
OUTPUT_PRICE_PER_M = CURRENT_MODEL["output_price"]


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
    except Exception:
        return None


def estimate_tokens(delta_bytes_in, delta_bytes_out):
    """Estimate token counts from network byte deltas.
    
    Applies SEPARATE API ratios for outbound (30%) and inbound (1%).
    """
    api_bytes_out = delta_bytes_out * API_RATIO_OUT
    api_bytes_in = delta_bytes_in * API_RATIO_IN

    input_tokens = max(0, int(api_bytes_out / BYTES_OUT_PER_INPUT_TOKEN))
    output_tokens = max(0, int(api_bytes_in / BYTES_IN_PER_OUTPUT_TOKEN))
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
    print(f"   Model: {CURRENT_MODEL['name']} (${INPUT_PRICE_PER_M}/M in, ${OUTPUT_PRICE_PER_M}/M out)")
    print(f"   API traffic ratio: {API_TRAFFIC_RATIO:.1%}")
    print(f"   Polling every {POLL_INTERVAL}s")
    print(f"   Log: {USAGE_LOG}")
    print(f"   PID: {os.getpid()}")

    # Save PID
    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))

    # Wait for language_server to appear (don't exit — GUI depends on this daemon)
    prev = get_nettop_snapshot()
    while not prev:
        print("⏳ Waiting for language_server_macos_arm... (retrying in 10s)")
        time.sleep(10)
        prev = get_nettop_snapshot()
    save_snapshot(prev)
    print(f"   Initial: {prev['bytes_in']:,} bytes in / {prev['bytes_out']:,} bytes out")
    print(f"   Tracking PIDs: {prev['pids']}")

    session_in = 0
    session_out = 0
    poll_count = 0
    QUOTA_POLL_EVERY = 10  # poll quota every 10 loops = 5 min at 30s interval

    # Initial quota snapshot
    poll_quota()

    while True:
        time.sleep(POLL_INTERVAL)
        poll_count += 1

        # Poll quota periodically
        if poll_count % QUOTA_POLL_EVERY == 0:
            poll_quota()

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

        # Noise filter: skip idle heartbeats/telemetry
        if (delta_in + delta_out) < MIN_DELTA_BYTES:
            prev = curr
            continue

        session_in += delta_in
        session_out += delta_out

        # Estimate tokens
        est = estimate_tokens(delta_in, delta_out)

        entry = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "model": CURRENT_MODEL_ID,
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

    # Note: daemon_loop handles missing language_server gracefully
    # Do NOT gate startup on language_server being present

    print(f"📋 Current model: {CURRENT_MODEL['name']}")
    print(f"   Switch with: python3 {sys.argv[0]} model")

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
    print(f"   Model: {CURRENT_MODEL['name']}")
    print(f"   API traffic ratio: {API_TRAFFIC_RATIO:.1%}")
    print(f"   ──────────────────────────────────")
    print(f"   Network (raw):  {total_bytes_in:,} in / {total_bytes_out:,} out")
    print(f"   Network (API):  ~{int(total_bytes_in * API_TRAFFIC_RATIO):,} in / ~{int(total_bytes_out * API_TRAFFIC_RATIO):,} out")
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

    print(f"📊 Antigravity Estimated Token Usage")
    print(f"   Model: {CURRENT_MODEL['name']} | API ratio: {API_TRAFFIC_RATIO:.1%}")
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
    print(f"\n⚠️  Estimates: {API_TRAFFIC_RATIO:.0%} API ratio × {BYTES_OUT_PER_INPUT_TOKEN:.1f} B/tok. Actual may vary ±30%.")


def cmd_model():
    """Show or switch the current model."""
    ensure_dir()

    # Quick switch: `model 3` or `model gemini-3-flash`
    if len(sys.argv) >= 3:
        choice = sys.argv[2]
        # Try shortcut number
        for mid, m in MODELS.items():
            if m["shortcut"] == choice:
                _save_model(mid)
                print(f"✅ Switched to {m['name']}")
                print(f"   ${m['input_price']}/M in, ${m['output_price']}/M out")
                print(f"\n⚠️  Restart daemon for new pricing: python3 {sys.argv[0]} stop && python3 {sys.argv[0]} start")
                return
        # Try model ID
        if choice in MODELS:
            _save_model(choice)
            m = MODELS[choice]
            print(f"✅ Switched to {m['name']}")
            print(f"   ${m['input_price']}/M in, ${m['output_price']}/M out")
            print(f"\n⚠️  Restart daemon for new pricing: python3 {sys.argv[0]} stop && python3 {sys.argv[0]} start")
            return
        print(f"❌ Unknown model: {choice}")

    # Show model list
    print(f"📋 Antigravity Model Selection")
    print(f"   Current: {CURRENT_MODEL['name']}")
    print(f"{'─' * 60}")
    print(f" {'#':<3} {'Model':<35} {'$/M in':>8} {'$/M out':>8}")
    print(f"{'─' * 60}")
    for mid, m in MODELS.items():
        marker = " ◀" if mid == CURRENT_MODEL_ID else ""
        print(f" [{m['shortcut']}] {m['name']:<35} ${m['input_price']:>6.2f} ${m['output_price']:>6.2f}{marker}")
    print(f"{'─' * 60}")
    print(f"\n💡 Quick switch:")
    print(f"   python3 {sys.argv[0]} model 1   # Gemini 3.1 Pro (High)")
    print(f"   python3 {sys.argv[0]} model 5   # Claude Opus 4.6")
    print(f"\n⚠️  After switching, restart daemon:")
    print(f"   python3 {sys.argv[0]} stop && python3 {sys.argv[0]} start")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    cmds = {
        "start": cmd_start,
        "stop": cmd_stop,
        "status": cmd_status,
        "report": cmd_report,
        "model": cmd_model,
    }
    if cmd in cmds:
        cmds[cmd]()
    else:
        print(f"Usage: {sys.argv[0]} [start|stop|status|report|model]")
        print(f"  start      — Start monitoring daemon")
        print(f"  stop       — Stop monitoring daemon")
        print(f"  status     — Show today's stats")
        print(f"  report     — Daily report")
        print(f"  model      — Show/switch model (model 1-6)")
