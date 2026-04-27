# Daily Activity Summaries — Design Spec

**Date:** 2026-04-27
**Status:** Approved for implementation planning
**Target release:** v0.3.0-launchmetrics.1

---

## Goal

Add a "Daily Activities" view to the dashboard that infers what the user actually worked on each day per project, by sending each day's user prompts to Claude Haiku via the local `claude` CLI and presenting the result as 2–5 bulleted activities per (day, project) cell.

This complements the existing token/cost metrics — those tell you *how much* you used Claude Code; this tells you *what for*.

---

## Why

The dashboard currently shows tokens, cost, and session counts. None of those tell the user (or their team) what was actually accomplished. Asking an LLM to summarize a day's prompts produces a topic-level digest that turns the dashboard into a lightweight personal/team retrospective tool.

---

## Constraints

- **No new pip dependencies.** Pure stdlib + the existing `claude` CLI already installed by every user of this tool.
- **Use the user's existing Claude account** — no API key management. Calls go through the `claude` CLI subprocess, inheriting OAuth/Enterprise auth.
- **Cheap by default.** First-init cost should be measured in pennies, not dollars. Incremental refresh in cents.
- **Non-blocking degradation.** If `claude` is not installed or fails, the rest of the dashboard works fine; the new section gracefully shows an error banner.
- **No recursive cost inflation.** The summarizer must not write its own sessions back into `~/.claude/projects/`, where the scanner would re-ingest them as "user activity".

---

## Architecture overview

```
                    ┌────────────────────────────────┐
JSONL files ──┐     │   summarizer.py (new module)   │
              │     │                                │
              ▼     │  • collect_prompts(day, proj)  │
         scanner.py │  • rank_cells_by_cost()        │
        (existing)  │  • run_claude(prompts) ──┐     │     ┌─────────┐
              │     │  • cache_summary()       │─────┼────▶│ claude  │
              ▼     │                          │     │     │  CLI    │
        usage.db ◀──┴──────────────────────────┴─────┘     └─────────┘
              │       (new table: daily_summaries)
              │
              ▼
        dashboard.py ── /api/daily-summaries ──▶ browser
                              (existing      ┐
                               + new endpt)  │  new "Daily Activities"
                                             │  section below charts
                                             │  honors range filter
```

Three trigger points invoke the summarizer:

1. **Eager pass** — `cli.py dashboard` startup, after the scan, summarizes the top 20% of (day, project) cells by cost (capped at 50). Blocking with `Summarizing… N/M` progress bar matching the scan UX.
2. **Lazy pass** — `/api/daily-summaries?date=YYYY-MM-DD` runs the summarizer on demand for cells the user expands in the UI that aren't cached.
3. **Incremental refresh** — subsequent dashboard runs detect prompt-hash mismatches in the cached eager set and re-summarize only the cells whose underlying prompts changed.

---

## Components

### `summarizer.py` (new module)

Pure logic + the subprocess call. No HTTP. No global state beyond reading config from env vars.

Public functions:
- `collect_prompts(date, project_path, db_path) -> str` — gather all `type=user` records for that (date, project), filter noise, dedupe, sort, concat, cap at 4 KB, return as a single string.
- `prompt_hash(text) -> str` — sha256 hex digest, used as cache invalidation signal.
- `rank_cells_by_cost(db_path) -> list[(date, project, cost)]` — return top `min(80th-percentile, SUMMARY_MAX_CELLS=50)` cells, sorted descending by cost.
- `summarize_cell(date, project, db_path) -> dict` — orchestrates: collect, hash, check cache, call `claude`, write back. Returns `{"activities": [...], "cached": bool, "error": str|None}`.
- `run_claude(prompt_text) -> tuple[list[str]|None, str|None]` — subprocess call; returns `(activities_list, None)` on success or `(None, error_code)` on failure.

### New SQLite table — `daily_summaries`

```sql
CREATE TABLE IF NOT EXISTS daily_summaries (
    summary_date  TEXT NOT NULL,    -- 'YYYY-MM-DD'
    project_path  TEXT NOT NULL,    -- the cwd as stored in sessions
    prompt_hash   TEXT NOT NULL,    -- sha256 of the filtered+sorted prompts
    activities    TEXT NOT NULL,    -- JSON array of bullets
    cost_usd      REAL NOT NULL,    -- denormalized at write-time, used for ranking
    created_at    REAL NOT NULL,    -- epoch seconds
    PRIMARY KEY (summary_date, project_path)
);
```

Created idempotently in `scanner.init_db()`. Errors are *not* cached — failed cells get retried on the next pass.

### `cli.py` — new phase after scan

After `cmd_scan` completes (and `last_scan_at` is written to `scan_meta`), `cmd_dashboard` runs the eager summarizer pass with a progress callback identical in shape to the scan progress: `Summarizing… N / M cells`. The pass is sequential (one subprocess at a time) — no parallelism — to keep the user's terminal output legible and avoid hammering Claude.

