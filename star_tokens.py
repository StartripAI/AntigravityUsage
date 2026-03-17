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
import webbrowser
from datetime import datetime
from pathlib import Path

PORT = 18877  # Internal port for API
ANTI_LOG = Path.home() / ".config" / "anti-tracker" / "nettop_usage.jsonl"
MODEL_FILE = Path.home() / ".config" / "anti-tracker" / "current_model.json"
ESTIMATOR_SCRIPT = Path(__file__).parent / "anti_estimator.py"

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
    return 10.0  # $10 per 20% tier → $50 for full 100%

COST_PER_20PCT = _get_quota_price()

def get_quota_cost_for_date(date_str):
    """Calculate Anti cost from quota snapshots for a given date.
    
    Reads quota snapshots, finds the max used% for the date,
    and returns interpolated cost based on configurable $/20% rate.
    """
    max_used = 0.0
    if QUOTA_LOG.exists():
        with open(QUOTA_LOG) as f:
            for line in f:
                if line.strip():
                    try:
                        snap = json.loads(line)
                        if snap["timestamp"].startswith(date_str):
                            used = snap.get("primary_used_pct", 0)
                            if used > max_used:
                                max_used = used
                    except Exception:
                        continue
    # Interpolate: each 20% tier = COST_PER_20PCT
    # If used=60%, that's 3 tiers × $10 = $30
    tiers_used = max_used / 20.0
    return round(tiers_used * COST_PER_20PCT, 2), max_used


def get_codex_data():
    try:
        result = subprocess.run(["tu", "daily", "--json"], capture_output=True, text=True, timeout=15)
        if result.returncode == 0:
            return json.loads(result.stdout)
    except Exception:
        pass
    return {"daily": []}


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
    from collections import defaultdict
    daily = defaultdict(lambda: {"input_tokens": 0, "output_tokens": 0, "bytes_in": 0, "bytes_out": 0, "cost": 0})
    for e in entries:
        date = e.get("timestamp", "")[:10]
        d = daily[date]
        d["input_tokens"] += e.get("input_tokens_est", 0)
        d["output_tokens"] += e.get("output_tokens_est", 0)
        d["bytes_in"] += e.get("delta_bytes_in", 0)
        d["bytes_out"] += e.get("delta_bytes_out", 0)
        d["cost"] += e.get("total_cost_est", 0)
    # Apply quota-based cost cap
    for date, d in daily.items():
        quota_cost, used_pct = get_quota_cost_for_date(date)
        d["raw_cost"] = d["cost"]
        d["quota_used_pct"] = used_pct
        if quota_cost > 0:
            d["cost"] = quota_cost  # Use quota-derived cost instead of nettop estimate
            d["capped"] = True
        else:
            # No quota data, use nettop estimate but cap at 100% = 5 tiers × $10
            d["cost"] = min(d["cost"], COST_PER_20PCT * 5)
            d["capped"] = d["raw_cost"] > COST_PER_20PCT * 5
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


