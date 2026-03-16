#!/usr/bin/env python3
"""
Antigravity Token Estimator — Interactive Calibration
Measures nettop byte deltas while user sends known prompts through the Antigravity UI.
Uses the deltas to derive accurate bytes-per-token calibration coefficients.

Usage:
    python3 calibrate.py             # Interactive calibration (guided)
    python3 calibrate.py --auto      # Auto-calibrate from existing nettop data
    python3 calibrate.py --report    # Show calibration results
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

LOG_DIR = Path.home() / ".config" / "anti-tracker"
CAL_LOG = LOG_DIR / "calibration.jsonl"
CAL_RESULT = LOG_DIR / "calibration_result.json"
PROCESS_NAME = "language_server"

# Known test prompts of varying sizes (char count → ~token estimate)
TEST_PROMPTS = [
    {
        "name": "tiny",
        "chars": 50,
        "est_tokens": 13,
        "text": "What is 2+2? Reply with just the number.",
        "reply_instruction": "Wait for the response, then press Enter.",
    },
    {
        "name": "small",
        "chars": 200,
        "est_tokens": 50,
        "text": "Explain the difference between a list and a tuple in Python. Include examples of when to use each one, and mention their key performance characteristics.",
        "reply_instruction": "Wait for the full response, then press Enter.",
    },
    {
        "name": "medium",
        "chars": 1000,
        "est_tokens": 250,
        "text": """Write a Python function that implements binary search on a sorted array. The function should:
1. Take a sorted list and a target value as parameters
2. Return the index of the target if found, or -1 if not found
3. Handle edge cases like empty arrays and single-element arrays
4. Include proper type hints
5. Add comprehensive docstring with examples
6. Also write unit tests using pytest that cover all edge cases including:
   - Empty array
   - Single element (found and not found)
   - Multiple elements (found at start, middle, end)
   - Target not in array (smaller than all, larger than all, between elements)
   - Duplicate elements
   - Large arrays (1000+ elements)""",
        "reply_instruction": "Wait for the FULL response to finish generating, then press Enter.",
    },
    {
        "name": "large",
        "chars": 4000,
        "est_tokens": 1000,
        "text": """I need you to design a complete REST API for a task management system. Include the following requirements:

## Data Models
1. User: id, name, email, role (admin/member), created_at
2. Project: id, name, description, owner_id, members[], status, created_at
3. Task: id, title, description, project_id, assignee_id, reporter_id, status (todo/in_progress/review/done), priority (low/medium/high/critical), due_date, tags[], comments[], created_at, updated_at
4. Comment: id, task_id, author_id, content, created_at

## Required Endpoints
For each endpoint provide: HTTP method, URL path, request body schema, response schema, status codes, and authorization rules.

### User Management
- Register, login, get profile, update profile, list users

### Project Management
- CRUD operations, add/remove members, list projects for user

### Task Management
- CRUD operations, assign task, change status, filter/search tasks, bulk update
- Advanced: kanban board view, task dependencies, subtasks

### Comments
- Add, edit, delete comments on tasks

### Analytics
- Tasks per user, overdue tasks, project progress, velocity metrics

## Additional Requirements
- Pagination for all list endpoints
- Rate limiting strategy
- Error response format
- Webhook support for task updates
- File attachment support for tasks
- Activity log/audit trail

Please provide the complete API specification.""",
        "reply_instruction": "This will generate a LONG response. Wait until FULLY complete, then press Enter.",
    },
]


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


def interactive_calibration():
    """Guided interactive calibration."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    print("🔬 Antigravity Calibration — Interactive Mode")
    print("=" * 50)
    print()
    print("Instructions:")
    print("  1. Open Antigravity and start a new chat")
    print("  2. For each test, COPY the prompt text and PASTE into Antigravity")
    print("  3. SEND the message and WAIT for the full response")
    print("  4. Come back here and press Enter")
    print()
    print(f"  {len(TEST_PROMPTS)} tests, will take ~5-10 minutes")
    print()
    input("Press Enter when ready to begin...")
    print()

    results = []
    for i, test in enumerate(TEST_PROMPTS):
        print(f"━━━ Test {i+1}/{len(TEST_PROMPTS)}: {test['name']} ({test['chars']} chars, ~{test['est_tokens']} tokens) ━━━")
        print()
        print("📋 Copy this prompt and paste into Antigravity:")
        print("─" * 40)
        print(test["text"])
        print("─" * 40)
        print()

        input("Press Enter AFTER you've PASTED the prompt (before sending)...")

        # Snapshot nettop BEFORE
        before = get_nettop()
        if not before:
            print("⚠️  nettop failed, skipping")
            continue

        print("✅ Baseline captured. Now SEND the message in Antigravity.")
        print(f"   {test['reply_instruction']}")

        input("Press Enter AFTER the response is FULLY generated...")

        # Small delay for nettop to update
        time.sleep(1)

        # Snapshot nettop AFTER
        after = get_nettop()
        if not after:
            print("⚠️  nettop failed, skipping")
            continue

        delta_in = max(0, after["bytes_in"] - before["bytes_in"])
        delta_out = max(0, after["bytes_out"] - before["bytes_out"])
        elapsed = after["time"] - before["time"]

        # Ask for approximate response length
        resp_len = input("Roughly how long was the response? (short/medium/long/very_long): ").strip().lower()
        est_output_tokens = {"short": 50, "medium": 200, "long": 500, "very_long": 2000}.get(resp_len, 200)

        entry = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "test_name": test["name"],
            "prompt_chars": test["chars"],
            "est_input_tokens": test["est_tokens"],
            "est_output_tokens": est_output_tokens,
            "delta_bytes_in": delta_in,
            "delta_bytes_out": delta_out,
            "elapsed_sec": round(elapsed, 1),
            "bytes_per_input_char": round(delta_out / max(1, test["chars"]), 2),
        }
        results.append(entry)

        with open(CAL_LOG, "a") as f:
            f.write(json.dumps(entry) + "\n")

        print(f"\n📊 Result:")
        print(f"   Δ bytes out: {delta_out:,} ({delta_out/max(1,test['chars']):.1f} B/char)")
        print(f"   Δ bytes in:  {delta_in:,} ({delta_in/max(1,est_output_tokens):.1f} B/est_out_tok)")
        print(f"   Elapsed: {elapsed:.1f}s")
        print()

    if results:
        print("\n" + "=" * 50)
        analyze_results(results)
    else:
        print("❌ No results collected")