`cmd_scan` itself does **not** trigger summarization (preserves "scan is fast and incremental" semantics). Only `cmd_dashboard` does.

### `dashboard.py` — new HTTP endpoint + new HTML section

- New endpoint: `GET /api/daily-summaries?date=YYYY-MM-DD`
  - Returns `{"date": "...", "cells": [{"project": "...", "cost": ..., "activities": [...]|null, "error": str|null, "eager": bool}]}`
  - For uncached cells, calls `summarizer.summarize_cell()` synchronously *within the request* (relies on `ThreadingHTTPServer` so other requests aren't blocked).
- New HTML `<section>` placed between the existing charts and the Sessions/Models tables.
- New JS (vanilla, no library): fetches summaries on day-row expansion via native `<details>`/`<summary>` toggle.

---

## Data flow per cell

```
1. collect_prompts(date, project)
   – scanner.usage.db gives us all sessions for that (date, project)
   – walk the corresponding JSONL files, pull type=user records
   – filter: drop length<5; drop in skiplist {yes,no,ok,exit,y,n,continue,...}
   – dedupe exact matches
   – sort, concat with newlines, cap at 4 KB

2. h = sha256(text)

3. existing = SELECT prompt_hash FROM daily_summaries WHERE date=? AND project=?
   if existing == h:  return cached  (cache hit)

4. activities, err = run_claude(text)

5. if err:                return {"error": err}    (NOT cached; retried later)
   if activities is None: return {"error": "unknown"}
   else:
       INSERT OR REPLACE INTO daily_summaries
         (summary_date, project_path, prompt_hash, activities, cost_usd, created_at)
         VALUES (?, ?, ?, ?, ?, ?)
       return {"activities": activities, "cached": False}
```

---

## CLI invocation

```python
subprocess.run([
    "claude", "-p", user_prompt_text,
    "--model", os.environ.get("SUMMARY_MODEL", "haiku"),
    "--output-format", "json",
    "--json-schema", SUMMARY_SCHEMA_JSON,
    "--no-session-persistence",   # critical: don't recurse into our own scanner
    "--disable-slash-commands",   # don't interpret prompts as skill calls
    "--system-prompt", SYSTEM_PROMPT,
], capture_output=True, text=True, timeout=60)
```

**System prompt (frozen constant):**

> You analyze user prompts from one day's work in one project and infer the main activities. Output 2 to 5 concrete activity bullets describing features, topics, or goals — not file names or implementation minutiae. No fluff, no greetings, no meta-commentary.

**JSON schema for structured output:**

```json
{"type":"object",
 "properties":{"activities":{"type":"array","items":{"type":"string"},
                              "minItems":1,"maxItems":5}},
 "required":["activities"]}
```

We do **not** use `--bare`: it forces `ANTHROPIC_API_KEY` auth and ignores OAuth, breaking the "use your Claude account" requirement.

We do **not** use `--max-budget-usd`: that flag only works for API-key users. Cost is governed by Enterprise quota plus our own input/output caps.

---

## Eager-set selection (the "interesting cells" rule)

Computed once per dashboard launch, after the scan:

```python
cells = [(date, project, cost) for ... in usage.db]   # all cells with cost > 0
threshold = percentile([c.cost for c in cells], 80)
eager = sorted([c for c in cells if c.cost >= threshold],
               key=lambda c: -c.cost)[:SUMMARY_MAX_CELLS]
```

Defaults:
- Percentile: **80** (top 20%)
- Hard cap: `SUMMARY_MAX_CELLS=50` (overridable via env var)
- Model: `SUMMARY_MODEL=haiku` (overridable via env var)

The cap protects against unbounded first-init runs for users with years of history. Top-20% of 1000 cells would be 200 — too many. The cap caps wall-time at roughly `50 × 5s = ~4 min`.

---

## UI layout

A new section between the charts and the Sessions table:

```
─── Daily Activities ────────────────────────────────────────────────
▶ 2026-04-26   3 projects   $4.21
▼ 2026-04-25   2 projects   $9.18
    claude-costs-dashboard                               ★  $8.86
      • Implemented Custom date range picker with URL persistence
      • Fixed timezone off-by-one in default range bounds
      • Added stale-data banner triggered when last scan ≥ 24h
    debatecoach                                             $0.32
      • Added .settings file to enable Superpowers plugin
      • Configured initial project workspace
▶ 2026-04-24   1 project   $0.18
─────────────────────────────────────────────────────────────────────
```

- Day rows: native `<details>`/`<summary>`; collapsed by default.
- Star (★) marks eager-set cells; absence indicates lazy.
- Honors the existing range filter (This Week / 7d / 30d / Custom / etc.) — same selection drives this section.
- Days with zero activity are hidden entirely.

### Row state matrix

| State | Renders as |
|---|---|
| Cached | Bullets render immediately; ★ if eager |
| Lazy + day collapsed | No fetch, nothing rendered |
| Lazy + day expanded for the first time | Inline spinner: `Summarizing… (≈3s)` → bullets replace it |
| Errored | `Summary unavailable: <error_code>` + small "Retry" link that re-issues the fetch |
| `claude` not installed | Section shows banner above the day list: *"Daily Activities requires the `claude` CLI on PATH."* |

### Lazy fetch flow

When a day is expanded, the JS sends one `GET /api/daily-summaries?date=YYYY-MM-DD` request.
- The server returns cached cells + triggers summarization for any uncached cells of that date sequentially within the same request.
- The browser shows one spinner per day until the response lands (~3–15 s for a busy day).
- "Block on day" was preferred over "stream per cell" for cleaner perceived UX (one spinner, all bullets appear together).

No frontend libraries; native `<details>` for collapse, vanilla JS for fetch + render.

---

## Error handling

| Layer | Failure | Behavior |
|---|---|---|
| `summarizer.run_claude()` | subprocess error / timeout / parse error | Returns `(None, error_code)`; never raises |
| Eager pass during startup | Any cell errors out | Print `Skipped: <date> <project> — <error>` to stderr; continue; dashboard still starts |
| `/api/daily-summaries` | summarizer returns error | JSON response includes per-cell error; UI renders "Summary unavailable" with retry |
| `claude` not on PATH | First call returns `FileNotFoundError` mapped to `claude_not_installed` | Eager pass aborts cleanly with one stderr message; section banner explains; everything else works |
| 60s timeout | `subprocess.TimeoutExpired` | error_code `timeout`; not cached; auto-retry next run |
| Non-zero exit | Captured stderr first line | error_code `cli_error: <stderr>`; not cached |
| Invalid JSON output | `json.JSONDecodeError` | error_code `parse_error`; not cached (rare with `--json-schema`) |

---

## Cost protection

Multiple bounds stack:
- `SUMMARY_MAX_CELLS=50` cap on eager set
- 4 KB input cap per call
- `maxItems: 5` output cap via JSON schema
- Lazy fetch only on user expansion (no scroll-triggered prefetch)
- Cache hit on prompt-hash match → no call

Worst-case first init: 50 × ~1.5 KB input × ~150 output tokens at Haiku rates ≈ **$0.08**. Day-by-day refresh: typically 0–2 cells per scan ≈ pennies.

---

## Configuration (env vars)

| Var | Default | Effect |
|---|---|---|
| `SUMMARY_MODEL` | `haiku` | Claude model alias passed to `claude --model` |
| `SUMMARY_MAX_CELLS` | `50` | Hard cap on eager-set size |

Plus existing `HOST`, `PORT`, `--projects-dir` — unchanged.

---

## Testing

| Layer | What's tested | How |
|---|---|---|
| `summarizer.collect_prompts()` | filtering, dedup, capping, hashing | Unit tests with hand-crafted JSONL fixtures |
| `summarizer.run_claude()` | subprocess flag construction, error mapping | Mock `subprocess.run`; assert on argv; cover timeout / non-zero / parse-error / FileNotFoundError paths |
| `summarizer.rank_cells_by_cost()` | percentile + cap logic | Unit tests with synthetic cost lists |
| `init_db` | new `daily_summaries` table created idempotently | Same pattern as the existing `scan_meta` test |
| `/api/daily-summaries` | endpoint returns cached + triggers lazy correctly | HTTP test against `ThreadingHTTPServer` with mocked summarizer |
| UI | expand/collapse, bullet render, error states | Manual checklist (no JS test harness in this repo, matches existing convention) |

---

## Out of scope (deferred)

- Multi-day summary aggregation ("what did I work on this week?")
- Exporting summaries to CSV/markdown
- Cross-project intent detection ("this all relates to GIS work")
- LLM-graded session ratings or quality scores
- Scheduling (auto-rescan + auto-resummarize on a cron)
- Parallel `claude` subprocesses (sequential is intentional for v1)

---

## Manual test plan (for the eventual implementation)

1. Fresh `~/.claude/usage.db` → run `python3 cli.py dashboard` → terminal shows `Scanning…` then `Summarizing…` then browser opens.
2. Daily Activities section visible below charts.
3. Click an eager (★) day — bullets render immediately, no spinner.
4. Click a non-eager day — spinner appears for ~3-15s, then bullets render.
5. Re-open browser — previously lazy-loaded day is instant (cached).
6. Touch a JSONL file with new user prompts on an eager day → re-run dashboard → that one cell re-summarizes (others skipped).
7. Rename `claude` (or `mv` it off PATH) → re-run dashboard → eager pass aborts with stderr message; dashboard still starts; Daily Activities banner shows "claude not installed".
8. Range filter still drives the section (switch to "Custom: 2026-04-01 → 2026-04-15"; only days in that window appear).
