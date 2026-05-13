#!/usr/bin/env python3
"""
star-tokens — Unified AI Token Usage Dashboard (Native GUI)
Uses pywebview for native macOS window with liquid glass UI.

Features:
  - Daily / Weekly / Monthly time range tabs (default: Monthly)
  - Codex CLI (precise) + Antigravity (estimated) unified view
  - Date range label showing selected period

Usage:
    python3 star_tokens.py
"""
import http.server
import json
import os
import subprocess
import sys
import threading
import time
import webbrowser
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from anti_estimator import select_primary_quota_model, used_pct_from_quota_model

PORT = 18877  # Internal port for API
ANTI_LOG = Path.home() / ".config" / "anti-tracker" / "nettop_usage.jsonl"
MODEL_FILE = Path.home() / ".config" / "anti-tracker" / "current_model.json"
ESTIMATOR_SCRIPT = Path(__file__).parent / "anti_estimator.py"
CLAUDE_PROJECT_DIRS = [
    Path.home() / ".config" / "claude" / "projects",
    Path.home() / ".claude" / "projects",
]
TOKEN_FIELDS = [
    "input_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
]
COST_FIELDS = ["cost_usd"]

# Load model info
MODELS = {
    "gemini-3.1-pro-high": {"name": "Gemini 3.1 Pro (High)", "input_price": 1.25, "output_price": 10.00},
    "gemini-3.1-pro-low": {"name": "Gemini 3.1 Pro (Low)", "input_price": 0.30, "output_price": 2.50},
    "gemini-3-flash": {"name": "Gemini 3 Flash", "input_price": 0.10, "output_price": 0.40},
    "claude-sonnet-4.6-thinking": {"name": "Claude Sonnet 4.6 (Thinking)", "input_price": 3.00, "output_price": 15.00},
    "claude-opus-4.6-thinking": {"name": "Claude Opus 4.6 (Thinking)", "input_price": 5.00, "output_price": 25.00},
    "gpt-oss-120b-medium": {"name": "GPT-OSS 120B (Medium)", "input_price": 1.00, "output_price": 4.00},
}

def _get_current_model():
    if MODEL_FILE.exists():
        try:
            with open(MODEL_FILE) as f:
                data = json.load(f)
            mid = data.get("model", "claude-opus-4.6-thinking")
            if mid in MODELS:
                return mid, MODELS[mid]
        except Exception:
            pass
    return "claude-opus-4.6-thinking", MODELS["claude-opus-4.6-thinking"]

ANTI_MODEL_ID, ANTI_MODEL = _get_current_model()
ANTI_INPUT_PRICE = ANTI_MODEL["input_price"]
ANTI_OUTPUT_PRICE = ANTI_MODEL["output_price"]

# Quota-Based Cost Model 
# Quota moves in 20% steps. Each 20% = $COST_PER_20PCT in subscription value.
QUOTA_CONFIG = Path.home() / ".config" / "anti-tracker" / "quota_price.json"
QUOTA_LOG = Path.home() / ".config" / "anti-tracker" / "quota_snapshots.jsonl"

def _get_quota_price():
    if QUOTA_CONFIG.exists():
        try:
            with open(QUOTA_CONFIG) as f:
                return json.load(f).get("cost_per_20pct", 10.0)
        except Exception: pass
    return 50.0  # $50 per 20% tier → $250 for full 100%

COST_PER_20PCT = _get_quota_price()
_CLAUDE_VALIDATION_CACHE = {"at": 0.0, "value": None}
_CLAUDE_LOCAL_CACHE = {"at": 0.0, "value": None}
CLAUDE_MISMATCH_THRESHOLD_PCT = 2.0


def _safe_number(value, default=0):
    return value if isinstance(value, (int, float)) else default


def _snapshot_primary_used_pct(snapshot):
    models = snapshot.get("models", [])
    primary = select_primary_quota_model(models)
    if primary:
        return used_pct_from_quota_model(primary)
    return float(snapshot.get("primary_used_pct") or 0.0)


def get_quota_usage_for_date(date_str):
    """Calculate Anti cost from quota snapshots for a given date.
    
    Accumulates quota usage across 5h window resets.
    When usage drops significantly (reset detected), the consumed
    amount from the previous window is banked and accumulation
    continues from the new baseline.
    
    Example: 80% → reset → 60% → reset → 40% = 80+60+40 = 180% total
    """
    snapshots = []  # (timestamp, used_pct) sorted by time
    if QUOTA_LOG.exists():
        with open(QUOTA_LOG) as f:
            for line in f:
                if line.strip():
                    try:
                        snap = json.loads(line)
                        if snap["timestamp"].startswith(date_str):
                            used = _snapshot_primary_used_pct(snap)
                            snapshots.append((snap["timestamp"], used))
                    except Exception:
                        continue
    if not snapshots:
        return 0.0, 0.0, False
    
    # Sort by timestamp
    snapshots.sort(key=lambda x: x[0])
    
    # Accumulate across window resets
    # A "reset" = usage drops by >15% between consecutive snapshots
    total_consumed = 0.0
    prev_used = 0.0
    window_peak = 0.0
    
    for _, used in snapshots:
        if used < prev_used - 15:  # reset detected (usage dropped significantly)
            total_consumed += window_peak  # bank the peak of the ended window
            window_peak = used  # start new window tracking
        else:
            window_peak = max(window_peak, used)
        prev_used = used
    
    # Add current window's peak
    total_consumed += window_peak
    
    # Interpolate: each 20% tier = COST_PER_20PCT
    tiers_used = total_consumed / 20.0
    return round(tiers_used * COST_PER_20PCT, 2), total_consumed, True


def get_quota_cost_for_date(date_str):
    cost, used_pct, _ = get_quota_usage_for_date(date_str)
    return cost, used_pct


