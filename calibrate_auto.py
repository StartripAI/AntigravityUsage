#!/usr/bin/env python3
"""
Antigravity Calibration — Automated 100M Token Experiment
Uses AppleScript to auto-type prompts into Antigravity chat,
measures nettop byte deltas to derive bytes-per-token ratios.

Usage:
    python3 calibrate_auto.py              # Full run (~1000 prompts, ~50M tokens)
    python3 calibrate_auto.py --quick      # Quick test (20 prompts)
    python3 calibrate_auto.py --report     # Show results so far
    python3 calibrate_auto.py --resume     # Resume interrupted run
"""
import json
import os
import random
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

LOG_DIR = Path.home() / ".config" / "anti-tracker"
CAL_LOG = LOG_DIR / "calibration.jsonl"
CAL_RESULT = LOG_DIR / "calibration_result.json"
PROCESS_NAME = "language_server"

# Prompt templates of varying sizes
PROMPTS = {
    "tiny": [
        "What is 2+2?",
        "Say hello",
        "Name 3 colors",
        "What day is it?",
        "Say OK",
    ],
    "small": [
        "Explain recursion in Python in 3 sentences.",
        "Write a Python function that reverses a string. Keep it simple.",
        "What is the difference between a list and a tuple? Brief answer.",
        "Explain what a REST API is in simple terms.",
        "Write a bash one-liner to count lines in a file.",
    ],
    "medium": [
        "Write a complete Python class for a binary search tree with insert, search, delete, and in-order traversal methods. Include type hints and docstrings.",
        "Design a database schema for a blog platform with users, posts, comments, and tags. Write the SQL CREATE TABLE statements for PostgreSQL.",
        "Write a React component for a todo list with add, delete, toggle complete, and filter functionality. Use hooks and TypeScript.",
        "Explain the CAP theorem and its implications for distributed systems. Give examples of databases that prioritize each combination.",
        "Write a Python script that reads a CSV file, cleans the data by removing duplicates and null values, and generates a summary report with statistics.",
    ],
    "large": [
        """I need a complete implementation of a rate limiter in Python with the following requirements:
1. Token bucket algorithm
2. Support for multiple rate limit tiers (free, pro, enterprise)
3. Redis backend for distributed rate limiting
4. Decorator pattern for easy integration with Flask/FastAPI routes
5. Sliding window fallback when Redis is unavailable
6. Comprehensive logging and metrics
7. Unit tests with pytest
8. Configuration via environment variables or YAML
Please provide the complete, production-ready implementation with all files.""",
        """Design and implement a complete authentication system with:
1. JWT-based authentication with access and refresh tokens
2. OAuth2 support (Google, GitHub, Apple)
3. Two-factor authentication (TOTP)
4. Password hashing with bcrypt
5. Rate limiting on login attempts
6. Session management with Redis
7. Role-based access control (RBAC)
8. Password reset flow with email
9. Account lockout after failed attempts
10. Audit logging
Provide complete Python implementation with FastAPI.""",
    ],
}


def get_nettop():
    """Get cumulative network bytes for language_server."""
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
        return {"bytes_in": total_in, "bytes_out": total_out, "time": time.time()}
    except Exception:
        return None


def type_in_antigravity(text):
    """Use AppleScript to type text into Antigravity chat and send it."""
    # Escape text for AppleScript
    escaped = text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")

    script = f'''
    tell application "Antigravity"
        activate
    end tell
    delay 0.5
    tell application "System Events"
        tell process "Antigravity"
            -- Focus on the chat input (Cmd+L to focus chat)
            keystroke "l" using {{command down}}
            delay 0.3
            -- Type the prompt
            keystroke "{escaped}"
            delay 0.2
            -- Send with Enter
            key code 36
        end tell
    end tell
    '''
    try:
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)
        return True
    except Exception as e:
        print(f"    ❌ AppleScript error: {e}")
        return False


def wait_for_response(before_bytes_in, timeout=120):
    """Wait until response is done (network traffic stops increasing)."""
    stable_count = 0
    last_in = before_bytes_in
    start = time.time()

    while time.time() - start < timeout:
        time.sleep(2)
        snap = get_nettop()
        if not snap:
            continue
        if snap["bytes_in"] == last_in:
            stable_count += 1
            if stable_count >= 3:  # 6 seconds of no new inbound data
                return snap
        else:
            stable_count = 0
            last_in = snap["bytes_in"]
    return get_nettop()


