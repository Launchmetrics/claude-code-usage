# README Clarity, Custom Date Range, Scan Progress, Stale-Data Banner — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship four small, additive features for the v0.2.0-launchmetrics.1 release: a non-technical setup section in the README, a custom date range picker, terminal progress during scans, and a stale-data banner in the dashboard.

**Architecture:** All four features land in a single PR. They are independent and additive — no schema migration beyond `CREATE TABLE IF NOT EXISTS`, no new HTTP endpoints, no new dependencies. The existing `/api/data` response is extended with two new fields (`last_scan_at`, `data_age_seconds`); the embedded HTML/JS in `dashboard.py` gains a banner element and a "Custom…" range button; `scanner.scan()` gains an optional `progress_callback` parameter and writes `last_scan_at` to a new `scan_meta` table.

**Tech Stack:** Python 3.8 stdlib (sqlite3, http.server), embedded vanilla JavaScript, Chart.js loaded from CDN (unchanged). No new dependencies.

**Spec:** `docs/superpowers/specs/2026-04-26-readme-calendar-scan-ux-design.md`

---

## File Structure

| File | What changes |
|------|--------------|
| `README.md` | New section "Setup for non-technical users (macOS)" between Quick Start and Usage |
| `scanner.py` | Add `scan_meta` table to `init_db`; `scan()` writes `last_scan_at`; `scan()` accepts optional `progress_callback` arg |
| `dashboard.py` | `get_dashboard_data()` reads `last_scan_at` and adds `data_age_seconds`; HTML adds Custom range button + date inputs + stale-data banner; JS handles `range=custom` and renders banner |
| `cli.py` | `cmd_scan` and `cmd_dashboard` pass a stderr-printing progress callback |
| `tests/test_scanner.py` | New tests for `progress_callback`, `scan_meta` schema, `last_scan_at` updates |
| `tests/test_dashboard.py` | New tests for new `/api/data` fields, custom range query string handling |
| `tests/test_cli.py` | New tests for `cmd_scan` stderr progress output |

Each task lands as one commit. The order below ensures every commit leaves the test suite green.

---

## Task 1: README — Setup for non-technical users

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Add the new section between "Quick Start" and "Usage"**

In `README.md`, after the macOS/Linux Quick Start block (around line 68) and before the `---` and `## Usage` heading (around line 70), insert:

```markdown
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
```

- [ ] **Step 2: Verify Markdown renders correctly**

Run: `cat README.md | head -120` and visually confirm the new section is well-formed. (Optional: open `README.md` in a Markdown previewer.)

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs: add Setup for non-technical users section to README"
```

---

## Task 2: scanner.py — `scan_meta` table

**Files:**
- Modify: `scanner.py:41-91` (`init_db` function)
- Test: `tests/test_scanner.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_scanner.py` (at the end of the file, before any `if __name__` block, otherwise at end):

```python
def test_init_db_creates_scan_meta_table(tmp_path):
    """init_db should create a scan_meta table with key/value columns."""
    import scanner
    db_path = tmp_path / "test.db"
    conn = scanner.get_db(db_path)
    scanner.init_db(conn)

    # scan_meta should exist with expected columns
    cols = conn.execute("PRAGMA table_info(scan_meta)").fetchall()
    col_names = [c[1] for c in cols]
    assert col_names == ["key", "value"], f"unexpected columns: {col_names}"

    # key should be PRIMARY KEY
    pk_cols = [c for c in cols if c[5] == 1]  # column 5 is `pk` flag
    assert len(pk_cols) == 1 and pk_cols[0][1] == "key"
    conn.close()