def _get_codex_dedup_ratios():
    """Calculate per-day dedup ratio from state_5.sqlite to correct overcounting.
    
    Two sources of overcounting:
    1. Worktrees: same conversation runs in parallel across worktrees
    2. Restarts: same conversation restarted → new session re-reports ALL accumulated context
    
    Fix: group all sessions by TITLE per day, take MAX tokens (the final session
    has the most accumulated tokens = the real total usage for that conversation).
    """
    import sqlite3
    db_path = Path.home() / ".codex" / "state_5.sqlite"
    if not db_path.exists():
        return {}
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        rows = conn.execute(
            "SELECT created_at, tokens_used, title FROM threads"
        ).fetchall()
        conn.close()
    except Exception:
        return {}
    
    from collections import defaultdict
    from datetime import datetime as _dt, timezone as _tz
    
    # Group by (day, title) → take max tokens
    day_title_max = defaultdict(lambda: defaultdict(int))
    day_raw = defaultdict(int)
    
    for created_at, tokens, title in rows:
        day = _dt.fromtimestamp(created_at, tz=_tz.utc).strftime("%Y-%m-%d")
        day_raw[day] += tokens
        if tokens > day_title_max[day][title]:
            day_title_max[day][title] = tokens
    
    ratios = {}
    for day in day_raw:
        deduped = sum(day_title_max[day].values())
        if day_raw[day] > 0:
            ratios[day] = deduped / day_raw[day]
    return ratios


def _scale_usage_counts(container, ratio):
    for key in TOKEN_FIELDS + COST_FIELDS:
        if key in container:
            container[key] = round(container[key] * ratio) if key in TOKEN_FIELDS else round(container[key] * ratio, 4)


