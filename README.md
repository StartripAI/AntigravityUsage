# ⚡ AntigravityUsage

**Passive token usage tracker for [Antigravity IDE](https://antigravity.dev) — no API keys, no proxies, zero intrusion.**

Antigravity doesn't expose token counts. This toolkit estimates them by monitoring network traffic between Antigravity's `language_server` process and Google Cloud Code API, then converts bytes → tokens using a calibrated ratio.

> **Calibrated at 4.0 bytes/token** (TCP payload level) from 300+ request/response bursts over 2.5 hours of live usage.

---

## 🎯 What It Does

| Feature | How |
|---------|-----|
| **Token estimation** | Monitors `nettop` network stats for `language_server` |
| **Cost tracking** | Applies Claude Opus 4.6 pricing ($5/M in, $25/M out) |
| **Calibration** | `tcpdump` passive packet capture → bytes/token ratio |
| **Dashboard** | Native GUI (pywebview) or browser-based, with Codex CLI integration |

---

## 📦 Quick Start

### 1. Start the Estimator Daemon

```bash
python3 anti_estimator.py start
```

This runs in the background, polling `nettop` every 30 seconds. No `sudo` required.

### 2. Check Usage

```bash
python3 anti_estimator.py status   # Today's stats
python3 anti_estimator.py report   # Daily breakdown
```

### 3. Open the Dashboard

```bash
pip3 install pywebview   # Optional, for native window
python3 star_tokens.py
```

Falls back to browser if `pywebview` isn't installed.

### 4. Stop

```bash
python3 anti_estimator.py stop
```

---

## 🔬 Calibration

The default ratio (4.0 bytes/token) works well out of the box. To recalibrate for your own usage patterns:

### Passive Calibration (recommended)

```bash
# Capture TCP packets (requires sudo once for tcpdump)
sudo python3 tcpdump_calibrate.py --duration 3600

# Just use Antigravity normally — every message is a data point

# Analyze results (no sudo)
python3 tcpdump_calibrate.py --analyze
```

### Interactive Calibration

```bash
python3 calibrate.py    # Guided prompts with known token counts
```

Results are saved to `~/.config/anti-tracker/calibration_result.json` and auto-loaded by the estimator.

---

## 📁 Project Structure

```
anti_estimator.py      # Core daemon — nettop monitoring + token estimation
star_tokens.py         # Dashboard GUI — unified Codex + Antigravity view
tcpdump_calibrate.py   # TCP-level calibration via packet capture
calibrate.py           # Interactive calibration with guided prompts
calibrate_auto.py      # Automated calibration via AppleScript
nettop_recorder.py     # High-frequency nettop recorder for passive data
start.sh / stop.sh     # mitmproxy-based tracker (alternative approach)
tracker_addon.py       # mitmproxy addon for HTTP-level tracking
report.py              # Standalone report generator
```

---

## 🧮 How It Works

### The Problem

Antigravity uses gRPC over TLS to communicate with `daily-cloudcode-pa.googleapis.com`. There's no public API to query token usage — the UI shows no token counts.

### The Solution

1. **Monitor network traffic** via `nettop` (macOS built-in, no root required)
2. **Convert bytes → tokens** using a calibrated ratio
3. **Key discovery**: Antigravity resends the **entire conversation context** with every request

### Calibration Method

We capture TCP payload sizes via `tcpdump` and observe:

- **Outbound (request)**: Full conversation context, 400KB–1.6MB per request
- **Inbound (response)**: Model output, 5KB–120KB per response
- **Ratio**: Protobuf-encoded text over gRPC ≈ **4.0 bytes per token**

This ratio was validated across 300+ request/response pairs and remained stable throughout.

### Architecture

```
┌──────────────┐    nettop     ┌──────────────────┐
│  Antigravity  │◄────────────►│  anti_estimator   │
│  (language    │   bytes/sec  │  (daemon)         │
│   _server)    │              │                   │
└──────┬───────┘              └────────┬──────────┘
       │ gRPC/TLS                      │ JSONL log
       ▼                               ▼
┌──────────────┐              ┌──────────────────┐
│  Google Cloud │              │  star_tokens.py   │
│  Code API     │              │  (dashboard)      │
└──────────────┘              └──────────────────┘
```

---

## 📊 Dashboard Preview

The dashboard shows:
- **Total cost** across Codex CLI + Antigravity
- **Daily bar chart** with per-provider breakdown
- **Detailed table** with input/output token counts
- **Estimator status** indicator (running/stopped)

Supports dark/light mode with glassmorphism UI.

---

## ⚙️ Configuration

All data is stored in `~/.config/anti-tracker/`:

| File | Purpose |
|------|---------|
| `nettop_usage.jsonl` | Raw usage log (one entry per polling interval) |
| `calibration_result.json` | Calibrated bytes/token ratio |
| `estimator.pid` | Daemon PID file |
| `tcpdump_capture.jsonl` | Raw tcpdump calibration data |

### Pricing

Default: Claude Opus 4.6 Thinking — $5/M input, $25/M output. Edit the constants in `anti_estimator.py` to match your model.

---

## 🔧 Requirements

- **macOS** (uses `nettop`, a macOS-specific tool)
- **Python 3.8+** (no pip dependencies for core functionality)
- **Antigravity IDE** running
- `pywebview` (optional, for native dashboard window)
- `tcpdump` + `sudo` (only for calibration)

---

## ⚠️ Accuracy

Token estimates are **approximations** (±15%), not exact counts. The estimator cannot distinguish between:
- Conversation text vs. system prompts
- Tool definitions vs. user messages  
- Cached vs. uncached tokens

For precise tracking, use the mitmproxy approach (`start.sh`) which intercepts actual API payloads — but requires restarting Antigravity with `HTTPS_PROXY`.

---

## 📄 License

MIT — do whatever you want with it.

---

## 🤝 Contributing

PRs welcome! Some ideas:
- [ ] Linux support (replace `nettop` with `ss` or `/proc/net`)
- [ ] Windows support
- [ ] Better token count extraction from Antigravity's local SQLite DB
- [ ] Historical data export (CSV, JSON)
- [ ] Multi-model pricing support

---

*Built by [StartripAI](https://github.com/StartripAI) — because you should know what you're spending.*