def auto_calibrate():
    """Auto-calibrate from existing nettop usage data."""
    usage_log = LOG_DIR / "nettop_usage.jsonl"
    if not usage_log.exists():
        print("❌ No nettop usage data. Run the estimator daemon first.")
        return

    entries = []
    with open(usage_log) as f:
        for line in f:
            if line.strip():
                try:
                    e = json.loads(line)
                    if e.get("source") != "historical_estimate" and e.get("delta_bytes_out", 0) > 1000:
                        entries.append(e)
                except json.JSONDecodeError:
                    continue

    if len(entries) < 3:
        print(f"⚠️  Only {len(entries)} real data points. Need more usage data.")
        print("   Use Antigravity for a while with the estimator running.")
        return

    print(f"📊 Auto-calibrating from {len(entries)} real data points")
    # Use the existing data to refine ratios
    analyze_results(entries, auto=True)


def analyze_results(results=None, auto=False):
    """Analyze calibration data and save coefficients."""
    if results is None:
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

    if not results:
        print("❌ No results")
        return

    # Calculate ratios
    if auto:
        # From nettop data, we only have bytes — use the deltas to infer ratios
        # bytes_out correlates with input, bytes_in correlates with output
        ratios_out = [r["delta_bytes_out"] / max(1, r.get("input_tokens_est", 1)) for r in results]
        ratios_in = [r["delta_bytes_in"] / max(1, r.get("output_tokens_est", 1)) for r in results]
    else:
        ratios_out = [r["delta_bytes_out"] / max(1, r.get("est_input_tokens", r.get("prompt_chars", 1) / 4)) for r in results]
        ratios_in = [r["delta_bytes_in"] / max(1, r.get("est_output_tokens", 200)) for r in results]

    # Remove outliers (>2 std dev)
    def remove_outliers(data):
        if len(data) < 5:
            return data
        mean = sum(data) / len(data)
        std = (sum((x - mean) ** 2 for x in data) / len(data)) ** 0.5
        return [x for x in data if abs(x - mean) < 2 * std]

    ratios_out = remove_outliers(ratios_out)
    ratios_in = remove_outliers(ratios_in)

    avg_out = sum(ratios_out) / max(1, len(ratios_out))
    avg_in = sum(ratios_in) / max(1, len(ratios_in))
    std_out = (sum((x - avg_out) ** 2 for x in ratios_out) / max(1, len(ratios_out))) ** 0.5
    std_in = (sum((x - avg_in) ** 2 for x in ratios_in) / max(1, len(ratios_in))) ** 0.5

    print(f"\n📊 Calibration Results ({len(results)} samples)")
    print(f"{'=' * 50}")
    print(f"  bytes_out per input token:  {avg_out:.2f} (±{std_out:.2f})")
    print(f"  bytes_in per output token:  {avg_in:.2f} (±{std_in:.2f})")
    print(f"{'=' * 50}")

    cal = {
        "bytes_out_per_input_token": round(avg_out, 3),
        "bytes_in_per_output_token": round(avg_in, 3),
        "std_out": round(std_out, 3),
        "std_in": round(std_in, 3),
        "samples": len(results),
        "calibrated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    with open(CAL_RESULT, "w") as f:
        json.dump(cal, f, indent=2)
    print(f"\n✅ Saved to {CAL_RESULT}")
    print(f"   anti_estimator.py will auto-load these values.")


if __name__ == "__main__":
    if "--report" in sys.argv:
        analyze_results()
    elif "--auto" in sys.argv:
        auto_calibrate()
    else:
        interactive_calibration()