def _run_tu_report(source):
    try:
        result = subprocess.run(["tu", source, "--json"], capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            return json.loads(result.stdout)
    except Exception:
        pass
    return {"daily": []}


def _apply_codex_dedup_ratios(data):
    ratios = _get_codex_dedup_ratios()
    for day in data.get("daily", []):
        r = ratios.get(day.get("date", ""), 1.0)
        if r >= 1.0:
            continue
        _scale_usage_counts(day.get("totals", {}), r)
        for model_totals in day.get("models", {}).values():
            _scale_usage_counts(model_totals, r)
        for source_totals in day.get("sources", {}).values():
            _scale_usage_counts(source_totals, r)
    if "totals" in data:
        totals = defaultdict(float)
        for day in data.get("daily", []):
            for key in TOKEN_FIELDS + COST_FIELDS:
                totals[key] += day.get("totals", {}).get(key, 0)
        data["totals"] = {
            key: round(value) if key in TOKEN_FIELDS else round(value, 4)
            for key, value in totals.items()
        }
    return data


def get_codex_data():
    return _apply_codex_dedup_ratios(_run_tu_report("codex"))


def get_claude_tu_data():
    return _run_tu_report("claude")


def _report_total_tokens(report):
    return _sum_report_totals(report).get("total_tokens", 0)


def _total_delta_pct(left, right):
    return round(((left - right) / right * 100.0), 2) if right else 0.0


def get_claude_local_data():
    now = time.time()
    if _CLAUDE_LOCAL_CACHE["value"] is not None and now - _CLAUDE_LOCAL_CACHE["at"] < 60:
        return _CLAUDE_LOCAL_CACHE["value"]
    files = _discover_claude_usage_files()
    data = scan_claude_usage_files(files) if files else {"daily": [], "totals": {}, "stats": {"files_discovered": 0}}
    data["source"] = "local_jsonl_dedup"
    _CLAUDE_LOCAL_CACHE["at"] = now
    _CLAUDE_LOCAL_CACHE["value"] = data
    return data


def get_claude_data(tu_data=None):
    tu_data = tu_data or get_claude_tu_data()
    local = get_claude_local_data()
    local_total = _report_total_tokens(local)
    tu_total = _report_total_tokens(tu_data)
    delta_pct = abs(_total_delta_pct(local_total, tu_total))
    if local_total and tu_total and delta_pct > CLAUDE_MISMATCH_THRESHOLD_PCT:
        local["tu_total_tokens"] = tu_total
        local["tu_delta_pct"] = _total_delta_pct(local_total, tu_total)
        return local
    tu_data["source"] = "tu_claude"
    return tu_data


def _extract_nested(obj, path):
    cur = obj
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _extract_usage_value(entry, keys):
    for key in keys:
        value = _extract_nested(entry, key)
        if isinstance(value, (int, float)):
            return int(value)
    return 0


def _extract_claude_usage(entry):
    return {
        "input_tokens": _extract_usage_value(entry, ["message.usage.input_tokens", "usage.input_tokens", "input_tokens"]),
        "cache_creation_input_tokens": _extract_usage_value(entry, [
            "message.usage.cache_creation_input_tokens",
            "usage.cache_creation_input_tokens",
            "cache_creation_input_tokens",
        ]),
        "cache_read_input_tokens": _extract_usage_value(entry, [
            "message.usage.cache_read_input_tokens",
            "usage.cache_read_input_tokens",
            "usage.cached_input_tokens",
            "cache_read_input_tokens",
            "cached_input_tokens",
        ]),
        "output_tokens": _extract_usage_value(entry, ["message.usage.output_tokens", "usage.output_tokens", "output_tokens"]),
        "reasoning_output_tokens": _extract_usage_value(entry, [
            "message.usage.reasoning_output_tokens",
            "usage.reasoning_output_tokens",
            "usage.output_tokens_details.reasoning_tokens",
            "output_tokens_details.reasoning_tokens",
        ]),
        "cost_usd": float(_safe_number(entry.get("costUSD"), 0.0)),
    }


def _claude_model(entry):
    for path in ["message.model", "usage.model", "model", "message.metadata.model"]:
        value = _extract_nested(entry, path)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "unknown"


def _claude_model_prices(model):
    model = (model or "").lower()
    if "opus" in model:
        return 5.0, 25.0
    if "sonnet" in model:
        return 3.0, 15.0
    if "haiku" in model:
        return 1.0, 5.0
    return 0.0, 0.0


def _claude_cache_creation_tokens(entry, usage):
    creation = _extract_nested(entry, "message.usage.cache_creation") or _extract_nested(entry, "usage.cache_creation")
    total = usage.get("cache_creation_input_tokens", 0)
    if not isinstance(creation, dict):
        return total, 0, 0
    five_min = int(_safe_number(creation.get("ephemeral_5m_input_tokens"), 0))
    one_hour = int(_safe_number(creation.get("ephemeral_1h_input_tokens"), 0))
    accounted = five_min + one_hour
    unclassified = max(total - accounted, 0)
    return unclassified, five_min, one_hour


def _estimate_claude_cost(entry, model, usage):
    recorded = _safe_number(entry.get("costUSD"), 0.0)
    if recorded > 0:
        return float(recorded)

    input_price, output_price = _claude_model_prices(model)
    if input_price <= 0 and output_price <= 0:
        return 0.0

    input_cost = usage.get("input_tokens", 0) * input_price
    cache_write_cost = usage.get("cache_creation_input_tokens", 0) * input_price * 1.25
    cache_read_cost = usage.get("cache_read_input_tokens", 0) * input_price * 0.1
    output_cost = usage.get("output_tokens", 0) * output_price
    return (input_cost + cache_write_cost + cache_read_cost + output_cost) / 1_000_000


def _add_counts(target, usage):
    for key in TOKEN_FIELDS + COST_FIELDS:
        target[key] += usage.get(key, 0)


def scan_claude_usage_files(files):
    daily = defaultdict(lambda: {
        "totals": defaultdict(float),
        "models": defaultdict(lambda: defaultdict(float)),
    })
    stats = {
        "files_discovered": len(files),
        "lines_total": 0,
        "lines_parsed": 0,
        "deduped_entries": 0,
        "lines_invalid_json": 0,
        "lines_missing_usage": 0,
    }
    seen = set()

    for file_path in files:
        try:
            with open(file_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    stats["lines_total"] += 1
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        stats["lines_invalid_json"] += 1
                        continue
                    if entry.get("type") not in (None, "assistant"):
                        continue
                    usage = _extract_claude_usage(entry)
                    model = _claude_model(entry)
                    usage["total_tokens"] = (
                        usage["input_tokens"]
                        + usage["cache_creation_input_tokens"]
                        + usage["cache_read_input_tokens"]
                        + usage["output_tokens"]
                    )
                    usage["cost_usd"] = _estimate_claude_cost(entry, model, usage)
                    if usage["total_tokens"] == 0:
                        stats["lines_missing_usage"] += 1
                        continue

                    message_id = _extract_nested(entry, "message.id") or entry.get("messageId")
                    request_id = entry.get("requestId") or entry.get("request_id")
                    if message_id and request_id:
                        key = f"{message_id}:{request_id}"
                        if key in seen:
                            stats["deduped_entries"] += 1
                            continue
                        seen.add(key)

                    timestamp = entry.get("timestamp", "")
                    date = timestamp[:10] if len(timestamp) >= 10 else "unknown"
                    _add_counts(daily[date]["totals"], usage)
                    _add_counts(daily[date]["models"][model], usage)
                    stats["lines_parsed"] += 1
        except OSError:
            continue

    rows = []
    totals = defaultdict(float)
    for date in sorted(daily.keys()):
        row = daily[date]
        clean_totals = {
            key: round(value) if key in TOKEN_FIELDS else round(value, 6)
            for key, value in row["totals"].items()
        }
        clean_models = {
            model: {
                key: round(value) if key in TOKEN_FIELDS else round(value, 6)
                for key, value in counts.items()
            }
            for model, counts in row["models"].items()
        }
        rows.append({"date": date, "totals": clean_totals, "models": clean_models})
        for key, value in clean_totals.items():
            totals[key] += value

    return {
        "daily": rows,
        "totals": {
            key: round(value) if key in TOKEN_FIELDS else round(value, 6)
            for key, value in totals.items()
        },
        "stats": stats,
    }


def _discover_claude_usage_files():
    files = []
    for root in CLAUDE_PROJECT_DIRS:
        if root.exists():
            files.extend(root.rglob("*.jsonl"))
    return files


def _sum_report_totals(report):
    totals = defaultdict(float)
    for day in report.get("daily", []):
        for key in TOKEN_FIELDS + COST_FIELDS:
            totals[key] += day.get("totals", {}).get(key, 0)
    if report.get("totals"):
        for key in TOKEN_FIELDS + COST_FIELDS:
            if key in report["totals"]:
                totals[key] = report["totals"][key]
    return {
        key: round(value) if key in TOKEN_FIELDS else round(value, 6)
        for key, value in totals.items()
    }


def get_claude_validation_summary(claude_data=None, tu_data=None):
    now = time.time()
    if _CLAUDE_VALIDATION_CACHE["value"] is not None and now - _CLAUDE_VALIDATION_CACHE["at"] < 300:
        return _CLAUDE_VALIDATION_CACHE["value"]

    local = get_claude_local_data()
    if not local.get("daily"):
        return None

    tu_totals = _sum_report_totals(tu_data or get_claude_tu_data())
    active_totals = _sum_report_totals(claude_data or local)
    local_total = local.get("totals", {}).get("total_tokens", 0)
    tu_total = tu_totals.get("total_tokens", 0)
    delta = local_total - tu_total
    delta_pct = _total_delta_pct(local_total, tu_total)
    summary = {
        "source": "local_jsonl_dedup_vs_tu_claude",
        "active_source": (claude_data or local).get("source", "unknown"),
        "active_total_tokens": active_totals.get("total_tokens", 0),
        "local_total_tokens": local_total,
        "local_cost": local.get("totals", {}).get("cost_usd", 0),
        "tu_total_tokens": tu_total,
        "tu_cost": tu_totals.get("cost_usd", 0),
        "delta_tokens": delta,
        "delta_pct": delta_pct,
        "mismatch_threshold_pct": CLAUDE_MISMATCH_THRESHOLD_PCT,
        "stats": local.get("stats", {}),
    }
    _CLAUDE_VALIDATION_CACHE["at"] = now
    _CLAUDE_VALIDATION_CACHE["value"] = summary
    return summary


def _provider_entry(day):
    totals = day.get("totals", {})
    models = day.get("models", {})
    return {
        "input_tokens": totals.get("input_tokens", 0),
        "cache_creation_tokens": totals.get("cache_creation_input_tokens", 0),
        "cached_tokens": totals.get("cache_read_input_tokens", 0),
        "output_tokens": totals.get("output_tokens", 0),
        "reasoning_tokens": totals.get("reasoning_output_tokens", 0),
        "total_tokens": totals.get("total_tokens", 0),
        "cost": round(totals.get("cost_usd", 0), 2),
        "models": sorted(
            models.keys(),
            key=lambda model: models[model].get("total_tokens", 0),
            reverse=True,
        ),
    }


def get_anti_data():
    entries = []
    if ANTI_LOG.exists():
        with open(ANTI_LOG) as f:
            for line in f:
                if line.strip():
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
    daily = defaultdict(lambda: {"input_tokens": 0, "output_tokens": 0, "bytes_in": 0, "bytes_out": 0, "cost": 0})
    for e in entries:
        date = e.get("timestamp", "")[:10]
        d = daily[date]
        d["input_tokens"] += e.get("input_tokens_est", 0)
        d["output_tokens"] += e.get("output_tokens_est", 0)
        d["bytes_in"] += e.get("delta_bytes_in", 0)
        d["bytes_out"] += e.get("delta_bytes_out", 0)
        d["cost"] += e.get("total_cost_est", 0)

    for date, d in daily.items():
        quota_cost, used_pct, has_quota = get_quota_usage_for_date(date)
        raw_cost = d["cost"]
        raw_input = d["input_tokens"]
        raw_output = d["output_tokens"]
        d["raw_cost"] = raw_cost
        d["raw_input_tokens"] = raw_input
        d["raw_output_tokens"] = raw_output
        d["quota_used_pct"] = used_pct
        d["capped"] = False
        if has_quota and used_pct <= 0:
            d["cost"] = 0.0
            d["input_tokens"] = 0
            d["output_tokens"] = 0
            d["cost_source"] = "quota_zero"
            d["suppressed_raw_estimate"] = raw_input > 0 or raw_output > 0 or raw_cost > 0
        elif has_quota and quota_cost > 0:
            d["cost"] = quota_cost
            d["cost_source"] = "quota"
        else:
            d["cost_source"] = "nettop"
    return dict(daily)


def get_anti_status():
    pid_file = Path.home() / ".config" / "anti-tracker" / "estimator.pid"
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 0)
            return {"running": True, "pid": pid}
        except (ProcessLookupError, ValueError):
            pass
    return {"running": False, "pid": None}


def build_api_response():
    codex = get_codex_data()
    claude_tu = get_claude_tu_data()
    claude = get_claude_data(claude_tu)
    anti = get_anti_data()
    anti_status = get_anti_status()
    all_dates = set()
    codex_by_date = {}
    claude_by_date = {}
    for day in codex.get("daily", []):
        date = day.get("date", "")
        if date:
            codex_by_date[date] = day
            all_dates.add(date)
    for day in claude.get("daily", []):
        date = day.get("date", "")
        if date:
            claude_by_date[date] = day
            all_dates.add(date)
    all_dates.update(anti.keys())
    combined = []
    for date in sorted(all_dates):
        entry = {"date": date, "codex": None, "claude": None, "antigravity": None}
        if date in codex_by_date:
            entry["codex"] = _provider_entry(codex_by_date[date])
        if date in claude_by_date:
            entry["claude"] = _provider_entry(claude_by_date[date])
        if date in anti:
            ad = anti[date]
            ac = ad["cost"]
            at = ad["input_tokens"] + ad["output_tokens"]
            entry["antigravity"] = {
                "input_tokens": ad["input_tokens"],
                "output_tokens": ad["output_tokens"],
                "total_tokens": at,
                "cost": round(ac, 2),
                "bytes_in": ad["bytes_in"],
                "bytes_out": ad["bytes_out"],
                "model": ANTI_MODEL["name"],
                "estimated": True,
                "capped": ad.get("capped", False),
                "raw_cost": round(ad.get("raw_cost", ac), 2),
                "raw_input_tokens": ad.get("raw_input_tokens", ad["input_tokens"]),
                "raw_output_tokens": ad.get("raw_output_tokens", ad["output_tokens"]),
                "quota_used_pct": ad.get("quota_used_pct", 0),
                "cost_source": ad.get("cost_source", "nettop"),
                "suppressed_raw_estimate": ad.get("suppressed_raw_estimate", False),
            }
        combined.append(entry)
    return {
        "daily": combined,
        "anti_estimator": anti_status,
        "claude_validation": get_claude_validation_summary(claude, claude_tu),
        "model": ANTI_MODEL["name"],
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Star Tokens</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{--radius:20px;--blur:24px;--t:0.4s cubic-bezier(.4,0,.2,1)}
[data-theme="dark"]{
  --bg:#050510;--surface:rgba(255,255,255,0.04);--glass:rgba(255,255,255,0.06);
  --gb:rgba(255,255,255,0.08);--gh:rgba(255,255,255,0.1);
  --text:#f0f0f5;--t2:#8b8ba3;--t3:#55556a;
  --accent:#6c63ff;--a2:#a78bfa;--green:#34d399;--gbg:rgba(52,211,153,0.1);
  --orange:#fb923c;--obg:rgba(251,146,60,0.1);--cyan:#22d3ee;--red:#f87171;
  --o1:#6c63ff;--o2:#ec4899;--o3:#06b6d4;--o4:#f59e0b;
  --sh:0 8px 32px rgba(0,0,0,0.4);
}
[data-theme="light"]{
  --bg:#f0f2f5;--surface:rgba(255,255,255,0.6);--glass:rgba(255,255,255,0.7);
  --gb:rgba(0,0,0,0.06);--gh:rgba(255,255,255,0.85);
  --text:#1a1a2e;--t2:#64648a;--t3:#9e9eb8;
  --accent:#6c63ff;--a2:#8b5cf6;--green:#059669;--gbg:rgba(5,150,105,0.08);
  --orange:#ea580c;--obg:rgba(234,88,12,0.08);--cyan:#0891b2;--red:#dc2626;
  --o1:#818cf8;--o2:#f472b6;--o3:#22d3ee;--o4:#fbbf24;
  --sh:0 8px 32px rgba(0,0,0,0.08);
}
body{font-family:'Inter',sans-serif;background:var(--bg);color:var(--text);min-height:100vh;overflow-x:hidden;transition:background var(--t),color var(--t);-webkit-user-select:none;user-select:none}
.dbg{position:fixed;inset:0;z-index:0;overflow:hidden;pointer-events:none}
.orb{position:absolute;border-radius:50%;filter:blur(100px);opacity:0.35;animation:drift 20s ease-in-out infinite alternate}
[data-theme="light"] .orb{opacity:0.18;filter:blur(120px)}
.orb:nth-child(1){width:600px;height:600px;background:var(--o1);top:-10%;left:-5%;animation-duration:25s}
.orb:nth-child(2){width:500px;height:500px;background:var(--o2);top:40%;right:-10%;animation-duration:20s;animation-delay:-5s}
.orb:nth-child(3){width:400px;height:400px;background:var(--o3);bottom:-5%;left:30%;animation-duration:22s;animation-delay:-10s}
.orb:nth-child(4){width:350px;height:350px;background:var(--o4);top:20%;left:50%;animation-duration:18s;animation-delay:-8s}
@keyframes drift{0%{transform:translate(0,0) scale(1)}33%{transform:translate(60px,-40px) scale(1.1)}66%{transform:translate(-30px,50px) scale(0.95)}100%{transform:translate(40px,20px) scale(1.05)}}
.gl{background:var(--glass);backdrop-filter:blur(var(--blur)) saturate(180%);-webkit-backdrop-filter:blur(var(--blur)) saturate(180%);border:1px solid var(--gb);border-radius:var(--radius);box-shadow:var(--sh);transition:all var(--t)}
.gl:hover{background:var(--gh)}
.ct{max-width:1200px;margin:0 auto;padding:24px;position:relative;z-index:1}
.hd{display:flex;justify-content:space-between;align-items:center;margin-bottom:20px;flex-wrap:wrap;gap:16px}
.logo{display:flex;align-items:center;gap:14px}
.li{width:48px;height:48px;border-radius:14px;background:linear-gradient(135deg,var(--accent),var(--a2));display:flex;align-items:center;justify-content:center;font-size:24px;box-shadow:0 4px 20px rgba(108,99,255,0.3)}
.logo h1{font-size:26px;font-weight:800;letter-spacing:-0.5px}
.logo .sub{font-size:12px;color:var(--t2);font-weight:400;letter-spacing:0.5px}
.hr{display:flex;align-items:center;gap:12px}
.tt{width:56px;height:30px;border-radius:15px;background:var(--surface);border:1px solid var(--gb);cursor:pointer;position:relative;transition:all var(--t)}
.tt::after{content:'';position:absolute;width:22px;height:22px;border-radius:50%;top:3px;left:3px;background:var(--accent);transition:transform var(--t);box-shadow:0 2px 8px rgba(108,99,255,0.3)}
[data-theme="light"] .tt::after{transform:translateX(26px)}
.tt .i{position:absolute;top:6px;font-size:14px;z-index:1;transition:opacity var(--t)}
.tt .m{left:8px;opacity:1}.tt .s{right:7px;opacity:0.4}
[data-theme="light"] .tt .m{opacity:0.4}[data-theme="light"] .tt .s{opacity:1}
.bd{display:inline-flex;align-items:center;gap:6px;padding:6px 14px;border-radius:20px;font-size:12px;font-weight:500}
.bd.on{background:var(--gbg);color:var(--green)}.bd.off{background:rgba(248,113,113,0.1);color:var(--red)}
.dt{width:7px;height:7px;border-radius:50%;flex-shrink:0}
.bd.on .dt{background:var(--green);animation:pl 2s infinite}.bd.off .dt{background:var(--red)}
@keyframes pl{0%,100%{opacity:1}50%{opacity:0.3}}
.rb{background:var(--glass);backdrop-filter:blur(12px);border:1px solid var(--gb);color:var(--t2);padding:7px 16px;border-radius:12px;cursor:pointer;font-size:13px;font-family:inherit;transition:all 0.2s}
.rb:hover{border-color:var(--accent);color:var(--accent)}
/* Time range tabs */
.tabs{display:flex;gap:4px;padding:4px;border-radius:14px;background:var(--surface);border:1px solid var(--gb);margin-bottom:20px;width:fit-content}
.tab{padding:8px 20px;border-radius:10px;font-size:13px;font-weight:600;cursor:pointer;transition:all 0.2s;color:var(--t2);border:none;background:none;font-family:inherit;letter-spacing:0.3px}
.tab:hover{color:var(--text)}
.tab.active{background:var(--accent);color:#fff;box-shadow:0 2px 12px rgba(108,99,255,0.4)}
.range-row{display:flex;align-items:center;justify-content:space-between;margin-bottom:20px;flex-wrap:wrap;gap:12px}
.range-label{font-size:14px;color:var(--t2);font-weight:500;font-family:'JetBrains Mono',monospace}
.range-label span{color:var(--text);font-weight:700}
.cds{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:24px}
.cd{padding:16px;position:relative;overflow:hidden}
.cd .lb{font-size:10px;color:var(--t2);text-transform:uppercase;letter-spacing:1.2px;font-weight:600;margin-bottom:6px}
.cd .vl{font-size:22px;font-weight:800;font-family:'JetBrains Mono',monospace;line-height:1.1}
.cd .sb{font-size:11px;color:var(--t3);margin-top:4px;font-family:'JetBrains Mono',monospace}
.cd .gw{position:absolute;width:100px;height:100px;border-radius:50%;filter:blur(50px);opacity:0.15;top:-20px;right:-20px}
.cd.t .vl{background:linear-gradient(135deg,var(--accent),var(--a2));-webkit-background-clip:text;-webkit-text-fill-color:transparent}.cd.t .gw{background:var(--accent)}
.cd.c .vl{color:var(--green)}.cd.c .gw{background:var(--green)}
.cd.a .vl{color:var(--orange)}.cd.a .gw{background:var(--orange)}
.cd.k .vl{color:var(--cyan)}.cd.k .gw{background:var(--cyan)}
.sc{padding:24px;margin-bottom:28px}
.sc h2{font-size:15px;font-weight:600;margin-bottom:18px;display:flex;align-items:center;gap:10px}
.lg{display:flex;gap:20px;margin-left:auto;font-size:12px;color:var(--t2)}
.lgi{display:flex;align-items:center;gap:6px}
.lgd{width:10px;height:10px;border-radius:3px}
.ch{display:flex;align-items:flex-end;gap:6px;height:220px;padding-bottom:28px;position:relative}
.ch::before{content:'';position:absolute;bottom:28px;left:0;right:0;border-top:1px dashed var(--gb)}
.bg{flex:1;display:flex;flex-direction:column;align-items:center;gap:1px;position:relative;cursor:pointer}
.br{width:100%;max-width:40px;border-radius:6px 6px 2px 2px;min-height:2px;transition:all 0.3s}
.br:hover{filter:brightness(1.3);transform:scaleX(1.1)}
.br.cx{background:linear-gradient(180deg,var(--green),rgba(52,211,153,0.4))}
.br.cl{background:linear-gradient(180deg,var(--cyan),rgba(34,211,238,0.4))}
.br.ax{background:linear-gradient(180deg,var(--orange),rgba(251,146,60,0.4))}
[data-theme="light"] .br.cx{background:linear-gradient(180deg,var(--green),rgba(5,150,105,0.3))}
[data-theme="light"] .br.cl{background:linear-gradient(180deg,var(--cyan),rgba(8,145,178,0.3))}
[data-theme="light"] .br.ax{background:linear-gradient(180deg,var(--orange),rgba(234,88,12,0.3))}
.bd2{position:absolute;bottom:-22px;font-size:10px;color:var(--t3);font-family:'JetBrains Mono',monospace;white-space:nowrap}
.tp{display:none;position:absolute;bottom:calc(100% + 8px);left:50%;transform:translateX(-50%);padding:10px 14px;border-radius:12px;font-size:12px;white-space:nowrap;z-index:20;font-family:'JetBrains Mono',monospace;line-height:1.6}
.bg:hover .tp{display:block}
table{width:100%;border-collapse:collapse}
th{text-align:left;padding:10px 14px;font-size:11px;font-weight:600;color:var(--t3);text-transform:uppercase;letter-spacing:0.8px;border-bottom:1px solid var(--gb)}
td{padding:14px;font-size:13px;border-bottom:1px solid rgba(255,255,255,0.03);font-family:'JetBrains Mono',monospace}
[data-theme="light"] td{border-bottom-color:rgba(0,0,0,0.04)}
tr:hover td{background:rgba(108,99,255,0.03)}
.tg{display:inline-block;padding:3px 10px;border-radius:6px;font-size:11px;font-weight:600;letter-spacing:0.3px}
.tg.cx{background:var(--gbg);color:var(--green)}.tg.cl{background:rgba(34,211,238,0.1);color:var(--cyan)}.tg.ax{background:var(--obg);color:var(--orange)}
.tg.es{background:rgba(167,139,250,0.1);color:var(--a2);font-style:italic;font-weight:400}
.tr td{font-weight:700;border-top:2px solid var(--gb)}
.tr td:last-child{background:linear-gradient(135deg,var(--accent),var(--a2));-webkit-background-clip:text;-webkit-text-fill-color:transparent;font-size:15px}
.ft{text-align:center;padding:20px;font-size:11px;color:var(--t3)}
.qm{display:flex;flex-direction:column;gap:8px}
.qr{display:flex;align-items:center;gap:10px;font-size:12px;font-family:'JetBrains Mono',monospace}
.qr .ql{width:160px;color:var(--t2);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;flex-shrink:0}
.qr .qb{flex:1;height:16px;background:var(--gb);border-radius:8px;overflow:hidden;position:relative}
.qr .qf{height:100%;border-radius:8px;transition:width 0.4s ease}
.qr .qp{width:50px;text-align:right;font-weight:600;flex-shrink:0}
.qr .qrt{width:80px;text-align:right;color:var(--t3);font-size:10px;flex-shrink:0}
@media(max-width:900px){.cds{grid-template-columns:repeat(2,1fr)}}
</style>
</head>
<body data-theme="light">
<div class="dbg"><div class="orb"></div><div class="orb"></div><div class="orb"></div><div class="orb"></div></div>
<div class="ct">
  <div class="hd">
    <div class="logo"><div class="li">⚡</div><div><h1>Star Tokens</h1><div class="sub">UNIFIED AI USAGE DASHBOARD</div></div></div>
    <div class="hr">
      <div id="st"></div>
      <button class="rb" onclick="startEstimator()">Start Anti</button>
      <button class="rb" onclick="refresh()">↻ Refresh</button>
      <div class="tt" onclick="toggleTheme()"><span class="i m">🌙</span><span class="i s">☀️</span></div>
    </div>
  </div>
  <div class="tabs" id="tabs">
    <button class="tab" data-range="daily" onclick="setRange('daily')">Daily</button>
    <button class="tab" data-range="weekly" onclick="setRange('weekly')">Weekly</button>
    <button class="tab active" data-range="monthly" onclick="setRange('monthly')">Monthly</button>
    <button class="tab" data-range="all" onclick="setRange('all')">All Time</button>
  </div>
  <div class="range-row">
    <div class="range-label" id="rl"></div>
  </div>
  <div class="cds" id="cds"></div>
  <div class="gl sc" id="validation"></div>
  <div class="gl sc">
    <h2>Daily Cost<div class="lg"><div class="lgi"><div class="lgd" style="background:var(--green)"></div>Codex</div><div class="lgi"><div class="lgd" style="background:var(--cyan)"></div>Claude Code</div><div class="lgi"><div class="lgd" style="background:var(--orange)"></div>Antigravity</div></div></h2>
    <div class="ch" id="ch"></div>
  </div>
  <div class="gl sc">
    <h2>Detailed Usage</h2>
    <table><thead><tr><th>Date</th><th>Provider</th><th style="text-align:right">Input</th><th style="text-align:right">Cached</th><th style="text-align:right">Output</th><th style="text-align:right">Cost</th></tr></thead><tbody id="tb"></tbody></table>
  </div>
  <div class="ft">Star Tokens · Codex via <b>tu codex</b> · Claude Code via local JSONL/tu validation · Antigravity via local snapshots/nettop · <span id="ts"></span></div>
</div>
<script>
const F=n=>n.toLocaleString(),C=n=>'$'+n.toFixed(2);
let _allData=null, _range='monthly';

function toggleTheme(){const t=document.body.dataset.theme==='dark'?'light':'dark';document.body.dataset.theme=t;localStorage.setItem('st-t',t)}
(()=>{const t=localStorage.getItem('st-t');if(t)document.body.dataset.theme=t;const r=localStorage.getItem('st-range');if(r)_range=r})();

function setRange(r){
  _range=r;
  localStorage.setItem('st-range',r);
  document.querySelectorAll('.tab').forEach(t=>t.classList.toggle('active',t.dataset.range===r));
  if(_allData)render(_allData);
}

async function startEstimator(){
  await fetch('http://localhost:"""+str(PORT)+r"""/api/start-estimator',{method:'POST'});
  await refresh();
}

function getDateRange(){
  const now=new Date();
  const y=now.getFullYear(),m=now.getMonth(),d=now.getDate();
  const pad=(n)=>String(n).padStart(2,'0');
  const fmt=(dt)=>`${dt.getFullYear()}-${pad(dt.getMonth()+1)}-${pad(dt.getDate())}`;
  if(_range==='daily'){
    const s=fmt(now);
    return {start:s,end:s,label:`Today — ${s}`};
  }
  if(_range==='weekly'){
    const day=now.getDay()||7; // Mon=1
    const mon=new Date(y,m,d-day+1);
    return {start:fmt(mon),end:fmt(now),label:`This Week — ${fmt(mon)} to ${fmt(now)}`};
  }
  if(_range==='monthly'){
    const s=`${y}-${pad(m+1)}-01`;
    return {start:s,end:fmt(now),label:`${now.toLocaleString('en',{month:'long'})} ${y}`};
  }
  return {start:'2000-01-01',end:'2099-12-31',label:'All Time'};
}

async function refresh(){
  const d=await(await fetch('http://localhost:"""+str(PORT)+r"""/api/data')).json();
  _allData=d;
  // Set active tab on load
  document.querySelectorAll('.tab').forEach(t=>t.classList.toggle('active',t.dataset.range===_range));
  render(d);
}

function render(d){
  const{daily:allDaily,anti_estimator:ae,generated_at:ga,model:mdl}=d;
  const dr=getDateRange();
  const daily=allDaily.filter(x=>x.date>=dr.start&&x.date<=dr.end);

  // Compute totals for filtered range
  let T={
    codex_cost:0,codex_in:0,codex_cache_create:0,codex_cached:0,codex_out:0,
    claude_cost:0,claude_in:0,claude_cache_create:0,claude_cached:0,claude_out:0,
    anti_cost:0,anti_in:0,anti_out:0,total_cost:0
  };
  daily.forEach(x=>{
    if(x.codex){T.codex_cost+=x.codex.cost;T.codex_in+=x.codex.input_tokens;T.codex_cache_create+=x.codex.cache_creation_tokens||0;T.codex_cached+=x.codex.cached_tokens;T.codex_out+=x.codex.output_tokens+x.codex.reasoning_tokens}
    if(x.claude){T.claude_cost+=x.claude.cost;T.claude_in+=x.claude.input_tokens;T.claude_cache_create+=x.claude.cache_creation_tokens||0;T.claude_cached+=x.claude.cached_tokens;T.claude_out+=x.claude.output_tokens+x.claude.reasoning_tokens}
    if(x.antigravity){T.anti_cost+=x.antigravity.cost;T.anti_in+=x.antigravity.input_tokens;T.anti_out+=x.antigravity.output_tokens}
  });
  T.total_cost=T.codex_cost+T.claude_cost+T.anti_cost;
  const totalIn=T.codex_in+T.claude_in+T.anti_in,totalOut=T.codex_out+T.claude_out+T.anti_out,totalCached=T.codex_cache_create+T.codex_cached+T.claude_cache_create+T.claude_cached;

  document.getElementById('st').innerHTML=ae.running?'<span class="bd on"><span class="dt"></span>Estimator</span>':'<span class="bd off"><span class="dt"></span>Estimator off</span>';
  document.getElementById('ts').textContent=ga;
  document.getElementById('rl').innerHTML=`<span>${dr.label}</span> · ${daily.length} day${daily.length!==1?'s':''}`;

  const fmtTok=n=>n>=1e9?`${(n/1e9).toFixed(2)}B`:n>=1e6?`${(n/1e6).toFixed(1)}M`:F(n);
  const codexTok=T.codex_in+T.codex_cache_create+T.codex_cached+T.codex_out;
  const claudeTok=T.claude_in+T.claude_cache_create+T.claude_cached+T.claude_out;
  const antiTok=T.anti_in+T.anti_out,totalTok=codexTok+claudeTok+antiTok;
  document.getElementById('cds').innerHTML=`
    <div class="gl cd t"><div class="gw"></div><div class="lb">Total Cost</div><div class="vl">${C(T.total_cost)}</div><div class="sb">${daily.length} day${daily.length!==1?'s':''}</div></div>
    <div class="gl cd c"><div class="gw"></div><div class="lb">Codex</div><div class="vl">${C(T.codex_cost)}</div></div>
    <div class="gl cd k"><div class="gw"></div><div class="lb">Claude Code</div><div class="vl">${C(T.claude_cost)}</div></div>
    <div class="gl cd a"><div class="gw"></div><div class="lb">Antigravity</div><div class="vl">${C(T.anti_cost)}</div></div>
    <div class="gl cd" style="--accent:#6366f1"><div class="gw" style="background:#6366f1"></div><div class="lb">Total Tokens</div><div class="vl" style="color:#818cf8">${fmtTok(totalTok)}</div></div>
    <div class="gl cd c"><div class="gw"></div><div class="lb">Codex Tokens</div><div class="vl">${fmtTok(codexTok)}</div></div>
    <div class="gl cd k"><div class="gw"></div><div class="lb">Claude Tokens</div><div class="vl">${fmtTok(claudeTok)}</div></div>
    <div class="gl cd a"><div class="gw"></div><div class="lb">Anti Tokens</div><div class="vl">${fmtTok(antiTok)}</div></div>`;

  const mx=Math.max(...daily.map(d=>(d.codex?.cost||0)+(d.claude?.cost||0)+(d.antigravity?.cost||0)),1);
  document.getElementById('ch').innerHTML=daily.map(d=>{
    const cc=d.codex?.cost||0,lc=d.claude?.cost||0,ac=d.antigravity?.cost||0;
    const ch=Math.max(cc>0?2:0,cc/mx*190),lh=Math.max(lc>0?2:0,lc/mx*190),ah=Math.max(ac>0?2:0,ac/mx*190);
    return`<div class="bg"><div class="gl tp"><div style="font-weight:600;margin-bottom:4px">${d.date}</div><div style="color:var(--green)">Codex: ${C(cc)}</div><div style="color:var(--cyan)">Claude: ${C(lc)}</div><div style="color:var(--orange)">Anti: ${C(ac)}</div><div style="margin-top:4px;font-weight:600">Total: ${C(cc+lc+ac)}</div></div>${ac>0?`<div class="br ax" style="height:${ah}px"></div>`:''}${lc>0?`<div class="br cl" style="height:${lh}px"></div>`:''}${cc>0?`<div class="br cx" style="height:${ch}px"></div>`:''}<span class="bd2">${d.date.slice(5)}</span></div>`}).join('');

  let r='';
  daily.slice().reverse().forEach(d=>{
    let shown=false;
    if(d.codex){const c=d.codex;r+=`<tr><td>${d.date}</td><td><span class="tg cx">Codex</span> ${c.models.join(', ')}</td><td style="text-align:right">${F(c.input_tokens)}</td><td style="text-align:right;color:var(--t3)">${F((c.cache_creation_tokens||0)+c.cached_tokens)}</td><td style="text-align:right">${F(c.output_tokens+c.reasoning_tokens)}</td><td style="text-align:right">${C(c.cost)}</td></tr>`;shown=true}
    if(d.claude){const c=d.claude;r+=`<tr><td>${shown?'':d.date}</td><td><span class="tg cl">Claude Code</span> ${c.models.join(', ')}</td><td style="text-align:right">${F(c.input_tokens)}</td><td style="text-align:right;color:var(--t3)">${F((c.cache_creation_tokens||0)+c.cached_tokens)}</td><td style="text-align:right">${F(c.output_tokens+c.reasoning_tokens)}</td><td style="text-align:right">${C(c.cost)}</td></tr>`;shown=true}
    if(d.antigravity){const a=d.antigravity;const sourceLabel=a.cost_source==='quota_zero'?'idle':a.cost_source==='quota'?'adjusted':(a.cost_source||'est.');const raw=a.suppressed_raw_estimate?` <span class="tg es">raw ${C(a.raw_cost)}</span>`:'';r+=`<tr><td>${shown?'':d.date}</td><td><span class="tg ax">Anti</span> <span class="tg es">${sourceLabel}</span>${raw}</td><td style="text-align:right">${a.estimated?'~':''}${F(a.input_tokens)}</td><td style="text-align:right;color:var(--t3)">-</td><td style="text-align:right">${a.estimated?'~':''}${F(a.output_tokens)}</td><td style="text-align:right">${a.estimated?'~':''}${C(a.cost)}</td></tr>`}
  });
  r+=`<tr class="tr"><td>TOTAL</td><td></td><td style="text-align:right">${F(totalIn)}</td><td style="text-align:right;color:var(--t3)">${F(totalCached)}</td><td style="text-align:right">${F(totalOut)}</td><td style="text-align:right">${C(T.total_cost)}</td></tr>`;
  document.getElementById('tb').innerHTML=r;

  const v=d.claude_validation;
  if(v){
    document.getElementById('validation').innerHTML=`<h2>Claude Validation<span style="font-size:12px;font-weight:500;color:var(--t3)">active: ${v.active_source}</span></h2><div class="qm"><div class="qr"><div class="ql">Local parsed</div><div style="flex:1">${F(v.local_total_tokens)} tokens · ${C(v.local_cost||0)}</div></div><div class="qr"><div class="ql">tu claude</div><div style="flex:1">${F(v.tu_total_tokens)} tokens · ${C(v.tu_cost||0)}</div></div><div class="qr"><div class="ql">Delta</div><div style="flex:1;color:${Math.abs(v.delta_pct)<=v.mismatch_threshold_pct?'var(--green)':'var(--orange)'}">${F(v.delta_tokens)} (${v.delta_pct}%)</div></div><div class="qr"><div class="ql">Deduped</div><div style="flex:1">${F(v.stats?.deduped_entries||0)} entries</div></div></div>`;
    document.getElementById('validation').style.display='';
  } else {
    document.getElementById('validation').style.display='none';
  }
}
refresh();setInterval(refresh,30000);
</script>
</body></html>"""


class Handler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/data":
            data = build_api_response()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(data, ensure_ascii=False).encode())
        elif self.path in ("/", "/index.html"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(DASHBOARD_HTML.encode())
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path == "/api/start-estimator":
            ensure_estimator()
            data = {"anti_estimator": get_anti_status()}
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(data, ensure_ascii=False).encode())
        else:
            self.send_error(404)

    def log_message(self, *a): pass


def ensure_estimator():
    """Start the estimator daemon after an explicit user action."""
    import time
    status = get_anti_status()
    if status["running"]:
        print(f"  📡 Antigravity estimator already running (PID {status['pid']})")
        return status
    # Kill stale PID file
    pid_file = Path.home() / ".config" / "anti-tracker" / "estimator.pid"
    if pid_file.exists():
        pid_file.unlink(missing_ok=True)
    if not ESTIMATOR_SCRIPT.exists():
        print("  ⚠️  anti_estimator.py not found!")
        return get_anti_status()
    # Start and verify (retry once)
    for attempt in range(2):
        subprocess.Popen([sys.executable, str(ESTIMATOR_SCRIPT), "start"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(2)
        status = get_anti_status()
        if status["running"]:
            print(f"  📡 Started Antigravity estimator daemon (PID {status['pid']})")
            return status
    print("  ❌ Failed to start estimator daemon after 2 attempts!")
    return get_anti_status()


def start_api_server():
    server = http.server.HTTPServer(("127.0.0.1", PORT), Handler)
    server.serve_forever()


def launch_native_window(url):
    try:
        import webview
    except ImportError:
        print("  pywebview is not installed, so the native window cannot open.")
        print("  Install it with: python3 -m pip install -r requirements.txt")
        print("  Browser fallback is explicit only: python3 star_tokens.py --browser")
        return False

    try:
        webview.create_window(
            "Star Tokens",
            url,
            width=1200,
            height=800,
            min_size=(800, 600),
            background_color="#050510",
            text_select=False,
        )
        print("  Opening native window...")
        webview.start(debug=False)
        return True
    except Exception as exc:
        print(f"  Native window failed: {exc}")
        print("  Browser fallback is explicit only: python3 star_tokens.py --browser")
        return False


def launch_browser(url):
    webbrowser.open(url)
    return True


def main():
    print("⚡ Star Tokens Dashboard (Native GUI)")

    # Start API server in background thread
    api_thread = threading.Thread(target=start_api_server, daemon=True)
    api_thread.start()
    print(f"  📊 API server on localhost:{PORT}")
    print("  Antigravity estimator is not auto-started; use anti_estimator.py start when needed.")

    url = f"http://127.0.0.1:{PORT}"
    if "--browser" in sys.argv:
        launch_browser(url)
        try:
            api_thread.join()
        except KeyboardInterrupt:
            pass
        return 0

    return 0 if launch_native_window(url) else 1


if __name__ == "__main__":
    sys.exit(main())
