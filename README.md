# Star Tokens

Unified local usage dashboard for **Codex**, **Claude Code**, and **Antigravity**.

Star Tokens reads local usage logs and tokenusage reports, then shows one native dashboard for daily token and cost tracking. It is designed for local visibility: no API keys, no proxy requirement for the main dashboard, and no SaaS backend.

## What It Tracks

| Source | Data path | Accuracy strategy |
| --- | --- | --- |
| **Codex** | `tu codex --json` plus local Codex state DB dedupe | Keeps Codex separate from Claude and applies per-day dedupe ratios for repeated Codex sessions/worktrees. |
| **Claude Code** | `~/.claude/projects/**/*.jsonl` and `tu claude --json` | Local parser dedupes by `message.id + requestId`; `tu claude` and optional `ccusage` are used as diagnostics. |
| **Antigravity** | Local quota snapshots and `nettop` usage logs | Preserves historical logs but suppresses false usage when current primary model shows idle/zero usage. |

## Dashboard

`star_tokens.py` starts a local HTTP API and opens a native `pywebview` window titled **Star Tokens**.

The dashboard includes:

- Total cost and token cards across Codex, Claude Code, and Antigravity
- Daily cost chart split by provider
- Detailed daily table for input, cache, output, and cost
- Claude validation panel showing local JSONL vs `tu claude`
- Manual Antigravity estimator start button

The dashboard no longer auto-starts the Antigravity estimator. Start it manually when you want to collect Anti network data.

## Quick Start

Install the native window dependency:

```bash
python3 -m pip install -r requirements.txt
```

Open the dashboard:

```bash
python3 star_tokens.py
```

If `pywebview` is missing, the app prints an install command and exits. Browser fallback is explicit:

```bash
python3 star_tokens.py --browser
```

## Antigravity Estimator

Start the estimator from the dashboard with **Start Anti**, or from the shell:

```bash
python3 anti_estimator.py start
python3 anti_estimator.py status
python3 anti_estimator.py report
python3 anti_estimator.py stop
```

The estimator polls macOS `nettop` every 30 seconds and writes local JSONL usage logs under `~/.config/anti-tracker/`.

## Accuracy Notes

### Codex

Codex usage comes from `tu codex --json`. Star Tokens also reads `~/.codex/state_5.sqlite` to correct known over-counting from repeated sessions and parallel worktrees by applying a per-day dedupe ratio.

### Claude Code

Claude Code usage is parsed from local JSONL files in:

- `~/.claude/projects`
- `~/.config/claude/projects`

The local parser counts:

- `input_tokens`
- `cache_creation_input_tokens`
- `cache_read_input_tokens`
- `output_tokens`

It dedupes repeated assistant usage entries by `message.id + requestId`. If local JSONL and `tu claude` differ by more than 2%, Star Tokens uses the local deduped parser for the dashboard and shows the mismatch in the validation panel. `npx ccusage@latest daily --json` can be used as an external comparison.

### Antigravity

Antigravity does not expose stable token logs. Star Tokens keeps historical `nettop` estimates, but uses local quota snapshots to detect idle days and avoid showing false Anti usage/cost when the current primary model is at 0%.

Primary Antigravity model selection follows this priority:

1. Non-thinking Claude
2. Gemini Pro Low
3. Gemini Flash
4. Fallback: lowest remaining quota

This prevents stale Claude Thinking usage from polluting a current Gemini idle day.

## Project Structure

```text
star_tokens.py         # Native dashboard + local API for Codex, Claude Code, Antigravity
anti_estimator.py      # Antigravity nettop/quota snapshot daemon
requirements.txt       # Native window dependency
tests/                 # Usage parser, source separation, Anti idle, launch behavior tests
tcpdump_calibrate.py   # Optional TCP-level calibration for Antigravity estimates
calibrate.py           # Interactive calibration helper
calibrate_auto.py      # Automated calibration helper
nettop_recorder.py     # High-frequency nettop recorder
report.py              # Standalone Antigravity report generator
SOP.md                 # Pricing and usage calculation rules
```

## Requirements

- macOS
- Python 3.8+
- [`tokenusage`](https://github.com/hanbu97/tokenusage) CLI available as `tu`
- `pywebview` for the default native window
- Antigravity IDE only if you want Antigravity tracking
- Optional: `npx ccusage@latest daily --json` for Claude Code validation

## Data Files

| File | Purpose |
| --- | --- |
| `~/.config/anti-tracker/nettop_usage.jsonl` | Antigravity network estimate log |
| `~/.config/anti-tracker/quota_snapshots.jsonl` | Antigravity quota snapshots used for idle/adjusted cost logic |
| `~/.config/anti-tracker/current_model.json` | Current Antigravity model pricing selection |
| `~/.config/anti-tracker/calibration_result.json` | Optional bytes/token calibration |
| `~/.codex/state_5.sqlite` | Codex dedupe source |
| `~/.claude/projects/**/*.jsonl` | Claude Code local usage source |

## Development

Run tests:

```bash
python3 -m py_compile star_tokens.py anti_estimator.py
python3 -m unittest tests/test_usage_dashboard.py
```

## License

MIT

Built by [StartripAI](https://github.com/StartripAI).
