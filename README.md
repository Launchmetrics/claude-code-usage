# Claude Code Usage Dashboard

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg?style=flat-square)](LICENSE)
[![claude-code](https://img.shields.io/badge/claude--code-black?style=flat-square)](https://claude.ai/code)

**Pro and Max subscribers get a progress bar. This gives you the full picture.**

Claude Code writes detailed usage logs locally — token counts, models, sessions, projects — regardless of your plan. This dashboard reads those logs and turns them into charts and cost estimates. Works on API, Pro, and Max plans.

![Claude Usage Dashboard](docs/screenshot.png)

**Created by:** [The Product Compass Newsletter](https://www.productcompass.pm)

**This is a Launchmetrics fork of [phuryn/claude-usage](https://github.com/phuryn/claude-usage)** — maintained for internal use, with bug fixes and clarifications. Upstream contributions still welcome.

---

## What this tracks

Works on **API, Pro, Max, Teams, and Enterprise plans** — Claude Code writes local usage logs regardless of subscription type. This tool reads those logs and gives you visibility that Anthropic's UI doesn't provide.

Captures usage from:
- **Claude Code CLI** (`claude` command in terminal)
- **VS Code extension** (Claude Code sidebar)
- **Dispatched Code sessions** (sessions routed through Claude Code)

**Not captured:**
- **Cowork sessions** — these run server-side and do not write local JSONL transcripts
- **Sessions on other machines** — only logs in this Mac/PC's `~/.claude/` are read; nothing is synced
- **Web claude.ai usage** — different product, not Claude Code

### Account attribution

Claude Code writes all sessions to the same local directory (`~/.claude/projects/`) regardless of which Claude account is signed in. If you switch between accounts (e.g. Max, Pro, Teams, Enterprise, or an API key) on the same machine, **all of their sessions land in one pile** and the JSONL records contain **no account, org, or plan identifier**. This dashboard therefore cannot:

- split usage by account
- tell you which account paid for which session
- detect that you switched plans

If you need per-account attribution, you'll need to track that yourself (e.g. by separating projects per account, or by inspecting timestamps against when you switched).

---

## Requirements

- Python 3.8+
- No third-party Python packages — uses only the standard library (`sqlite3`, `http.server`, `json`, `pathlib`)
- The dashboard page loads Chart.js from a public CDN (`cdn.jsdelivr.net`); offline use will show empty charts

> Anyone running Claude Code already has Python installed.

## Quick Start

No `pip install`, no virtual environment, no build step.

### Windows
```
git clone https://github.com/Launchmetrics/claude-code-usage
cd claude-code-usage
python cli.py dashboard
```

### macOS / Linux
```
git clone https://github.com/Launchmetrics/claude-code-usage
cd claude-code-usage
python3 cli.py dashboard
```

---

## Setup for non-technical users (macOS)

If you've never used the Terminal before, follow these steps. They take about 5 minutes.

### 1. Check that Python is installed

1. Press `Cmd+Space`, type **Terminal**, press Enter. A black/white window opens.
2. Paste this command and press Enter:
   ```
   python3 --version
   ```
3. You should see something like `Python 3.11.4`. Anything 3.8 or higher is fine.
4. If you see `command not found: python3`, install Python from [python.org/downloads](https://www.python.org/downloads/) (pick the macOS installer), then come back to step 2.

### 2. Install and run the dashboard

In the same Terminal window, paste these two commands one at a time. Each one may take a few seconds.

```
git clone https://github.com/Launchmetrics/claude-code-usage
cd claude-code-usage
python3 cli.py dashboard
```

The first run will scan your Claude Code history. This takes a few seconds for light users and several minutes if you've been using Claude Code heavily — you'll see progress in the Terminal as files are processed.

When it's done, your browser opens automatically to `http://localhost:8080` showing the dashboard.

### 3. If something goes wrong

| Error message | Fix |
|---|---|
| `command not found: git` | Run `xcode-select --install` and click "Install" when the popup appears. Then retry. |
| `command not found: python3` | Install Python from [python.org/downloads](https://www.python.org/downloads/). |
| `Address already in use` | Another program is using port 8080. Run with a different port: `PORT=9000 python3 cli.py dashboard` |
| Browser doesn't open automatically | Open it manually and go to `http://localhost:8080` |

### 4. Running it again later

Open Terminal and run:

```
cd claude-code-usage
python3 cli.py dashboard
```

The dashboard will pick up any new Claude Code activity automatically.

---

## Usage

> On macOS/Linux, use `python3` instead of `python` in all commands below.

```
# Scan JSONL files and populate the database (~/.claude/usage.db)
python cli.py scan

# Show today's usage summary by model (in terminal)
python cli.py today

# Show the last 7 days (per-day breakdown + by-model totals)
python cli.py week

# Show all-time statistics (in terminal)
python cli.py stats

# Scan + open browser dashboard at http://localhost:8080
python cli.py dashboard

# Custom host and port via environment variables
HOST=0.0.0.0 PORT=9000 python cli.py dashboard

# Scan a custom projects directory
python cli.py scan --projects-dir /path/to/transcripts
```

The scanner is incremental — it tracks each file's path and modification time, so re-running `scan` is fast and only processes new or changed files.

By default, the scanner checks both `~/.claude/projects/` and the Xcode Claude integration directory (`~/Library/Developer/Xcode/CodingAssistant/ClaudeAgentConfig/projects/`), skipping any that don't exist. Use `--projects-dir` to scan a custom location instead.

---

## How it works

Claude Code writes one JSONL file per session to `~/.claude/projects/`. Each line is a JSON record; `assistant`-type records contain:
- `message.usage.input_tokens` — raw prompt tokens
- `message.usage.output_tokens` — generated tokens
- `message.usage.cache_creation_input_tokens` — tokens written to prompt cache
- `message.usage.cache_read_input_tokens` — tokens served from prompt cache
- `message.model` — the model used (e.g. `claude-sonnet-4-6`)

`scanner.py` parses those files and stores the data in a SQLite database at `~/.claude/usage.db`.

`dashboard.py` serves a single-page dashboard on `localhost:8080` with Chart.js charts (loaded from CDN). It auto-refreshes every 30 seconds and supports model filtering with bookmarkable URLs. The bind address and port can be overridden with `HOST` and `PORT` environment variables (defaults: `localhost`, `8080`).

---

## Cost estimates

Costs are calculated using **Anthropic API pricing as of April 2026** ([claude.com/pricing#api](https://claude.com/pricing#api)).

**Only models whose name contains `opus`, `sonnet`, or `haiku` are included in cost calculations.** Local models, unknown models, and any other model names are excluded (shown as `n/a`).

| Model | Input | Output | Cache Write | Cache Read |
|-------|-------|--------|------------|-----------|
| claude-opus-4-7 | $5.00/MTok | $25.00/MTok | $6.25/MTok | $0.50/MTok |
| claude-opus-4-6 | $5.00/MTok | $25.00/MTok | $6.25/MTok | $0.50/MTok |
| claude-sonnet-4-6 | $3.00/MTok | $15.00/MTok | $3.75/MTok | $0.30/MTok |
| claude-haiku-4-5 | $1.00/MTok | $5.00/MTok | $1.25/MTok | $0.10/MTok |

> **Note:** These are API prices. If you use Claude Code via a Max or Pro subscription, your actual cost structure is different (subscription-based, not per-token).

### What "Est. Cost" actually means

Read the dashboard's cost figures as **"what this usage would cost on the API at today's prices"**, not as your actual spend. Specifically:

- **Subscription plans (Max, Pro, Teams, Enterprise):** you pay a flat fee with included usage, so the marginal per-session cost is effectively $0 within plan limits. The dollar value the dashboard prints is hypothetical.
- **API key (pay-as-you-go):** the estimate is roughly right, modulo the caveats below.
- **Pricing is frozen** at the April 2026 table above. Sessions from earlier (or later) are repriced at these rates regardless of what was charged at the time.
- **No volume / commitment discounts** are applied — enterprise contracts often have negotiated rates the dashboard cannot know about.
- **Mixed accounts** — because the dashboard cannot tell which account ran a session (see [Account attribution](#account-attribution)), it cannot apply different pricing assumptions per account.

What the cost number IS useful for:
- **Relative comparison** between projects, models, sessions, days — the *ratios* are meaningful even when the absolute dollar value is wrong.
- **Spotting expensive patterns** (e.g. a single session burning massive cache writes).
- **Sizing API spend** if you were considering migrating from a subscription to API-based billing.

---

## Files

| File | Purpose |
|------|---------|
| `scanner.py` | Parses JSONL transcripts, writes to `~/.claude/usage.db` |
| `dashboard.py` | HTTP server + single-page HTML/JS dashboard |
| `cli.py` | `scan`, `today`, `stats`, `dashboard` commands |
