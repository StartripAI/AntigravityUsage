#!/usr/bin/env python3
"""
star-tokens — Unified AI Token Usage Dashboard (Native GUI)
Uses pywebview for native macOS window with liquid glass UI.

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
ESTIMATOR_SCRIPT = Path(__file__).parent / "anti_estimator.py"

ANTI_MODEL = "claude-opus-4-6-thinking"
ANTI_INPUT_PRICE = 5.00
ANTI_OUTPUT_PRICE = 25.00


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
    total_codex_cost = total_anti_cost = total_codex_tokens = total_anti_tokens = 0
    for date in sorted(all_dates):
        entry = {"date": date, "codex": None, "antigravity": None}
        if date in codex_by_date:
            cd = codex_by_date[date]
            t = cd.get("totals", {})
            cc, ct = t.get("cost_usd", 0), t.get("total_tokens", 0)
            entry["codex"] = {
                "input_tokens": t.get("input_tokens", 0), "cached_tokens": t.get("cache_read_input_tokens", 0),
                "output_tokens": t.get("output_tokens", 0), "reasoning_tokens": t.get("reasoning_output_tokens", 0),
                "total_tokens": ct, "cost": round(cc, 2), "models": list(cd.get("models", {}).keys()),
            }
            total_codex_cost += cc; total_codex_tokens += ct
        if date in anti:
            ad = anti[date]
            ac, at = ad["cost"], ad["input_tokens"] + ad["output_tokens"]
            entry["antigravity"] = {
                "input_tokens": ad["input_tokens"], "output_tokens": ad["output_tokens"],
                "total_tokens": at, "cost": round(ac, 2), "bytes_in": ad["bytes_in"], "bytes_out": ad["bytes_out"],
                "model": ANTI_MODEL, "estimated": True,
            }
            total_anti_cost += ac; total_anti_tokens += at
        combined.append(entry)
    return {
        "daily": combined,
        "totals": {"codex_cost": round(total_codex_cost, 2), "codex_tokens": total_codex_tokens,
                   "anti_cost": round(total_anti_cost, 2), "anti_tokens": total_anti_tokens,
                   "total_cost": round(total_codex_cost + total_anti_cost, 2),
                   "total_tokens": total_codex_tokens + total_anti_tokens},
        "anti_estimator": anti_status,
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
.hd{display:flex;justify-content:space-between;align-items:center;margin-bottom:28px;flex-wrap:wrap;gap:16px}
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
.cds{display:grid;grid-template-columns:repeat(4,1fr);gap:16px;margin-bottom:28px}
.cd{padding:22px;position:relative;overflow:hidden}
.cd .lb{font-size:11px;color:var(--t2);text-transform:uppercase;letter-spacing:1.2px;font-weight:600;margin-bottom:10px}
.cd .vl{font-size:30px;font-weight:800;font-family:'JetBrains Mono',monospace;line-height:1.1}
.cd .sb{font-size:12px;color:var(--t3);margin-top:6px;font-family:'JetBrains Mono',monospace}
.cd .gw{position:absolute;width:120px;height:120px;border-radius:50%;filter:blur(50px);opacity:0.15;top:-20px;right:-20px}
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
@media(max-width:900px){.cds{grid-template-columns:repeat(2,1fr)}}
</style>
</head>
<body data-theme="dark">
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
  <div class="cds" id="cds"></div>
  <div class="gl sc">
    <h2>Daily Cost<div class="lg"><div class="lgi"><div class="lgd" style="background:var(--green)"></div>Codex</div><div class="lgi"><div class="lgd" style="background:var(--orange)"></div>Antigravity</div></div></h2>
    <div class="ch" id="ch"></div>
  </div>
  <div class="gl sc">
    <h2>Detailed Usage</h2>
    <table><thead><tr><th>Date</th><th>Provider</th><th style="text-align:right">Input</th><th style="text-align:right">Output</th><th style="text-align:right">Total</th><th style="text-align:right">Cost</th></tr></thead><tbody id="tb"></tbody></table>
  </div>
  <div class="ft">Star Tokens · Codex via <b>tu</b> (precise) · Antigravity via nettop (est.) · <span id="ts"></span></div>
</div>
<script>
const F=n=>n.toLocaleString(),C=n=>'$'+n.toFixed(2);
function toggleTheme(){const t=document.body.dataset.theme==='dark'?'light':'dark';document.body.dataset.theme=t;localStorage.setItem('st-t',t)}
(()=>{const t=localStorage.getItem('st-t');if(t)document.body.dataset.theme=t})();
async function refresh(){const d=await(await fetch('http://localhost:"""+str(PORT)+r"""/api/data')).json();render(d)}
function render(d){
  const{daily,totals:T,anti_estimator:ae,generated_at:ga}=d;
  document.getElementById('st').innerHTML=ae.running?'<span class="bd on"><span class="dt"></span>Estimator</span>':'<span class="bd off"><span class="dt"></span>Estimator off</span>';
  document.getElementById('ts').textContent=ga;
  document.getElementById('cds').innerHTML=`
    <div class="gl cd t"><div class="gw"></div><div class="lb">Total Cost</div><div class="vl">${C(T.total_cost)}</div><div class="sb">${F(T.total_tokens)} tok</div></div>
    <div class="gl cd c"><div class="gw"></div><div class="lb">Codex · Precise</div><div class="vl">${C(T.codex_cost)}</div><div class="sb">${F(T.codex_tokens)} tok</div></div>
    <div class="gl cd a"><div class="gw"></div><div class="lb">Antigravity · Est.</div><div class="vl">${C(T.anti_cost)}</div><div class="sb">~${F(T.anti_tokens)} tok</div></div>
    <div class="gl cd k"><div class="gw"></div><div class="lb">Token Volume</div><div class="vl">${(T.total_tokens/1e9).toFixed(2)}B</div><div class="sb">${daily.length} days</div></div>`;
  const rc=daily.slice(-14),mx=Math.max(...rc.map(d=>(d.codex?.cost||0)+(d.antigravity?.cost||0)),1);
  document.getElementById('ch').innerHTML=rc.map(d=>{
    const cc=d.codex?.cost||0,ac=d.antigravity?.cost||0,ch=Math.max(2,cc/mx*190),ah=Math.max(ac>0?2:0,ac/mx*190);
    return`<div class="bg"><div class="gl tp"><div style="font-weight:600;margin-bottom:4px">${d.date}</div><div style="color:var(--green)">Codex: ${C(cc)}</div><div style="color:var(--orange)">Anti: ${C(ac)}</div><div style="margin-top:4px;font-weight:600">Total: ${C(cc+ac)}</div></div>${ac>0?`<div class="br ax" style="height:${ah}px"></div>`:''}${cc>0?`<div class="br cx" style="height:${ch}px"></div>`:''}<span class="bd2">${d.date.slice(5)}</span></div>`}).join('');
  let r='';
  daily.slice().reverse().forEach(d=>{
    if(d.codex){const c=d.codex;r+=`<tr><td>${d.date}</td><td><span class="tg cx">Codex</span> ${c.models.slice(0,2).join(', ')}</td><td style="text-align:right">${F(c.input_tokens+c.cached_tokens)}</td><td style="text-align:right">${F(c.output_tokens+c.reasoning_tokens)}</td><td style="text-align:right">${F(c.total_tokens)}</td><td style="text-align:right">${C(c.cost)}</td></tr>`}
    if(d.antigravity){const a=d.antigravity;r+=`<tr><td>${d.codex?'':d.date}</td><td><span class="tg ax">Anti</span> <span class="tg es">est.</span></td><td style="text-align:right">~${F(a.input_tokens)}</td><td style="text-align:right">~${F(a.output_tokens)}</td><td style="text-align:right">~${F(a.total_tokens)}</td><td style="text-align:right">~${C(a.cost)}</td></tr>`}
  });
  r+=`<tr class="tr"><td>TOTAL</td><td></td><td></td><td></td><td style="text-align:right">${F(T.total_tokens)}</td><td style="text-align:right">${C(T.total_cost)}</td></tr>`;
  document.getElementById('tb').innerHTML=r;
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
