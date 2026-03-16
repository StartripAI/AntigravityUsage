"""
Antigravity Token Tracker — mitmproxy addon
Passively captures token usage from Antigravity language server traffic.
Logs to ~/.config/anti-tracker/usage.jsonl
"""
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from mitmproxy import http, ctx

LOG_DIR = Path.home() / ".config" / "anti-tracker"
LOG_FILE = LOG_DIR / "usage.jsonl"
TARGET_HOST = "daily-cloudcode-pa.googleapis.com"


def ensure_log_dir():
    LOG_DIR.mkdir(parents=True, exist_ok=True)


def extract_json_fields(body: bytes) -> dict | None:
    """
    Try to extract JSON from the response body.
    Connect protocol responses may have a 5-byte gRPC frame header
    before JSON/protobuf payload.
    """
    if not body:
        return None

    text = None

    # Try raw body as JSON first
    try:
        text = body.decode("utf-8", errors="ignore")
    except Exception:
        pass

    # If body starts with gRPC frame (1 byte flag + 4 bytes length), skip it
    if body[0:1] in (b'\x00', b'\x01') and len(body) > 5:
        try:
            text = body[5:].decode("utf-8", errors="ignore")
        except Exception:
            pass

    if not text:
        return None

    # Try to find JSON objects in the text
    # Sometimes responses have multiple JSON objects or are wrapped
    json_candidates = []

    # Try the whole thing
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Try to find JSON objects with regex
    for match in re.finditer(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', text):
        try:
            obj = json.loads(match.group())
            json_candidates.append(obj)
        except json.JSONDecodeError:
            continue

    if json_candidates:
        # Return the largest/most complete JSON object
        return max(json_candidates, key=lambda x: len(json.dumps(x)))

    return None


def extract_token_info(data: dict) -> dict:
    """
    Recursively search for token-related fields in the response data.
    """
    result = {}

    def recurse(obj, path=""):
        if isinstance(obj, dict):
            for k, v in obj.items():
                kl = k.lower()
                if any(t in kl for t in [
                    "token", "usage", "model", "quota", "remaining",
                    "reset", "cost", "credit", "input", "output",
                    "cached", "reasoning", "plan", "email", "fraction"
                ]):
                    full_key = f"{path}.{k}" if path else k
                    result[full_key] = v
                if isinstance(v, (dict, list)):
                    recurse(v, f"{path}.{k}" if path else k)
        elif isinstance(obj, list):
            for i, item in enumerate(obj):
                if isinstance(item, (dict, list)):
                    recurse(item, f"{path}[{i}]")

    recurse(data)
    return result


class AntigravityTracker:
    def __init__(self):
        ensure_log_dir()
        self.request_count = 0
        ctx.log.info(f"[AntiTracker] Logging to {LOG_FILE}")
        ctx.log.info(f"[AntiTracker] Monitoring traffic to {TARGET_HOST}")

    def response(self, flow: http.HTTPFlow):
        if not flow.request.pretty_host.endswith("googleapis.com"):
            return

        self.request_count += 1
        url = flow.request.pretty_url
        method = flow.request.method
        status = flow.response.status_code if flow.response else None
        path = flow.request.path

        # Log all Antigravity-related requests
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "local_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "method": method,
            "path": path,
            "status": status,
            "request_size": len(flow.request.content) if flow.request.content else 0,
            "response_size": len(flow.response.content) if flow.response and flow.response.content else 0,
        }

        # Try to parse response body for token info
        if flow.response and flow.response.content:
            parsed = extract_json_fields(flow.response.content)
            if parsed:
                token_info = extract_token_info(parsed)
                if token_info:
                    entry["token_data"] = token_info
                # Also store a subset of raw data for debugging
                entry["raw_keys"] = list(parsed.keys()) if isinstance(parsed, dict) else []

        # Try to parse request body for model info
        if flow.request.content:
            req_parsed = extract_json_fields(flow.request.content)
            if req_parsed:
                req_info = extract_token_info(req_parsed)
                if req_info:
                    entry["request_data"] = req_info

        # Write to JSONL log
        with open(LOG_FILE, "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

        ctx.log.info(
            f"[AntiTracker] #{self.request_count} {method} {path} "
            f"→ {status} ({entry.get('response_size', 0)} bytes)"
        )
        if "token_data" in entry:
            ctx.log.info(f"[AntiTracker] Token data: {json.dumps(entry['token_data'], indent=2)[:500]}")


addons = [AntigravityTracker()]
