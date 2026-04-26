# Design: README clarity, custom date range, scan progress, stale-data banner

**Date:** 2026-04-26
**Author:** Pau Montero (pau.montero@launchmetrics.com)
**Status:** Proposed

## Context

The Launchmetrics fork of `claude-code-usage` is being shared with colleagues across the company, with mixed technical levels. Three rough edges have surfaced:

1. **Setup is intimidating for non-technical colleagues.** The README assumes comfort with terminals, `git`, and Python. New users need a more guided path.
2. **The preset date ranges (Week / Month / 7d / 30d / 90d / All) don't cover every question.** "How much did we spend last quarter?" or "Show me last week (Mon–Fri)" requires arbitrary date picking.
3. **First scan is slow and silent.** `cli.py scan` can take several minutes for a heavy user's history. Currently it prints nothing until done, so users wonder if it hung.
4. **Stale data is invisible.** The dashboard's 30-second auto-refresh re-queries the DB but does NOT re-read JSONL files. If a user leaves the dashboard open for hours/days while continuing to use Claude Code, the numbers silently drift behind reality. The Rescan button exists but isn't visually called out.

Constraints carried from upstream:
- Pure stdlib (no `pip install`, no virtualenv).
- Single-page HTML embedded in `dashboard.py`.
- Chart.js loaded from public CDN; no other JS libraries.

---

## Feature 1: README "Setup for non-technical users"

### Goal
A colleague who has never opened Terminal should be able to follow the README from start to working dashboard without help.

### Scope
- Add a new section between **Quick Start** and **Usage**, titled **"Setup for non-technical users (macOS)"**.
- Walk through:
  1. **Verify Python is installed.** `python3 --version` → expect 3.8 or higher. If "command not found", link to python.org's macOS installer.
  2. **Open Terminal.** `Cmd+Space` → type "Terminal" → Enter.
  3. **Paste the setup commands.** Two commands (`git clone …` then `cd … && python3 cli.py dashboard`). Note that the dashboard will print a URL like `http://localhost:8080`; the browser should open automatically.
  4. **What if something goes wrong?** Three common failures with one-line fixes:
     - `command not found: git` → install Xcode Command Line Tools (`xcode-select --install`).
     - `command not found: python3` → install from python.org.
     - `Address already in use` → set a different port: `PORT=9000 python3 cli.py dashboard`.

### Non-goals
- Windows-specific walkthrough (Launchmetrics colleagues are on Macs; the existing Quick Start covers Windows enough for the technical minority).
- Screenshots (rot quickly when UI changes).
- Video.

### Files
- `README.md` only.

---

## Feature 2: Custom date range picker

### Goal
Let users pick an arbitrary `[from, to]` date range, in addition to the existing presets, with bookmarkable URLs.

### UX
- Existing preset row: `This Week | This Month | 7d | 30d | 90d | All`.
- Add a new **"Custom…"** button at the end of the row.
- Clicking **"Custom…"** toggles a small form below the row:
  - `From: <input type="date">`
  - `To:   <input type="date">`
  - `Apply` button
- Defaults: `From` = 30 days ago, `To` = today (matches current default range).
- Native browser date pickers — no JS library, no styling work beyond what the rest of the page uses.
- After Apply, the **"Custom…"** button shows the active range as its label, e.g. `Custom: 2026-03-01 → 2026-04-15`, so users can see the active range at a glance.

### URL persistence
- `?range=custom&from=YYYY-MM-DD&to=YYYY-MM-DD`
- Existing presets continue to use `?range=week|month|7d|30d|90d|all`.
- On page load, if `range=custom`, the form is pre-populated and the chart filters apply immediately.
- Existing `history.replaceState` URL persistence is reused.

### Implementation
- `getRangeBounds(range)` in `dashboard.py` HTML/JS gains a `custom` branch:
  ```javascript
  if (range === 'custom') {
    const params = new URLSearchParams(window.location.search);
    return {
      start: params.get('from'),
      end:   params.get('to'),
    };
  }
  ```
- Apply button writes `?range=custom&from=…&to=…` and re-runs the existing filter pipeline.
- All existing chart/table filters already accept `{start, end}`, so no downstream changes.

### Edge cases
- `From > To` → swap silently before applying.
- `From > today` → clamp to today.
- Either field empty → Apply is disabled.
- Invalid dates → browser native `<input type="date">` prevents this; no extra validation needed.

### Non-goals
- Calendar widget library (jQuery UI / Pikaday / Flatpickr) — native `<input type="date">` is good enough and stays within the no-deps constraint.
- Quick chips inside the custom form (e.g. "Last quarter") — presets handle the common cases.
- Time-of-day precision — date granularity matches the rest of the dashboard.

### Files
- `dashboard.py` only (HTML + embedded JS).

---

## Feature 3: Scan progress in terminal

### Goal
During `python3 cli.py scan`, the user should see continuous progress so they know the process is alive and can estimate when it will finish.