def test_init_db_scan_meta_idempotent(tmp_path):
    """Calling init_db twice should not raise (CREATE TABLE IF NOT EXISTS)."""
    import scanner
    db_path = tmp_path / "test.db"
    conn = scanner.get_db(db_path)
    scanner.init_db(conn)
    scanner.init_db(conn)  # second call should be a no-op
    conn.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_scanner.py::test_init_db_creates_scan_meta_table tests/test_scanner.py::test_init_db_scan_meta_idempotent -v`

Expected: FAIL with `no such table: scan_meta` or empty column list.

- [ ] **Step 3: Implement the schema change**

In `scanner.py`, modify `init_db()`. Find the existing `executescript` call (around line 42) and add the `scan_meta` table to the script. Replace:

```python
def init_db(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            session_id      TEXT PRIMARY KEY,
            ...
        );

        CREATE TABLE IF NOT EXISTS turns (
            ...
        );

        CREATE TABLE IF NOT EXISTS processed_files (
            path    TEXT PRIMARY KEY,
            mtime   REAL,
            lines   INTEGER
        );

        CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(session_id);
        CREATE INDEX IF NOT EXISTS idx_turns_timestamp ON turns(timestamp);
        CREATE INDEX IF NOT EXISTS idx_sessions_first ON sessions(first_timestamp);
    """)
```

with the same content plus the new table at the end of the script (right before the closing `"""`):

```python
        CREATE TABLE IF NOT EXISTS scan_meta (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
```

(Keep the existing tables and indexes exactly as they are — only add the new `scan_meta` block.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_scanner.py -v`

Expected: ALL pass, including the two new tests and the existing scanner tests.

- [ ] **Step 5: Commit**

```bash
git add scanner.py tests/test_scanner.py
git commit -m "feat(scanner): add scan_meta table for tracking last_scan_at"
```

---

## Task 3: scanner.py — Write `last_scan_at` after each scan

**Files:**
- Modify: `scanner.py` (`scan` function, near line 519)
- Test: `tests/test_scanner.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_scanner.py`:

```python
def test_scan_writes_last_scan_at_to_scan_meta(tmp_path):
    """After scan() completes, scan_meta should contain a last_scan_at row."""
    import scanner
    import time
    db_path = tmp_path / "test.db"
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()
    # Empty projects dir — scan should still complete and write last_scan_at

    before = time.time()
    scanner.scan(projects_dir=projects_dir, db_path=db_path, verbose=False)
    after = time.time()

    conn = scanner.get_db(db_path)
    row = conn.execute(
        "SELECT value FROM scan_meta WHERE key = 'last_scan_at'"
    ).fetchone()
    conn.close()

    assert row is not None, "last_scan_at row missing from scan_meta"
    ts = float(row["value"])
    assert before <= ts <= after, f"last_scan_at {ts} outside [{before}, {after}]"


def test_scan_updates_last_scan_at_on_repeat(tmp_path):
    """Running scan() twice should update last_scan_at to the more recent time."""
    import scanner
    import time
    db_path = tmp_path / "test.db"
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()

    scanner.scan(projects_dir=projects_dir, db_path=db_path, verbose=False)
    conn = scanner.get_db(db_path)
    first = float(conn.execute(
        "SELECT value FROM scan_meta WHERE key = 'last_scan_at'"
    ).fetchone()["value"])
    conn.close()

    time.sleep(0.05)  # ensure measurable time delta

    scanner.scan(projects_dir=projects_dir, db_path=db_path, verbose=False)
    conn = scanner.get_db(db_path)
    second = float(conn.execute(
        "SELECT value FROM scan_meta WHERE key = 'last_scan_at'"
    ).fetchone()["value"])
    conn.close()

    assert second > first, f"last_scan_at did not advance: {first} -> {second}"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_scanner.py::test_scan_writes_last_scan_at_to_scan_meta tests/test_scanner.py::test_scan_updates_last_scan_at_on_repeat -v`

Expected: FAIL with `last_scan_at row missing from scan_meta`.

- [ ] **Step 3: Implement the write**

In `scanner.py`, find the end of the `scan()` function — there's a `conn.close()` followed by `return {...}` near line 519. Add a write to `scan_meta` just before `conn.close()`. The relevant section currently looks like:

```python
    if verbose:
        print(f"\nScan complete:")
        print(f"  New files:     {new_files}")
        print(f"  Updated files: {updated_files}")
        print(f"  Skipped files: {skipped_files}")
        print(f"  Turns added:   {total_turns}")
        print(f"  Sessions seen: {len(total_sessions)}")

    conn.close()
    return {"new": new_files, "updated": updated_files, "skipped": skipped_files,
            "turns": total_turns, "sessions": len(total_sessions)}
```

Change it to:

```python
    if verbose:
        print(f"\nScan complete:")
        print(f"  New files:     {new_files}")
        print(f"  Updated files: {updated_files}")
        print(f"  Skipped files: {skipped_files}")
        print(f"  Turns added:   {total_turns}")
        print(f"  Sessions seen: {len(total_sessions)}")

    conn.execute(
        "INSERT OR REPLACE INTO scan_meta (key, value) VALUES (?, ?)",
        ("last_scan_at", str(time.time())),
    )
    conn.commit()

    conn.close()
    return {"new": new_files, "updated": updated_files, "skipped": skipped_files,
            "turns": total_turns, "sessions": len(total_sessions)}
```

Also ensure `import time` is present at the top of `scanner.py` (check the existing imports — most stdlib scanners already have it; if not, add `import time` to the top-of-file imports).

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_scanner.py -v`

Expected: ALL pass.

- [ ] **Step 5: Commit**

```bash
git add scanner.py tests/test_scanner.py
git commit -m "feat(scanner): write last_scan_at to scan_meta on every scan"
```

---

## Task 4: dashboard.py — Expose `last_scan_at` and `data_age_seconds`

**Files:**
- Modify: `dashboard.py:15-121` (`get_dashboard_data` function)
- Test: `tests/test_dashboard.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_dashboard.py`. First, locate or add the existing imports and ensure `import sqlite3, time, urllib.request, json` are available (the test file already imports most of these — add what's missing).

```python
def test_api_data_includes_last_scan_at_and_data_age(tmp_path, monkeypatch):
    """After a scan, /api/data response should include last_scan_at and data_age_seconds."""
    import dashboard
    import scanner
    import json

    db_path = tmp_path / "test.db"
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()
    scanner.scan(projects_dir=projects_dir, db_path=db_path, verbose=False)

    monkeypatch.setattr(dashboard, "DB_PATH", db_path)
    data = dashboard.get_dashboard_data(db_path=db_path)

    assert "last_scan_at" in data
    assert "data_age_seconds" in data
    assert isinstance(data["last_scan_at"], (int, float))
    assert isinstance(data["data_age_seconds"], (int, float))
    assert data["data_age_seconds"] >= 0


def test_api_data_returns_null_last_scan_at_when_missing(tmp_path):
    """If scan_meta has no last_scan_at row, last_scan_at should be null."""
    import dashboard
    import scanner

    db_path = tmp_path / "test.db"
    # Initialize DB schema but do NOT run a scan, so scan_meta stays empty
    conn = scanner.get_db(db_path)
    scanner.init_db(conn)
    conn.close()

    data = dashboard.get_dashboard_data(db_path=db_path)
    assert data["last_scan_at"] is None
    assert data["data_age_seconds"] is None


def test_api_data_clamps_negative_data_age_to_zero(tmp_path):
    """If last_scan_at is in the future (clock skew), data_age_seconds should be 0."""
    import dashboard
    import scanner
    import time

    db_path = tmp_path / "test.db"
    conn = scanner.get_db(db_path)
    scanner.init_db(conn)
    future = time.time() + 3600  # 1 hour in the future
    conn.execute(
        "INSERT OR REPLACE INTO scan_meta (key, value) VALUES (?, ?)",
        ("last_scan_at", str(future)),
    )
    conn.commit()
    conn.close()

    data = dashboard.get_dashboard_data(db_path=db_path)
    assert data["data_age_seconds"] == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_dashboard.py::test_api_data_includes_last_scan_at_and_data_age tests/test_dashboard.py::test_api_data_returns_null_last_scan_at_when_missing tests/test_dashboard.py::test_api_data_clamps_negative_data_age_to_zero -v`

Expected: FAIL with `KeyError: 'last_scan_at'`.

- [ ] **Step 3: Implement the new fields in `get_dashboard_data`**

In `dashboard.py`, modify the return statement of `get_dashboard_data` (currently around lines 115-121). The existing return looks like:

```python
    return {
        "all_models":      all_models,
        "daily_by_model":  daily_by_model,
        "hourly_by_model": hourly_by_model,
        "sessions_all":    sessions_all,
        "generated_at":    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
```

Just before that return, query `scan_meta` and compute `data_age_seconds`:

```python
    # ── Scan staleness signal ────────────────────────────────────────────────
    last_scan_row = conn.execute(
        "SELECT value FROM scan_meta WHERE key = 'last_scan_at'"
    ).fetchone() if _table_exists(conn, "scan_meta") else None
    if last_scan_row:
        last_scan_at = float(last_scan_row["value"])
        data_age_seconds = max(0.0, time.time() - last_scan_at)
    else:
        last_scan_at = None
        data_age_seconds = None

    return {
        "all_models":       all_models,
        "daily_by_model":   daily_by_model,
        "hourly_by_model":  hourly_by_model,
        "sessions_all":     sessions_all,
        "generated_at":     datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "last_scan_at":     last_scan_at,
        "data_age_seconds": data_age_seconds,
    }
```

Add a small helper at module level (near the top of `dashboard.py`, after imports):

```python
def _table_exists(conn, name):
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return row is not None
```

Also ensure `import time` is present at the top of `dashboard.py`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_dashboard.py -v`

Expected: ALL pass, including the three new tests.

- [ ] **Step 5: Commit**

```bash
git add dashboard.py tests/test_dashboard.py
git commit -m "feat(dashboard): expose last_scan_at and data_age_seconds in /api/data"
```

---

## Task 5: dashboard.py — Stale-data banner UI

**Files:**
- Modify: `dashboard.py` (HTML template — banner element + CSS; JS — render logic)

This task is UI-only and tested manually. No automated tests. (Headless-browser testing would require new dependencies, which violates the no-deps constraint.)

- [ ] **Step 1: Add CSS for the banner**

In `dashboard.py`, find the existing CSS block (it starts around line 130 with `<style>` and contains rules like `.range-btn`). Add the following rules near the other component styles (e.g. after the `#rescan-btn` rules around line 150):

```css
  #stale-banner { display: none; background: rgba(251,191,36,0.12); border: 1px solid rgba(251,191,36,0.5); color: #fbbf24; padding: 10px 16px; border-radius: 6px; margin: 12px 0; font-size: 14px; align-items: center; gap: 12px; }
  #stale-banner.visible { display: flex; }
  #stale-banner button { background: rgba(251,191,36,0.2); border: 1px solid rgba(251,191,36,0.6); color: #fbbf24; padding: 4px 12px; border-radius: 4px; cursor: pointer; font-size: 13px; font-weight: 600; }
  #stale-banner button:hover { background: rgba(251,191,36,0.3); }
  #stale-banner button:disabled { opacity: 0.5; cursor: not-allowed; }
```

- [ ] **Step 2: Add the banner element to the HTML body**

Find the existing `<div class="range-group">` (around line 237). Just *above* that div (so the banner appears above the range buttons), insert:

```html
  <div id="stale-banner">
    <span>⚠ <span id="stale-banner-text">Last scan was a long time ago.</span> Recent Claude Code activity may not appear.</span>
    <button id="stale-banner-rescan" onclick="rescanFromBanner()">Rescan now</button>
  </div>
```

- [ ] **Step 3: Add JS helpers for relative time and banner rendering**

In the embedded JS section (the `<script>` block — find a sensible location near other utility functions, e.g. just before the `triggerRescan` function around line 1168), add:

```javascript
// ── Stale-data banner ──────────────────────────────────────────────────────
function formatRelativeTime(seconds) {
  if (seconds < 60) return 'just now';
  if (seconds < 3600) {
    const m = Math.floor(seconds / 60);
    return m === 1 ? '1 minute ago' : m + ' minutes ago';
  }
  if (seconds < 86400) {
    const h = Math.floor(seconds / 3600);
    return h === 1 ? '1 hour ago' : h + ' hours ago';
  }
  const d = Math.floor(seconds / 86400);
  return d === 1 ? '1 day ago' : d + ' days ago';
}

const STALE_THRESHOLD_SECONDS = 86400;  // 24 hours

function updateStaleBanner(dataAgeSeconds) {
  const banner = document.getElementById('stale-banner');
  const text = document.getElementById('stale-banner-text');
  if (dataAgeSeconds == null || dataAgeSeconds < STALE_THRESHOLD_SECONDS) {
    banner.classList.remove('visible');
    return;
  }
  text.textContent = 'Last scan was ' + formatRelativeTime(dataAgeSeconds) + '.';
  banner.classList.add('visible');
}

async function rescanFromBanner() {
  const btn = document.getElementById('stale-banner-rescan');
  btn.disabled = true;
  btn.textContent = 'Rescanning…';
  try {
    await fetch('/api/rescan', { method: 'POST' });
    await loadData();  // refreshes data_age_seconds → updateStaleBanner hides banner
  } catch (e) {
    btn.textContent = 'Error';
    setTimeout(() => { btn.disabled = false; btn.textContent = 'Rescan now'; }, 2000);
    return;
  }
  btn.disabled = false;
  btn.textContent = 'Rescan now';
}
```

- [ ] **Step 4: Wire `updateStaleBanner` into `loadData`**

Find the `loadData` function (it includes `await fetch('/api/data')` around line 1188 and parses `d.all_models` etc.). After the data is parsed and stored, add a call to `updateStaleBanner(d.data_age_seconds)`. The existing block looks roughly like:

```javascript
async function loadData() {
  // ...
  const resp = await fetch('/api/data');
  const d = await resp.json();
  rawData = d;
  // ... existing buildFilterUI / applyFilter etc.
}
```

Add `updateStaleBanner(d.data_age_seconds);` right after `rawData = d;` (or after `buildFilterUI(d.all_models)` — anywhere within `loadData` after the response is parsed is fine).

- [ ] **Step 5: Manual smoke test**

Run the dashboard:

```bash
python3 cli.py dashboard
```

Open `http://localhost:8080`. Verify no banner appears (data is fresh).

In a separate terminal, age the DB:

```bash
sqlite3 ~/.claude/usage.db "UPDATE scan_meta SET value = '$(($(date +%s) - 200000))' WHERE key = 'last_scan_at';"
```

Reload the dashboard. Yellow banner should appear with text like `Last scan was 2 days ago.` Click "Rescan now". After scan completes (a second or two), the banner should disappear.

- [ ] **Step 6: Commit**

```bash
git add dashboard.py
git commit -m "feat(dashboard): add stale-data banner when last scan >= 24h ago"
```

---

## Task 6: scanner.py — `progress_callback` parameter

**Files:**
- Modify: `scanner.py:322` (`scan` signature) and the file-processing loop near line 348
- Test: `tests/test_scanner.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_scanner.py`:

```python
def test_scan_calls_progress_callback_per_file(tmp_path):
    """progress_callback should be called once per file, with monotonically increasing done."""
    import scanner

    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()
    # Create three minimal JSONL files (empty is fine — scan still iterates them)
    for i in range(3):
        sub = projects_dir / f"proj{i}"
        sub.mkdir()
        (sub / f"session{i}.jsonl").write_text("")

    db_path = tmp_path / "test.db"
    calls = []
    def cb(done, total):
        calls.append((done, total))

    scanner.scan(projects_dir=projects_dir, db_path=db_path,
                 verbose=False, progress_callback=cb)

    assert len(calls) == 3, f"expected 3 callback calls, got {len(calls)}"
    assert [c[0] for c in calls] == [1, 2, 3], f"done values not monotonic: {calls}"
    assert all(c[1] == 3 for c in calls), f"total values inconsistent: {calls}"


def test_scan_without_callback_works_unchanged(tmp_path):
    """scan() called without progress_callback should behave exactly as before."""
    import scanner

    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()
    db_path = tmp_path / "test.db"

    # Should not raise
    result = scanner.scan(projects_dir=projects_dir, db_path=db_path, verbose=False)
    assert "new" in result
    assert "updated" in result
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_scanner.py::test_scan_calls_progress_callback_per_file tests/test_scanner.py::test_scan_without_callback_works_unchanged -v`

Expected: FAIL — `test_scan_calls_progress_callback_per_file` fails because `progress_callback` is not a parameter of `scan()`.

- [ ] **Step 3: Add `progress_callback` parameter to `scan()`**

In `scanner.py`, change the signature on line 322:

```python
def scan(projects_dir=None, projects_dirs=None, db_path=DB_PATH, verbose=True):
```

to:

```python
def scan(projects_dir=None, projects_dirs=None, db_path=DB_PATH, verbose=True, progress_callback=None):
```

Inside the file-processing loop, just *after* each file finishes processing — i.e. right after the `conn.commit()` on line 495 (which is inside the `for filepath in jsonl_files:` loop, after the `INSERT OR REPLACE INTO processed_files` block) — add:

```python
        if progress_callback is not None:
            progress_callback(jsonl_files.index(filepath) + 1, len(jsonl_files))
```

Wait — `jsonl_files.index(filepath)` is O(N) per call. Use enumerate instead. Restructure the loop top from:

```python
    for filepath in jsonl_files:
        try:
            mtime = os.path.getmtime(filepath)
        except OSError:
            continue
```

to:

```python
    total_files = len(jsonl_files)
    for file_index, filepath in enumerate(jsonl_files, start=1):
        try:
            mtime = os.path.getmtime(filepath)
        except OSError:
            if progress_callback is not None:
                progress_callback(file_index, total_files)
            continue
```

And replace the trailing per-file callback after `conn.commit()` near line 495 with:

```python
        if progress_callback is not None:
            progress_callback(file_index, total_files)
```

Note: also check the `continue` paths inside the loop — there's a `continue` near line 477 (`if line_count <= old_lines:`) that returns early. Make sure the callback fires for those files too. The cleanest approach is to wrap the loop body in a try/finally:

```python
    total_files = len(jsonl_files)
    for file_index, filepath in enumerate(jsonl_files, start=1):
        try:
            try:
                mtime = os.path.getmtime(filepath)
            except OSError:
                continue

            row = conn.execute(
                "SELECT mtime, lines FROM processed_files WHERE path = ?",
                (filepath,)
            ).fetchone()

            if row and abs(row["mtime"] - mtime) < 0.01:
                skipped_files += 1
                continue

            # ... existing if is_new / else branches unchanged ...

            # Record file as processed
            conn.execute("""
                INSERT OR REPLACE INTO processed_files (path, mtime, lines)
                VALUES (?, ?, ?)
            """, (filepath, mtime, line_count))
            conn.commit()
        finally:
            if progress_callback is not None:
                progress_callback(file_index, total_files)
```

This guarantees the callback fires exactly once per file regardless of which `continue` branch the loop took.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_scanner.py -v`

Expected: ALL pass, including the two new tests AND all previous scanner tests (no regression).

- [ ] **Step 5: Commit**

```bash
git add scanner.py tests/test_scanner.py
git commit -m "feat(scanner): add progress_callback parameter to scan()"
```

---

## Task 7: cli.py — Wire stderr progress callback

**Files:**
- Modify: `cli.py` (`cmd_scan` and `cmd_dashboard`)
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_cli.py`. First identify the existing pattern (the file already has tests that capture stdout/stderr via `subprocess.run` or direct calls). Add:

```python
def test_cmd_scan_writes_progress_to_stderr(tmp_path, capsys):
    """cmd_scan should print progress lines to stderr during a scan."""
    import cli

    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()
    sub = projects_dir / "proj"
    sub.mkdir()
    (sub / "session.jsonl").write_text("")

    db_path = tmp_path / "test.db"
    # Patch DB_PATH so cmd_scan writes here, not to ~/.claude/usage.db
    import scanner
    original_db_path = scanner.DB_PATH
    scanner.DB_PATH = db_path
    try:
        cli.cmd_scan(projects_dir=projects_dir)
    finally:
        scanner.DB_PATH = original_db_path

    captured = capsys.readouterr()
    # Progress should appear in stderr (carriage-return updates) or stdout
    combined = captured.out + captured.err
    assert "Scanning" in combined or "files" in combined, \
        f"no progress output found. stdout={captured.out!r} stderr={captured.err!r}"
```

Note: the cleanest test is hard to write without refactoring `cmd_scan`. The minimum bar is "some progress output reaches stderr/stdout when files exist". If `cmd_scan` already prints "Scan complete" etc. via `verbose=True`, that's not what we're testing — we're testing that the new in-place progress lines appear. Adjust the assertion to look for the specific marker string we'll print, e.g. `"Scanning…"` or `"/ "` (the divider in `47 / 312 files`).

A more targeted test:

```python
def test_cmd_scan_progress_callback_writes_in_place_to_stderr(tmp_path, capsys, monkeypatch):
    """When stderr is a TTY, progress should use carriage returns."""
    import cli, scanner, io

    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()
    for i in range(2):
        sub = projects_dir / f"p{i}"
        sub.mkdir()
        (sub / f"s{i}.jsonl").write_text("")

    db_path = tmp_path / "test.db"
    scanner_db = scanner.DB_PATH
    scanner.DB_PATH = db_path

    # Force isatty=True so we get carriage-return formatted output
    class FakeTTY(io.StringIO):
        def isatty(self): return True
    fake_err = FakeTTY()
    monkeypatch.setattr("sys.stderr", fake_err)

    try:
        cli.cmd_scan(projects_dir=projects_dir)
    finally:
        scanner.DB_PATH = scanner_db

    output = fake_err.getvalue()
    assert "\r" in output, f"expected carriage return in stderr output: {output!r}"
    assert "Scanning" in output, f"expected 'Scanning' in stderr output: {output!r}"


def test_cmd_scan_progress_non_tty_uses_newlines(tmp_path, monkeypatch):
    """When stderr is not a TTY, progress should use newlines (no \\r)."""
    import cli, scanner, io

    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()
    for i in range(60):  # > 50 files so periodic logging triggers at least once
        sub = projects_dir / f"p{i}"
        sub.mkdir()
        (sub / f"s{i}.jsonl").write_text("")

    db_path = tmp_path / "test.db"
    scanner_db = scanner.DB_PATH
    scanner.DB_PATH = db_path

    class FakeNonTTY(io.StringIO):
        def isatty(self): return False
    fake_err = FakeNonTTY()
    monkeypatch.setattr("sys.stderr", fake_err)

    try:
        cli.cmd_scan(projects_dir=projects_dir)
    finally:
        scanner.DB_PATH = scanner_db

    output = fake_err.getvalue()
    assert "\r" not in output, f"non-TTY output should not contain carriage returns: {output!r}"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_cli.py::test_cmd_scan_progress_callback_writes_in_place_to_stderr tests/test_cli.py::test_cmd_scan_progress_non_tty_uses_newlines -v`

Expected: FAIL — current `cmd_scan` doesn't print per-file progress.

- [ ] **Step 3: Add the progress callback to `cmd_scan`**

In `cli.py`, find `cmd_scan` (search for `def cmd_scan`). It's a wrapper around `scanner.scan(...)`. Modify it to construct a progress callback and pass it through. The current `cmd_scan` likely looks like:

```python
def cmd_scan(projects_dir=None):
    import scanner
    result = scanner.scan(projects_dir=projects_dir)
    return result
```

Replace it with:

```python
def cmd_scan(projects_dir=None):
    import scanner
    import sys

    is_tty = sys.stderr.isatty()

    def progress(done, total):
        if is_tty:
            pct = (100 * done // total) if total else 100
            sys.stderr.write(f"\rScanning… {done} / {total} files ({pct}%)")
            sys.stderr.flush()
        else:
            # Non-TTY: log every 50 files
            if done == 1 or done == total or done % 50 == 0:
                sys.stderr.write(f"Scanning… {done} / {total} files\n")

    result = scanner.scan(projects_dir=projects_dir, progress_callback=progress, verbose=False)

    if is_tty:
        sys.stderr.write("\n")  # clear the in-place line before final summary
    sys.stderr.write(
        f"Done. {result['new']} new, {result['updated']} updated, "
        f"{result['skipped']} skipped, {result['turns']} turns.\n"
    )
    return result
```

Note: we now pass `verbose=False` to `scanner.scan` so the existing per-file `[NEW]/[UPD]` prints don't conflict with the in-place progress. The summary is printed by `cmd_scan` instead.

- [ ] **Step 4: Update `cmd_dashboard` to use the same progress output**

`cmd_dashboard` currently calls `cmd_scan(projects_dir=projects_dir)` (around line 366) before starting the server. That's already correct — `cmd_scan` now prints progress, so no change is needed in `cmd_dashboard`. Verify by reading `cmd_dashboard` and confirming it goes through `cmd_scan`, not directly through `scanner.scan`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_cli.py -v`

Expected: ALL pass.

- [ ] **Step 6: Manual smoke test**

```bash
python3 cli.py scan
```

Expected output (TTY):

```
Scanning… 1 / 312 files (0%)
Scanning… 47 / 312 files (15%)
...
Scanning… 312 / 312 files (100%)
Done. 0 new, 312 updated, 0 skipped, 18452 turns.
```

The first line should *update in place* rather than scrolling.

- [ ] **Step 7: Commit**

```bash
git add cli.py tests/test_cli.py
git commit -m "feat(cli): show in-place scan progress in terminal"
```

---

## Task 8: dashboard.py — Custom date range button + form

**Files:**
- Modify: `dashboard.py` (HTML range buttons; JS `RANGE_LABELS`, `getRangeBounds`, `setRange`, `readURLRange`, `updateURL`)

This task is UI/JS-only. Tested manually + one server-side smoke test that confirms `?range=custom&from=…&to=…` doesn't break `/api/data`.

- [ ] **Step 1: Write the failing server-side smoke test**

Add to `tests/test_dashboard.py`:

```python
def test_api_data_handles_custom_range_query_string(tmp_path, monkeypatch):
    """/api/data should ignore unknown range/from/to query params (no 500)."""
    import dashboard
    import scanner
    import urllib.request, json, threading, http.server

    db_path = tmp_path / "test.db"
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()
    scanner.scan(projects_dir=projects_dir, db_path=db_path, verbose=False)
    monkeypatch.setattr(dashboard, "DB_PATH", db_path)

    # Use the existing pattern in this file for spinning up the server in a thread.
    # If the file already has a fixture for this, reuse it instead.
    # For now, just verify get_dashboard_data is range-agnostic (server-side
    # filtering happens client-side, not via query string):
    data = dashboard.get_dashboard_data(db_path=db_path)
    assert "all_models" in data  # any successful response shape is fine
```

Note: this is a thin test because range filtering is client-side. The point is to confirm the server doesn't crash when the URL has new query params.

- [ ] **Step 2: Run test to verify it passes (it should — no behavior change yet)**

Run: `python3 -m pytest tests/test_dashboard.py::test_api_data_handles_custom_range_query_string -v`

Expected: PASS (this test is regression protection, not driving behavior).

- [ ] **Step 3: Extend RANGE_LABELS, RANGE_TICKS, VALID_RANGES**

In `dashboard.py`, find around line 484:

```javascript
const RANGE_LABELS = { 'week': 'This Week', 'month': 'This Month', 'prev-month': 'Previous Month', '7d': 'Last 7 Days', '30d': 'Last 30 Days', '90d': 'Last 90 Days', 'all': 'All Time' };
const RANGE_TICKS  = { 'week': 7, 'month': 15, 'prev-month': 15, '7d': 7, '30d': 15, '90d': 13, 'all': 12 };
const VALID_RANGES = Object.keys(RANGE_LABELS);
```

Change to:

```javascript
const RANGE_LABELS = { 'week': 'This Week', 'month': 'This Month', 'prev-month': 'Previous Month', '7d': 'Last 7 Days', '30d': 'Last 30 Days', '90d': 'Last 90 Days', 'all': 'All Time', 'custom': 'Custom' };
const RANGE_TICKS  = { 'week': 7, 'month': 15, 'prev-month': 15, '7d': 7, '30d': 15, '90d': 13, 'all': 12, 'custom': 15 };
const VALID_RANGES = Object.keys(RANGE_LABELS);
```

- [ ] **Step 4: Extend `getRangeBounds` to handle `custom`**

In `dashboard.py`, modify `getRangeBounds` (around line 497). Current:

```javascript
function getRangeBounds(range) {
  if (range === 'all') return { start: null, end: null };
  const today = new Date();
  const iso = d => d.toISOString().slice(0, 10);
  if (range === 'week') {
    // ...
  }
  if (range === 'month') {
    // ...
  }
  if (range === 'prev-month') {
    // ...
  }
  const days = range === '7d' ? 7 : range === '30d' ? 30 : 90;
  const d = new Date();
  d.setDate(d.getDate() - days);
  return { start: iso(d), end: null };
}
```

Add the custom branch right after the `if (range === 'all')` line:

```javascript
function getRangeBounds(range) {
  if (range === 'all') return { start: null, end: null };
  if (range === 'custom') {
    const params = new URLSearchParams(window.location.search);
    let from = params.get('from');
    let to   = params.get('to');
    // Swap if reversed
    if (from && to && from > to) [from, to] = [to, from];
    // Clamp future from to today
    const today = new Date().toISOString().slice(0, 10);
    if (from && from > today) from = today;
    return { start: from || null, end: to || null };
  }
  const today = new Date();
  const iso = d => d.toISOString().slice(0, 10);
  // ... rest unchanged ...
}
```

- [ ] **Step 5: Add the Custom button + date input form to the HTML**

In `dashboard.py`, find the `<div class="range-group">` block (around line 237):

```html
  <div class="range-group">
    <button class="range-btn" data-range="week" onclick="setRange('week')">This Week</button>
    <button class="range-btn" data-range="month" onclick="setRange('month')">This Month</button>
    <button class="range-btn" data-range="prev-month" onclick="setRange('prev-month')">Prev Month</button>
    <button class="range-btn" data-range="7d"  onclick="setRange('7d')">7d</button>
    <button class="range-btn" data-range="30d" onclick="setRange('30d')">30d</button>
    <button class="range-btn" data-range="90d" onclick="setRange('90d')">90d</button>
    <button class="range-btn" data-range="all" onclick="setRange('all')">All</button>
  </div>
```

Add a Custom button at the end of the group, plus a separate hidden form below:

```html
  <div class="range-group">
    <button class="range-btn" data-range="week" onclick="setRange('week')">This Week</button>
    <button class="range-btn" data-range="month" onclick="setRange('month')">This Month</button>
    <button class="range-btn" data-range="prev-month" onclick="setRange('prev-month')">Prev Month</button>
    <button class="range-btn" data-range="7d"  onclick="setRange('7d')">7d</button>
    <button class="range-btn" data-range="30d" onclick="setRange('30d')">30d</button>
    <button class="range-btn" data-range="90d" onclick="setRange('90d')">90d</button>
    <button class="range-btn" data-range="all" onclick="setRange('all')">All</button>
    <button class="range-btn" data-range="custom" onclick="toggleCustomRangeForm()">Custom…</button>
  </div>
  <div id="custom-range-form" style="display:none; margin-top:8px; gap:8px; align-items:center; font-size:13px;">
    <label>From: <input type="date" id="custom-from"></label>
    <label>To: <input type="date" id="custom-to"></label>
    <button onclick="applyCustomRange()" style="padding:4px 12px; background:var(--card); border:1px solid var(--border); color:var(--text); border-radius:4px; cursor:pointer;">Apply</button>
  </div>
```

Add CSS for the form's flex layout (next to the existing `.range-btn` rules around line 163):

```css
  #custom-range-form { display: flex; }
  #custom-range-form[hidden], #custom-range-form.hidden { display: none !important; }
```

Actually simpler: keep the inline `style="display:none"` initial state and toggle via JS classes. The JS will do the work.

- [ ] **Step 6: Add JS for toggling the form and applying the range**

Near the existing `setRange` function in the embedded JS (around line 529), add:

```javascript
function toggleCustomRangeForm() {
  const form = document.getElementById('custom-range-form');
  const isHidden = form.style.display === 'none' || !form.style.display;
  if (isHidden) {
    // Pre-populate inputs from current URL or defaults
    const params = new URLSearchParams(window.location.search);
    const fromInput = document.getElementById('custom-from');
    const toInput   = document.getElementById('custom-to');
    const today = new Date().toISOString().slice(0, 10);
    const thirtyAgo = (() => {
      const d = new Date(); d.setDate(d.getDate() - 30);
      return d.toISOString().slice(0, 10);
    })();
    fromInput.value = params.get('from') || thirtyAgo;
    toInput.value   = params.get('to')   || today;
    form.style.display = 'flex';
  } else {
    form.style.display = 'none';
  }
}

function applyCustomRange() {
  const from = document.getElementById('custom-from').value;
  const to   = document.getElementById('custom-to').value;
  if (!from || !to) return;
  // Update URL with from/to params, then call setRange('custom')
  const url = new URL(window.location);
  url.searchParams.set('range', 'custom');
  url.searchParams.set('from', from);
  url.searchParams.set('to', to);
  history.replaceState(null, '', url);
  setRange('custom');
  // Update the Custom button label to show the active range
  const btn = document.querySelector('.range-btn[data-range="custom"]');
  if (btn) btn.textContent = 'Custom: ' + from + ' → ' + to;
}
```

- [ ] **Step 7: Update `updateURL` to preserve from/to when range is custom**

Find the `updateURL` function. It should handle the case where `selectedRange === 'custom'` by NOT stripping `from` and `to`. Inspect the existing implementation:

```bash
grep -n "function updateURL" dashboard.py
```

A typical pattern is:

```javascript
function updateURL() {
  const url = new URL(window.location);
  url.searchParams.set('range', selectedRange);
  history.replaceState(null, '', url);
}
```

If that's all it does, it already preserves `from`/`to` by default. But verify it doesn't call `delete('from')` etc. If it does, conditionally skip the deletes when `selectedRange === 'custom'`.

- [ ] **Step 8: Update `readURLRange` and page-load logic**

`readURLRange` (around line 524) checks `VALID_RANGES.includes(p)`. Since `custom` is now in `VALID_RANGES`, it's already accepted. But on page load, if `range=custom` is in the URL, the form should be visible and pre-populated. Find the page-init code (around line 1203 where `setRange` is called from `DOMContentLoaded` or `loadData`) and add:

```javascript
// In the init block where setRange is called from URL:
const initialRange = readURLRange();
if (initialRange === 'custom') {
  const params = new URLSearchParams(window.location.search);
  const from = params.get('from');
  const to   = params.get('to');
  if (from && to) {
    document.getElementById('custom-from').value = from;
    document.getElementById('custom-to').value   = to;
    document.getElementById('custom-range-form').style.display = 'flex';
    const btn = document.querySelector('.range-btn[data-range="custom"]');
    if (btn) btn.textContent = 'Custom: ' + from + ' → ' + to;
  }
}
setRange(initialRange);
```

- [ ] **Step 9: Manual smoke test**

```bash
python3 cli.py dashboard
```

1. Click "Custom…" → form appears with default From=30 days ago, To=today.
2. Pick a custom range (e.g. 2026-04-01 to 2026-04-15) → click Apply.
3. Verify URL now has `?range=custom&from=2026-04-01&to=2026-04-15`.
4. Verify all charts and tables filter to that range.
5. Verify the Custom button label changed to `Custom: 2026-04-01 → 2026-04-15`.
6. Bookmark the URL, reload → range restored, form pre-populated.
7. Click another preset (e.g. 7d) → custom button reverts to `Custom…` (or stays with the active range; either is acceptable).
8. Click "Custom…" with from > to (e.g. From=2026-04-15, To=2026-04-01) → Apply silently swaps them; charts show the correct range.
9. Click "Custom…" with empty fields → Apply does nothing (silently ignored).

- [ ] **Step 10: Commit**

```bash
git add dashboard.py tests/test_dashboard.py
git commit -m "feat(dashboard): add Custom date range picker"
```

---

## Task 9: Final integration test + version bump

**Files:**
- Optionally: a top-level version constant (if one exists)

- [ ] **Step 1: Run the full test suite**

```bash
python3 -m pytest -v
```

Expected: all tests pass (the original 91 + new tests added in this plan; total should be 100+).

- [ ] **Step 2: End-to-end manual test**

```bash
rm ~/.claude/usage.db   # only if you want to test full first-scan flow; else skip
python3 cli.py dashboard
```

Verify in this order:
1. Terminal shows `Scanning… N / M files` updating in place during the scan.
2. After scan, terminal shows `Done. ...` summary.
3. Browser opens to `http://localhost:8080`.
4. No stale-data banner (data is fresh).
5. Click each preset range button — charts update.
6. Click "Custom…" → form appears → pick range → Apply → charts update.
7. Bookmark a custom-range URL, reload, verify state restored.
8. In another terminal, age the DB and reload the page → banner appears → click Rescan now → banner disappears.

- [ ] **Step 3: Update CHANGELOG.md**

Add a new section at the top of `CHANGELOG.md`:

```markdown
## 2026-04-26

- Add "Setup for non-technical users (macOS)" section to README
- Add Custom date range picker (calendar-based, additive to preset buttons)
- Add in-place scan progress in terminal (TTY) and periodic logging (non-TTY)
- Add stale-data banner to dashboard when last scan is older than 24 hours
- New `scan_meta` table tracks `last_scan_at` (auto-created via CREATE TABLE IF NOT EXISTS)
```

- [ ] **Step 4: Commit and push**

```bash
git add CHANGELOG.md
git commit -m "docs: update CHANGELOG for v0.2.0-launchmetrics.1 features"
git push origin main
```

- [ ] **Step 5: Tag the release**

```bash
git tag -a v0.2.0-launchmetrics.1 -m "Custom date range, scan progress, stale-data banner, README clarity"
git push origin v0.2.0-launchmetrics.1
```

---

## Notes for the engineer

- **TDD discipline:** every code-changing task starts with a failing test. Run the test, see it fail, then implement, then see it pass. Do not skip the "see it fail" step.
- **One commit per task:** each task ends with a commit. If you discover a problem mid-task, fix it before committing.
- **Backwards compatibility:** existing DBs (without `scan_meta`) get the new table on next scan. The dashboard tolerates `last_scan_at == null` and shows no banner. No migration script is needed.
- **No new dependencies:** keep all changes within the Python stdlib and the existing CDN-loaded Chart.js. Do not add a calendar library — `<input type="date">` is good enough.
- **Existing patterns:** the codebase uses `monkeypatch.setattr(dashboard, "DB_PATH", db_path)` for DB-redirection in tests; reuse that pattern. The HTTP test pattern in `tests/test_dashboard.py` (line ~132) spins up a real `ThreadingHTTPServer` in a thread — copy that approach for any new HTTP-level tests.