def estimate_tokens_from_text(text):
    """Rough token estimate: ~4 chars per token English."""
    return max(1, len(text) // 4)


def run_experiment(quick=False):
    """Run the automated calibration experiment."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    if quick:
        schedule = [("tiny", 5), ("small", 5), ("medium", 5), ("large", 2)]
    else:
        schedule = [("tiny", 200), ("small", 300), ("medium", 300), ("large", 200)]

    total_tests = sum(c for _, c in schedule)
    print(f"🔬 Antigravity Calibration — Automated Experiment")
    print(f"   Mode: {'quick (17 tests)' if quick else f'full ({total_tests} tests)'}")
    print(f"   Log: {CAL_LOG}")
    print()
    print("⚠️  This will auto-type prompts in Antigravity's chat window.")
    print("   Do NOT touch the keyboard/mouse during the experiment.")
    print("   Press Ctrl+C to stop at any time (results are saved per-prompt).")
    print()

    # Check if done count exists (for resume)
    done = 0
    if "--resume" in sys.argv and CAL_LOG.exists():
        with open(CAL_LOG) as f:
            done = sum(1 for _ in f)
        print(f"   Resuming from test #{done + 1}")

    input("Press Enter to start...")
    print()

    test_num = done
    total_input_chars = 0
    total_delta_out = 0
    total_delta_in = 0

    for bucket, count in schedule:
        prompts = PROMPTS[bucket]
        print(f"📦 Bucket: {bucket} ({count} tests)")

        for i in range(count):
            test_num += 1
            if test_num <= done:
                continue  # Skip already done

            prompt = random.choice(prompts)
            prompt_tokens_est = estimate_tokens_from_text(prompt)

            # Get nettop BEFORE
            before = get_nettop()
            if not before:
                print(f"  [{test_num}] ⚠️ nettop failed, skipping")
                continue

            # Type prompt into Antigravity
            if not type_in_antigravity(prompt):
                print(f"  [{test_num}] ⚠️ AppleScript failed, skipping")
                time.sleep(2)
                continue

            # Wait for response to complete
            after = wait_for_response(before["bytes_in"])
            if not after:
                continue

            delta_in = max(0, after["bytes_in"] - before["bytes_in"])
            delta_out = max(0, after["bytes_out"] - before["bytes_out"])
            elapsed = after["time"] - before["time"]

            total_input_chars += len(prompt)
            total_delta_out += delta_out
            total_delta_in += delta_in

            entry = {
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "test_num": test_num,
                "bucket": bucket,
                "prompt_chars": len(prompt),
                "est_input_tokens": prompt_tokens_est,
                "delta_bytes_in": delta_in,
                "delta_bytes_out": delta_out,
                "elapsed_sec": round(elapsed, 1),
                "bytes_out_per_char": round(delta_out / max(1, len(prompt)), 2),
            }

            with open(CAL_LOG, "a") as f:
                f.write(json.dumps(entry) + "\n")

            ratio = delta_out / max(1, len(prompt))
            print(
                f"  [{test_num}/{total_tests}] {bucket}: {len(prompt)} chars → "
                f"Δout={delta_out:,}B Δin={delta_in:,}B "
                f"({ratio:.1f} B/char, {elapsed:.0f}s)"
            )

            # Pace: wait 1-3s between prompts
            time.sleep(random.uniform(1.0, 3.0))

        print()

    # Analyze
    if test_num > done:
        print("=" * 50)
        analyze()


def analyze():
    """Analyze calibration data and save coefficients."""
    if not CAL_LOG.exists():
        print("❌ No calibration data")
        return

    results = []
    with open(CAL_LOG) as f:
        for line in f:
            if line.strip():
                try:
                    results.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

    valid = [r for r in results if r.get("delta_bytes_out", 0) > 500]
    if len(valid) < 5:
        print(f"⚠️ Only {len(valid)} valid samples")
        return

    # Per-bucket analysis
    from collections import defaultdict
    by_bucket = defaultdict(list)
    for r in valid:
        by_bucket[r.get("bucket", "unknown")].append(r)

    print(f"\n📊 Calibration Results ({len(valid)} valid samples)")
    print("=" * 60)

    all_ratios_out = []
    all_ratios_in = []

    for bucket in ["tiny", "small", "medium", "large"]:
        if bucket not in by_bucket:
            continue
        br = by_bucket[bucket]
        ratios = [r["delta_bytes_out"] / max(1, r["prompt_chars"]) for r in br]
        avg = sum(ratios) / len(ratios)
        all_ratios_out.extend(ratios)

        in_ratios = [r["delta_bytes_in"] / max(1, r.get("est_input_tokens", 50)) for r in br]
        all_ratios_in.extend(in_ratios)

        print(f"  {bucket:>8}: {len(br):>4} samples, avg {avg:.2f} B/char ({avg * 4:.1f} B/token)")

    # Remove outliers
    def trim(data, factor=2):
        if len(data) < 5:
            return data
        mean = sum(data) / len(data)
        std = (sum((x - mean) ** 2 for x in data) / len(data)) ** 0.5
        return [x for x in data if abs(x - mean) < factor * std]

    trimmed_out = trim(all_ratios_out)
    trimmed_in = trim(all_ratios_in)

    avg_out_per_char = sum(trimmed_out) / max(1, len(trimmed_out))
    avg_out_per_token = avg_out_per_char * 4  # ~4 chars per token
    std_out = (sum((x - avg_out_per_char) ** 2 for x in trimmed_out) / max(1, len(trimmed_out))) ** 0.5

    avg_in = sum(trimmed_in) / max(1, len(trimmed_in)) if trimmed_in else 5.5

    print(f"\n{'=' * 60}")
    print(f"  Bytes_out per input char:   {avg_out_per_char:.3f} (±{std_out:.3f})")
    print(f"  Bytes_out per input token:  {avg_out_per_token:.3f} (±{std_out * 4:.3f})")
    print(f"  Bytes_in per output token:  {avg_in:.3f}")
    print(f"{'=' * 60}")

    cal = {
        "bytes_out_per_input_token": round(avg_out_per_token, 3),
        "bytes_in_per_output_token": round(avg_in, 3),
        "bytes_out_per_input_char": round(avg_out_per_char, 3),
        "std_out_per_char": round(std_out, 3),
        "samples": len(valid),
        "calibrated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "per_bucket": {b: len(rs) for b, rs in by_bucket.items()},
    }
    with open(CAL_RESULT, "w") as f:
        json.dump(cal, f, indent=2)
    print(f"\n✅ Saved to {CAL_RESULT}")
    print(f"   anti_estimator.py will auto-load on next start.")


if __name__ == "__main__":
    if "--report" in sys.argv:
        analyze()
    elif "--quick" in sys.argv:
        run_experiment(quick=True)
    else:
        run_experiment(quick=False)