### Behavior
- Before scanning, list all candidate JSONL files (already happens internally) and print a one-line header: `Found 312 JSONL files. Scanning…`.
- For each file processed, update a single line in place using carriage return:
  ```
  Scanning… 47 / 312 files (15%)
  ```
- After the last file, print a final summary:
  ```
  Done. 312 files, 18,452 turns, 4.2s.
  ```
- If no new files (incremental scan with nothing new), print a single line: `No new or modified files. Database is up to date.`

### Implementation
- `scanner.scan()` accepts a new optional argument `progress_callback: Optional[Callable[[int, int], None]] = None`.
  - Called as `progress_callback(processed_count, total_count)` after each file.
  - When `None` (existing call sites that don't pass it), behavior is unchanged.
- `cli.py`'s `cmd_scan` and `cmd_dashboard` pass a callback that prints to stderr using carriage return:
  ```python
  def progress(done, total):
      sys.stderr.write(f"\rScanning… {done} / {total} files ({100*done//total}%)")
      sys.stderr.flush()
  ```
- After scan completes, print final summary with a leading `\n` to clear the in-place line.
- TTY detection: if `sys.stderr.isatty()` is `False` (e.g. piped to a log file), fall back to one log line every 50 files instead of carriage-return updates.

### Non-goals (deferred)
- Server-first `cmd_dashboard` (start HTTP server, then run scan in a background thread).
- `/api/scan-status` endpoint and dashboard polling banner.
- Browser auto-opens before scan finishes.

These are deferred until colleagues actually report the empty-dashboard-during-first-scan as a pain point. The terminal progress alone solves the "is it hung?" question, which is the primary fear.

### Files
- `scanner.py` — add `progress_callback` parameter to `scan()`.
- `cli.py` — wire the callback in `cmd_scan` and `cmd_dashboard`.

---

## Feature 4: Stale-data banner

### Goal
When dashboard data is older than 24 hours, surface a clear warning so users know to rescan. Replaces what would otherwise be a documentation note in the README.

### Behavior
- When data is fresh (< 24h since last scan): no banner. UI unchanged.
- When data is stale (≥ 24h since last scan): yellow warning banner near the top of the dashboard:
  > ⚠ Last scan was N hours/days ago. Recent Claude Code activity may not appear. **[Rescan now]**
- Clicking **Rescan now** triggers the same `/api/rescan` POST as the existing top-right Rescan button. After success, the banner disappears (or shows "Rescanned just now" briefly, then dismisses).
- Banner dismissal does not persist — if the user dismisses and the data is still stale on next page load, it reappears. (Banner is a state, not a notification.)

### "Last scan time" — source of truth
- New table `scan_meta` with a single row:
  ```sql
  CREATE TABLE IF NOT EXISTS scan_meta (
      key TEXT PRIMARY KEY,
      value TEXT NOT NULL
  );
  ```
- `scanner.scan()` writes `('last_scan_at', <unix_timestamp>)` after each successful scan completes (full or incremental).
- `INSERT OR REPLACE` keeps the row count at one.
- `CREATE TABLE IF NOT EXISTS` is the only migration needed — existing DBs gain the table on next scan.

### API
- `/api/stats` response gains a field:
  ```json
  { ..., "last_scan_at": 1714060800, "data_age_seconds": 7200 }
  ```
- `data_age_seconds` is computed server-side as `now - last_scan_at`. If `last_scan_at` is missing (DB existed before this feature), return `null` and the dashboard treats it as fresh (no banner) — the next scan will populate it.

### Frontend
- On `loadData()`, if `data_age_seconds >= 86400`, render the banner.
- Format the age human-readably: `2 hours ago`, `1 day ago`, `5 days ago`. Single function `formatRelativeTime(seconds)`.
- Banner uses existing CSS variables (no new colors): yellow/warning tone via inline styles or a new `.banner-warn` class.

### Edge cases
- Brand new DB, never scanned: `last_scan_at` is null. No banner. (User just ran `cli.py dashboard` which scans first; data is by definition fresh.)
- Clock skew (DB modified in the future): treat negative `data_age_seconds` as 0.
- After clicking Rescan and waiting for completion, the next `loadData()` call will see the new `last_scan_at` and the banner will disappear automatically.

### Non-goals
- Configurable threshold (24h is fine for now; revisit if anyone asks).
- Auto-rescan (no — explicit user action keeps the model simple and avoids surprise multi-minute background scans).
- Push notification or sound.

### Files
- `scanner.py` — create `scan_meta` table, write `last_scan_at` at end of `scan()`.
- `dashboard.py` — `/api/stats` returns `last_scan_at` and `data_age_seconds`; HTML/JS adds banner element + `formatRelativeTime` helper.

---

## Architecture impact

| Component | Change | Risk |
|-----------|--------|------|
| `README.md` | New section | None — docs only |
| `dashboard.py` HTML/JS | "Custom…" button + form, `getRangeBounds` extension, URL parsing | Low — additive, reuses existing filter pipeline |
| `scanner.py` | Optional `progress_callback` argument; `scan_meta` table with `last_scan_at` | Low — backwards-compatible default; `CREATE TABLE IF NOT EXISTS` handles migration |
| `cli.py` | Pass progress callback in two commands | Low |
| `dashboard.py` `/api/stats` | New fields `last_scan_at`, `data_age_seconds` | Low — additive |
| `dashboard.py` HTML/JS | Stale-data banner element + `formatRelativeTime` helper | Low |

One new SQLite table (`scan_meta`). No new dependencies. No new HTTP endpoints.

---

## Testing

New automated tests are added to the existing test files (`tests/test_scanner.py`, `tests/test_dashboard.py`, `tests/test_cli.py`). No new test files are created. Tests are written test-first during implementation. The existing 91-test suite must continue to pass.

### Feature 1 — README setup section
- **Manual only.** No automated tests (it's documentation). Acceptance: a non-technical colleague follows the README cold and reaches a working dashboard without help.

### Feature 2 — Custom date range
**New tests in `tests/test_dashboard.py`:**
- `test_get_range_bounds_custom_returns_from_to_from_query`: simulate a request with `?range=custom&from=2026-04-01&to=2026-04-15`, assert the server-side filter applied to charts/tables uses those bounds.
  - Note: `getRangeBounds` itself is JS in the embedded HTML and not directly unit-testable from Python. The server-observable behavior (filtered API responses) is what we test.
- `test_custom_range_swaps_when_from_after_to`: `?range=custom&from=2026-04-15&to=2026-04-01` → response same as if `from`/`to` were swapped.
- `test_custom_range_clamps_future_from_to_today`: `?range=custom&from=<future>&to=<future>` → server returns empty result set (or clamps to today; behavior to confirm during implementation).
- `test_custom_range_missing_from_or_to_falls_back_to_default`: malformed `?range=custom` (no from/to) → server falls back to default range without error.

**Manual:**
- Click "Custom…", pick a range, click Apply. Verify all charts and tables filter correctly.
- Bookmark the URL with `?range=custom&from=…&to=…`, reload, verify range restored and form pre-populated.

### Feature 3 — Scan progress
**New tests in `tests/test_scanner.py`:**
- `test_scan_calls_progress_callback_per_file`: pass a mock callback, scan a fixture directory with N files, assert the callback was called exactly N times with `(done, total)` where `done` increases monotonically from 1 to N and `total == N`.
- `test_scan_with_no_callback_works_unchanged`: existing behavior — calling `scan()` without `progress_callback` still works (backwards compatibility).

**New tests in `tests/test_cli.py`:**
- `test_cmd_scan_writes_progress_to_stderr`: capture stderr, run `cmd_scan` against a fixture, assert progress lines appear (e.g. matches `Scanning… \d+ / \d+ files`).
- `test_cmd_scan_non_tty_stderr_skips_carriage_return_updates`: redirect stderr to a non-TTY buffer, assert output uses newline-separated lines (no `\r`).

**Manual:**
- Run `python3 cli.py scan` on real history; verify in-place line updates render correctly in iTerm and Terminal.app.
- Pipe stderr to a file (`python3 cli.py scan 2>scan.log`); verify periodic log lines and no carriage-return artifacts.

### Feature 4 — Stale-data banner
**New tests in `tests/test_scanner.py`:**
- `test_scan_writes_last_scan_at_to_scan_meta`: after `scan()` completes, query `scan_meta` table, assert `last_scan_at` row exists and value is within a few seconds of `time.time()`.
- `test_scan_meta_table_created_if_missing`: open a DB without `scan_meta`, run `scan()`, assert the table is created (migration behavior).
- `test_repeat_scan_updates_last_scan_at`: run `scan()` twice with a small sleep; assert second `last_scan_at` is greater than first.

**New tests in `tests/test_dashboard.py`:**
- `test_api_stats_includes_last_scan_at_and_data_age_seconds`: hit `/api/stats`, assert response contains both fields with sensible types.
- `test_api_stats_returns_null_last_scan_at_when_missing`: with a fresh DB lacking `scan_meta`, `/api/stats` returns `last_scan_at: null` (no error).
- `test_api_stats_clamps_negative_data_age_to_zero`: write a future `last_scan_at` to `scan_meta`, hit `/api/stats`, assert `data_age_seconds == 0`.

**Manual:**
- Artificially age the DB (`UPDATE scan_meta SET value = '<old timestamp>' WHERE key = 'last_scan_at'`), reload dashboard, verify yellow banner appears with the expected phrasing.
- Click "Rescan now" in the banner, wait for scan to complete, verify banner disappears.
- Brand-new DB (no `scan_meta` row) → no banner shown.

---

## Rollout

Single PR to the Launchmetrics fork (`main` branch). All three features are independent and additive — no migration, no breaking changes for existing users. Tag a new release `v0.2.0-launchmetrics.1`.

Upstream (`phuryn/claude-usage`) PR is optional — README changes are Launchmetrics-specific (mention the fork), but Features 2 and 3 are general improvements worth contributing back. Decide after the fork lands.