def get_anti_quota():
    """Fetch live quota from tu antigravity --json"""
    try:
        result = subprocess.run(["tu", "antigravity", "--json"], capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            data = json.loads(result.stdout)
            return {
                "plan": data.get("plan_type", "Unknown"),
                "email": data.get("account_email", ""),
                "models": [{
                    "label": m["label"],
                    "remaining": round(m["remaining_fraction"] * 100, 1),
                    "reset_time": m.get("reset_time", 0),
                } for m in data.get("models", [])],
                "primary_used": data.get("primary_used_percent", 0),
            }
    except Exception:
        pass
    return None


def build_api_response():
    codex = get_codex_data()
    anti = get_anti_data()
    anti_status = get_anti_status()
    all_dates = set()
    codex_by_date = {}
    for day in codex.get("daily", []):
        date = day.get("date", "")
        codex_by_date[date] = day
        all_dates.add(date)
    all_dates.update(anti.keys())
    combined = []
    for date in sorted(all_dates):
        entry = {"date": date, "codex": None, "antigravity": None}
        if date in codex_by_date:
            cd = codex_by_date[date]
            t = cd.get("totals", {})
            cc, ct = t.get("cost_usd", 0), t.get("total_tokens", 0)
            entry["codex"] = {
                "input_tokens": t.get("input_tokens", 0), "cached_tokens": t.get("cache_read_input_tokens", 0),
                "output_tokens": t.get("output_tokens", 0), "reasoning_tokens": t.get("reasoning_output_tokens", 0),
                "total_tokens": ct, "cost": round(cc, 2),
                "models": sorted(cd.get("models", {}).keys(),
                                 key=lambda m: cd["models"][m].get("total_tokens", 0), reverse=True),
            }
        if date in anti:
            ad = anti[date]
            ac, at = ad["cost"], ad["input_tokens"] + ad["output_tokens"]
            entry["antigravity"] = {
                "input_tokens": ad["input_tokens"], "output_tokens": ad["output_tokens"],
                "total_tokens": at, "cost": round(ac, 2), "bytes_in": ad["bytes_in"], "bytes_out": ad["bytes_out"],
                "model": ANTI_MODEL["name"], "estimated": True,
                "capped": ad.get("capped", False), "raw_cost": round(ad.get("raw_cost", ac), 2),
                "quota_used_pct": ad.get("quota_used_pct", 0),
            }
        combined.append(entry)
    return {
        "daily": combined,
        "anti_estimator": anti_status,
        "anti_quota": get_anti_quota(),
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
.br.ax{background:linear-gradient(180deg,var(--orange),rgba(251,146,60,0.4))}
[data-theme="light"] .br.cx{background:linear-gradient(180deg,var(--green),rgba(5,150,105,0.3))}
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
.tg.cx{background:var(--gbg);color:var(--green)}.tg.ax{background:var(--obg);color:var(--orange)}
.tg.es{background:rgba(167,139,250,0.1);color:var(--a2);font-style:italic;font-weight:400}
.tr td{font-weight:700;border-top:2px solid var(--gb)}
.tr td:last-child{background:linear-gradient(135deg,var(--accent),var(--a2));-webkit-background-clip:text;-webkit-text-fill-color:transparent;font-size:15px}
.ft{text-align:center;padding:20px;font-size:11px;color:var(--t3)}
.qt{margin-bottom:20px}
.qt h2{font-size:16px;margin-bottom:14px;font-weight:700}
.qt h2 span{font-size:12px;font-weight:500;color:var(--t3);margin-left:8px}
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
  <div class="gl sc qt" id="quota"></div>
  <div class="gl sc">
    <h2>Daily Cost<div class="lg"><div class="lgi"><div class="lgd" style="background:var(--green)"></div>Codex</div><div class="lgi"><div class="lgd" style="background:var(--orange)"></div>Antigravity</div></div></h2>
    <div class="ch" id="ch"></div>
  </div>
  <div class="gl sc">
    <h2>Detailed Usage</h2>
    <table><thead><tr><th>Date</th><th>Provider</th><th style="text-align:right">Input</th><th style="text-align:right">Cached</th><th style="text-align:right">Output</th><th style="text-align:right">Cost</th></tr></thead><tbody id="tb"></tbody></table>
  </div>
  <div class="ft">Star Tokens · Codex via <b>tu</b> (precise) · Antigravity via nettop (est.) · <span id="ts"></span></div>
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
  let T={codex_cost:0,codex_in:0,codex_cached:0,codex_out:0,anti_cost:0,anti_in:0,anti_out:0,total_cost:0};
  daily.forEach(x=>{
    if(x.codex){T.codex_cost+=x.codex.cost;T.codex_in+=x.codex.input_tokens;T.codex_cached+=x.codex.cached_tokens;T.codex_out+=x.codex.output_tokens+x.codex.reasoning_tokens}
    if(x.antigravity){T.anti_cost+=x.antigravity.cost;T.anti_in+=x.antigravity.input_tokens;T.anti_out+=x.antigravity.output_tokens}
  });
  T.total_cost=T.codex_cost+T.anti_cost;
  const totalIn=T.codex_in+T.anti_in,totalOut=T.codex_out+T.anti_out;

  document.getElementById('st').innerHTML=ae.running?'<span class="bd on"><span class="dt"></span>Estimator</span>':'<span class="bd off"><span class="dt"></span>Estimator off</span>';
  document.getElementById('ts').textContent=ga;
  document.getElementById('rl').innerHTML=`<span>${dr.label}</span> · ${daily.length} day${daily.length!==1?'s':''}`;

  const fmtTok=n=>n>=1e9?`${(n/1e9).toFixed(2)}B`:n>=1e6?`${(n/1e6).toFixed(1)}M`:F(n);
  document.getElementById('cds').innerHTML=`
    <div class="gl cd t"><div class="gw"></div><div class="lb">Total Cost</div><div class="vl">${C(T.total_cost)}</div><div class="sb">${daily.length} day${daily.length!==1?'s':''}</div></div>
    <div class="gl cd c"><div class="gw"></div><div class="lb">Codex · API Value</div><div class="vl">${C(T.codex_cost)}</div></div>
    <div class="gl cd a"><div class="gw"></div><div class="lb">Antigravity · Quota</div><div class="vl">${C(T.anti_cost)}</div></div>
    <div class="gl cd" style="--accent:#6366f1"><div class="gw" style="background:#6366f1"></div><div class="lb">Input Tokens</div><div class="vl" style="color:#818cf8">${fmtTok(totalIn)}</div></div>
    <div class="gl cd k"><div class="gw"></div><div class="lb">Cached Tokens</div><div class="vl">${fmtTok(T.codex_cached)}</div></div>
    <div class="gl cd" style="--accent:#f59e0b"><div class="gw" style="background:#f59e0b"></div><div class="lb">Output Tokens</div><div class="vl" style="color:#fbbf24">${fmtTok(totalOut)}</div></div>`;

  const mx=Math.max(...daily.map(d=>(d.codex?.cost||0)+(d.antigravity?.cost||0)),1);
  document.getElementById('ch').innerHTML=daily.map(d=>{
    const cc=d.codex?.cost||0,ac=d.antigravity?.cost||0,ch=Math.max(2,cc/mx*190),ah=Math.max(ac>0?2:0,ac/mx*190);
    return`<div class="bg"><div class="gl tp"><div style="font-weight:600;margin-bottom:4px">${d.date}</div><div style="color:var(--green)">Codex: ${C(cc)}</div><div style="color:var(--orange)">Anti: ${C(ac)}</div><div style="margin-top:4px;font-weight:600">Total: ${C(cc+ac)}</div></div>${ac>0?`<div class="br ax" style="height:${ah}px"></div>`:''}${cc>0?`<div class="br cx" style="height:${ch}px"></div>`:''}<span class="bd2">${d.date.slice(5)}</span></div>`}).join('');

  let r='';
  daily.slice().reverse().forEach(d=>{
    if(d.codex){const c=d.codex;r+=`<tr><td>${d.date}</td><td><span class="tg cx">Codex</span> ${c.models.join(', ')}</td><td style="text-align:right">${F(c.input_tokens)}</td><td style="text-align:right;color:var(--t3)">${F(c.cached_tokens)}</td><td style="text-align:right">${F(c.output_tokens+c.reasoning_tokens)}</td><td style="text-align:right">${C(c.cost)}</td></tr>`}
    if(d.antigravity){const a=d.antigravity;r+=`<tr><td>${d.codex?'':d.date}</td><td><span class="tg ax">Anti</span> <span class="tg es">est.</span></td><td style="text-align:right">~${F(a.input_tokens)}</td><td style="text-align:right;color:var(--t3)">—</td><td style="text-align:right">~${F(a.output_tokens)}</td><td style="text-align:right">~${C(a.cost)}</td></tr>`}
  });
  r+=`<tr class="tr"><td>TOTAL</td><td></td><td style="text-align:right">${F(totalIn)}</td><td style="text-align:right;color:var(--t3)">${F(T.codex_cached)}</td><td style="text-align:right">${F(totalOut)}</td><td style="text-align:right">${C(T.total_cost)}</td></tr>`;
  document.getElementById('tb').innerHTML=r;

  // Quota section
  const q=data.anti_quota;
  if(q){
    const colors=['#6366f1','#22c55e','#f59e0b','#ec4899','#06b6d4','#8b5cf6'];
    const now=Date.now()/1000;
    let qh=`<h2>Antigravity Quota<span>${q.plan} · ${q.email}</span></h2><div class="qm">`;
    q.models.forEach((m,i)=>{
      const rem=m.remaining;
      const used=(100-rem).toFixed(1);
      const c=colors[i%colors.length];
      const bg=rem>60?'#22c55e':rem>30?'#f59e0b':'#ef4444';
      const dt=m.reset_time-now;
      const rh=Math.floor(dt/3600),rm=Math.floor((dt%3600)/60);
      const rt=dt>0?`${rh}h${rm}m`:'now';
      qh+=`<div class="qr"><div class="ql">${m.label}</div><div class="qb"><div class="qf" style="width:${rem}%;background:${bg}"></div></div><div class="qp" style="color:${bg}">${rem}%</div><div class="qrt">↻ ${rt}</div></div>`;
    });
    qh+='</div>';
    document.getElementById('quota').innerHTML=qh;
    document.getElementById('quota').style.display='';
  } else {
    document.getElementById('quota').style.display='none';
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
    def log_message(self, *a): pass


def ensure_estimator():
    status = get_anti_status()
    if not status["running"] and ESTIMATOR_SCRIPT.exists():
        subprocess.Popen([sys.executable, str(ESTIMATOR_SCRIPT), "start"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print("  📡 Started Antigravity estimator daemon")


def start_api_server():
    server = http.server.HTTPServer(("127.0.0.1", PORT), Handler)
    server.serve_forever()


def main():
    print("⚡ Star Tokens Dashboard (Native GUI)")
    ensure_estimator()

    # Start API server in background thread
    api_thread = threading.Thread(target=start_api_server, daemon=True)
    api_thread.start()
    print(f"  📊 API server on localhost:{PORT}")

    # Launch native window via pywebview
    try:
        import webview
        window = webview.create_window(
            "Star Tokens",
            f"http://127.0.0.1:{PORT}",
            width=1200,
            height=800,
            min_size=(800, 600),
            background_color="#050510",
            text_select=False,
        )
        print("  🪟 Opening native window...")
        webview.start(debug=False)
    except ImportError:
        print("  ⚠️  pywebview not found, falling back to browser")
        webbrowser.open(f"http://127.0.0.1:{PORT}")
        try:
            api_thread.join()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
