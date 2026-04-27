# Daily Activity Summaries — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a "Daily Activities" view that uses the local `claude` CLI to summarize each day's user prompts per project as 2–5 bulleted activities, cached in SQLite.

**Architecture:** New `summarizer.py` module wraps a `claude -p` subprocess call. Eager pass at `cli.py dashboard` startup summarizes the top-20% (day, project) cells by cost (capped at 50). Lazy pass on `/api/daily-summaries` for cells the user expands. Cache invalidated by sha256 hash of input prompts.

**Tech Stack:** Python 3.8 stdlib (sqlite3, hashlib, subprocess, http.server), embedded vanilla JavaScript, the existing `claude` CLI on PATH. No new pip dependencies.

**Spec:** `docs/superpowers/specs/2026-04-27-daily-activity-summaries-design.md`

---

## File Structure

| File | What changes |
|------|--------------|
| `scanner.py` | Add `daily_summaries` table to `init_db()` |
| `summarizer.py` | **New module** — `prompt_hash`, `collect_prompts`, `rank_cells_by_cost`, `run_claude`, `summarize_cell` |
| `cli.py` | `cmd_dashboard` runs eager summarizer pass after the scan with TTY progress |
| `dashboard.py` | New `/api/daily-summaries` endpoint + new HTML section + new JS |
| `tests/test_scanner.py` | New test for `daily_summaries` table |
| `tests/test_summarizer.py` | **New file** — unit tests for every public function in `summarizer.py` |
| `tests/test_cli.py` | New test for eager pass progress callback in `cmd_dashboard` |
| `tests/test_dashboard.py` | New tests for `/api/daily-summaries` endpoint |
| `CHANGELOG.md` | New section for v0.3.0-launchmetrics.1 |

Each task lands as one commit. Order ensures every commit leaves the test suite green.

---

## Task 1: scanner.py — `daily_summaries` table

**Files:**
- Modify: `scanner.py` (`init_db` function)
- Test: `tests/test_scanner.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_scanner.py`:

```python
def test_init_db_creates_daily_summaries_table(tmp_path):
    db_path = tmp_path / "test.db"
    conn = scanner.init_db(db_path)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(daily_summaries)")}
    assert cols == {
        "summary_date", "project_path", "prompt_hash",
        "activities", "cost_usd", "created_at",
    }
    conn.close()


def test_init_db_daily_summaries_idempotent(tmp_path):
    db_path = tmp_path / "test.db"
    scanner.init_db(db_path).close()
    conn = scanner.init_db(db_path)  # second call must not raise
    conn.execute("SELECT 1 FROM daily_summaries").fetchall()
    conn.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_scanner.py::test_init_db_creates_daily_summaries_table -v`
Expected: FAIL with `sqlite3.OperationalError: no such table: daily_summaries`

- [ ] **Step 3: Add the table to `init_db`**

In `scanner.py`, find the multi-statement `executescript` call inside `init_db()` (around line 44–86, the block that contains `CREATE TABLE IF NOT EXISTS scan_meta`). Add this CREATE TABLE statement at the end of that script (just before the closing `"""`):

```sql
        CREATE TABLE IF NOT EXISTS daily_summaries (
            summary_date  TEXT NOT NULL,
            project_path  TEXT NOT NULL,
            prompt_hash   TEXT NOT NULL,
            activities    TEXT NOT NULL,
            cost_usd      REAL NOT NULL,
            created_at    REAL NOT NULL,
            PRIMARY KEY (summary_date, project_path)
        );
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_scanner.py -k daily_summaries -v`
Expected: 2 passed.

- [ ] **Step 5: Run full suite to confirm no regressions**

Run: `python3 -m pytest tests/ -q`
Expected: all 103 prior tests still pass + 2 new tests = 105 passed.

- [ ] **Step 6: Commit**

```bash
git add scanner.py tests/test_scanner.py
git commit -m "feat(scanner): add daily_summaries table for activity summaries"
```

---

## Task 2: summarizer.py — `prompt_hash` function

**Files:**
- Create: `summarizer.py`
- Test: `tests/test_summarizer.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_summarizer.py` with:

```python
import summarizer


def test_prompt_hash_is_deterministic():
    assert summarizer.prompt_hash("hello") == summarizer.prompt_hash("hello")


def test_prompt_hash_differs_on_change():
    assert summarizer.prompt_hash("hello") != summarizer.prompt_hash("hello!")


def test_prompt_hash_returns_hex_string():
    h = summarizer.prompt_hash("hello")
    assert isinstance(h, str)
    assert len(h) == 64  # sha256 hex digest length
    int(h, 16)  # valid hex


def test_prompt_hash_handles_unicode():
    summarizer.prompt_hash("hola — què tal?")  # must not raise
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_summarizer.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'summarizer'`

- [ ] **Step 3: Create the module skeleton + `prompt_hash`**

Create `summarizer.py`:

```python
"""
summarizer.py - Generate per-day activity summaries by calling the local
`claude` CLI on the day's user prompts. Cached in usage.db.
"""

import hashlib
import json
import os
import sqlite3
import subprocess
import time
from pathlib import Path

# ── Constants ────────────────────────────────────────────────────────────────

NOISE_SKIPLIST = {
    "yes", "no", "ok", "okay", "exit", "y", "n",
    "continue", "thanks", "thank you", "great", "alright",
}
MIN_PROMPT_LENGTH = 5
MAX_INPUT_BYTES = 4096
DEFAULT_MAX_CELLS = 50
DEFAULT_PERCENTILE = 80
DEFAULT_MODEL = "haiku"
SUBPROCESS_TIMEOUT = 60

SYSTEM_PROMPT = (
    "You analyze user prompts from one day's work in one project and infer "
    "the main activities. Output 2 to 5 concrete activity bullets describing "
    "features, topics, or goals — not file names or implementation minutiae. "
    "No fluff, no greetings, no meta-commentary."
)

SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "activities": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "maxItems": 5,
        }
    },
    "required": ["activities"],
}


# ── Public functions ─────────────────────────────────────────────────────────

def prompt_hash(text: str) -> str:
    """Stable sha256 hex digest of the prompt text — cache invalidation key."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_summarizer.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add summarizer.py tests/test_summarizer.py
git commit -m "feat(summarizer): add module skeleton and prompt_hash"
```

---

## Task 3: summarizer.py — `collect_prompts`

**Files:**
- Modify: `summarizer.py`
- Modify: `tests/test_summarizer.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_summarizer.py`:

```python
import json


def _write_jsonl(path, records):
    path.write_text("\n".join(json.dumps(r) for r in records))


def test_collect_prompts_filters_noise_and_dedupes(tmp_path):
    proj_dir = tmp_path / "-Users-test-myproj"
    proj_dir.mkdir()
    _write_jsonl(proj_dir / "session.jsonl", [
        {"type": "user", "timestamp": "2026-04-25T10:00:00Z",
         "message": {"content": "refactor the epic correlation script"}},
        {"type": "user", "timestamp": "2026-04-25T10:05:00Z",
         "message": {"content": "yes"}},                           # noise: skiplist
        {"type": "user", "timestamp": "2026-04-25T10:10:00Z",
         "message": {"content": "hi"}},                            # noise: too short
        {"type": "user", "timestamp": "2026-04-25T10:15:00Z",
         "message": {"content": "refactor the epic correlation script"}},  # dup
        {"type": "user", "timestamp": "2026-04-25T10:20:00Z",
         "message": {"content": "add unit tests for the new endpoint"}},
        {"type": "assistant", "timestamp": "2026-04-25T10:30:00Z",
         "message": {"content": "should not be included"}},        # wrong type
    ])
    text = summarizer.collect_prompts(
        date="2026-04-25", cwd="/Users/test/myproj", projects_dirs=[tmp_path],
    )
    lines = text.split("\n")
    assert "refactor the epic correlation script" in lines
    assert "add unit tests for the new endpoint" in lines
    assert "yes" not in lines
    assert "hi" not in lines
    assert "should not be included" not in lines
    # Dedup: each prompt appears exactly once
    assert lines.count("refactor the epic correlation script") == 1


def test_collect_prompts_extracts_from_content_list(tmp_path):
    proj_dir = tmp_path / "-Users-test-myproj"
    proj_dir.mkdir()
    _write_jsonl(proj_dir / "session.jsonl", [
        {"type": "user", "timestamp": "2026-04-25T10:00:00Z",
         "message": {"content": [
             {"type": "text", "text": "build a calendar picker for the dashboard"},
         ]}},
    ])
    text = summarizer.collect_prompts(
        date="2026-04-25", cwd="/Users/test/myproj", projects_dirs=[tmp_path],
    )
    assert text == "build a calendar picker for the dashboard"


def test_collect_prompts_filters_by_date(tmp_path):
    proj_dir = tmp_path / "-Users-test-myproj"
    proj_dir.mkdir()
    _write_jsonl(proj_dir / "session.jsonl", [
        {"type": "user", "timestamp": "2026-04-24T23:59:59Z",
         "message": {"content": "from yesterday morning"}},
        {"type": "user", "timestamp": "2026-04-25T00:00:00Z",
         "message": {"content": "from today midnight"}},
    ])
    text = summarizer.collect_prompts(
        date="2026-04-25", cwd="/Users/test/myproj", projects_dirs=[tmp_path],
    )
    assert text == "from today midnight"


def test_collect_prompts_caps_at_4kb(tmp_path):
    proj_dir = tmp_path / "-Users-test-myproj"
    proj_dir.mkdir()
    long_prompt = "x" * 1000
    records = [
        {"type": "user", "timestamp": "2026-04-25T10:00:00Z",
         "message": {"content": f"{long_prompt} {i}"}}
        for i in range(10)
    ]
    _write_jsonl(proj_dir / "s.jsonl", records)
    text = summarizer.collect_prompts(
        date="2026-04-25", cwd="/Users/test/myproj", projects_dirs=[tmp_path],
    )
    assert len(text.encode("utf-8")) <= summarizer.MAX_INPUT_BYTES


def test_collect_prompts_returns_empty_when_no_matches(tmp_path):
    text = summarizer.collect_prompts(
        date="2026-04-25", cwd="/Users/test/nonexistent",
        projects_dirs=[tmp_path],
    )
    assert text == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_summarizer.py::test_collect_prompts_filters_noise_and_dedupes -v`
Expected: FAIL with `AttributeError: module 'summarizer' has no attribute 'collect_prompts'`

- [ ] **Step 3: Implement `collect_prompts`**

Append to `summarizer.py` (after `prompt_hash`):

```python
def _is_noise(text: str) -> bool:
    t = text.strip().lower()
    return len(t) < MIN_PROMPT_LENGTH or t in NOISE_SKIPLIST


def _extract_prompt_text(rec: dict) -> str:
    msg = rec.get("message")
    if not isinstance(msg, dict):
        return ""
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                return part.get("text", "")
    return ""


def _encoded_dirname(cwd: str) -> str:
    """The convention Claude Code uses to name per-project subdirectories."""
    return cwd.replace("/", "-")


def collect_prompts(date: str, cwd: str, projects_dirs) -> str:
    """
    Walk JSONLs under each projects_dir/<encoded(cwd)>/ and collect type=user
    prompts whose timestamp starts with `date`. Filter noise, dedupe exact
    matches, sort for determinism, concat with newlines, cap at MAX_INPUT_BYTES.
    """
    dirname = _encoded_dirname(cwd)
    prompts = set()
    for root in projects_dirs:
        target = Path(root) / dirname
        if not target.exists():
            continue
        for jsonl in sorted(target.glob("*.jsonl")):
            try:
                with jsonl.open() as f:
                    for line in f:
                        try:
                            rec = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if rec.get("type") != "user":
                            continue
                        ts = rec.get("timestamp", "")
                        if not isinstance(ts, str) or not ts.startswith(date):
                            continue
                        text = _extract_prompt_text(rec)
                        if not text or _is_noise(text):
                            continue
                        prompts.add(text.strip())
            except OSError:
                continue
    if not prompts:
        return ""
    sorted_prompts = sorted(prompts)
    out, size = [], 0
    for p in sorted_prompts:
        encoded = p.encode("utf-8")
        # +1 for newline separator (none for first item, but worst-case bound)
        if size + len(encoded) + 1 > MAX_INPUT_BYTES:
            break
        out.append(p)
        size += len(encoded) + 1
    return "\n".join(out)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_summarizer.py -v`
Expected: 4 prior + 5 new = 9 passed.

- [ ] **Step 5: Commit**

```bash
git add summarizer.py tests/test_summarizer.py
git commit -m "feat(summarizer): implement collect_prompts with noise filtering"
```

---

## Task 4: summarizer.py — `rank_cells_by_cost`

**Files:**
- Modify: `summarizer.py`
- Modify: `tests/test_summarizer.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_summarizer.py`:

```python
def _seed_turns(db_path, rows):
    """rows: list of (timestamp, cwd, model, input, output, cache_read, cache_write)"""
    import scanner
    scanner.init_db(db_path).close()
    conn = sqlite3.connect(db_path)
    for ts, cwd, model, inp, out, cr, cw in rows:
        conn.execute("""
            INSERT INTO turns
              (session_id, timestamp, model, input_tokens, output_tokens,
               cache_read_tokens, cache_creation_tokens, cwd)
            VALUES ('s1', ?, ?, ?, ?, ?, ?, ?)
        """, (ts, model, inp, out, cr, cw, cwd))
    conn.commit()
    conn.close()


def test_rank_cells_groups_by_day_and_cwd(tmp_path):
    db = tmp_path / "u.db"
    _seed_turns(db, [
        ("2026-04-25T10:00:00Z", "/proj/A", "claude-haiku-4-5", 1_000_000, 0, 0, 0),
        ("2026-04-25T11:00:00Z", "/proj/A", "claude-haiku-4-5", 1_000_000, 0, 0, 0),
        ("2026-04-25T12:00:00Z", "/proj/B", "claude-haiku-4-5",   500_000, 0, 0, 0),
    ])
    cells = summarizer.rank_cells_by_cost(db, max_cells=10, percentile=0)
    by_key = {(d, c): cost for d, c, cost in cells}
    assert by_key[("2026-04-25", "/proj/A")] == pytest.approx(2.0, rel=0.01)
    assert by_key[("2026-04-25", "/proj/B")] == pytest.approx(0.5, rel=0.01)


def test_rank_cells_applies_percentile_threshold(tmp_path):
    db = tmp_path / "u.db"
    rows = []
    # 10 cells with linearly increasing cost
    for i in range(10):
        rows.append(
            (f"2026-04-{i+1:02d}T10:00:00Z", f"/proj/{i}",
             "claude-haiku-4-5", (i + 1) * 1_000_000, 0, 0, 0)
        )
    _seed_turns(db, rows)
    cells = summarizer.rank_cells_by_cost(db, max_cells=100, percentile=80)
    # 80th percentile of 10 items: top 20% = 2 items (indexes 8, 9)
    assert len(cells) == 2
    # sorted descending
    assert cells[0][2] > cells[1][2]


def test_rank_cells_caps_at_max_cells(tmp_path):
    db = tmp_path / "u.db"
    rows = [
        (f"2026-04-{i+1:02d}T10:00:00Z", f"/proj/{i}",
         "claude-haiku-4-5", 1_000_000, 0, 0, 0)
        for i in range(20)
    ]
    _seed_turns(db, rows)
    cells = summarizer.rank_cells_by_cost(db, max_cells=3, percentile=0)
    assert len(cells) == 3


def test_rank_cells_skips_zero_cost(tmp_path):
    db = tmp_path / "u.db"
    _seed_turns(db, [
        ("2026-04-25T10:00:00Z", "/proj/A", "unknown-model", 1_000_000, 0, 0, 0),
        ("2026-04-25T11:00:00Z", "/proj/B", "claude-haiku-4-5", 1_000_000, 0, 0, 0),
    ])
    cells = summarizer.rank_cells_by_cost(db, max_cells=10, percentile=0)
    cwds = {c[1] for c in cells}
    assert "/proj/A" not in cwds
    assert "/proj/B" in cwds


def test_rank_cells_empty_db(tmp_path):
    import scanner
    db = tmp_path / "u.db"
    scanner.init_db(db).close()
    assert summarizer.rank_cells_by_cost(db, max_cells=10) == []
```

Add `import pytest` and `import sqlite3` at the top of `tests/test_summarizer.py` if not already present.

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_summarizer.py::test_rank_cells_groups_by_day_and_cwd -v`
Expected: FAIL with `AttributeError`.

- [ ] **Step 3: Implement `rank_cells_by_cost`**

Append to `summarizer.py`:

```python
def rank_cells_by_cost(db_path, max_cells=None, percentile=None):
    """
    Returns a sorted list of (date, cwd, cost_usd) tuples for the eager set —
    cells whose cost is at or above the given percentile, capped at max_cells,
    sorted descending by cost. Skips cells with cost == 0 (unknown models).
    """
    if max_cells is None:
        max_cells = int(os.environ.get("SUMMARY_MAX_CELLS", str(DEFAULT_MAX_CELLS)))
    if percentile is None:
        percentile = DEFAULT_PERCENTILE
    from cli import calc_cost
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT
            substr(timestamp, 1, 10) AS day,
            cwd,
            model,
            input_tokens, output_tokens,
            cache_read_tokens, cache_creation_tokens
        FROM turns
        WHERE cwd IS NOT NULL AND cwd != ''
    """).fetchall()
    conn.close()
    cells = {}
    for r in rows:
        cost = calc_cost(
            r["model"],
            r["input_tokens"] or 0,
            r["output_tokens"] or 0,
            r["cache_read_tokens"] or 0,
            r["cache_creation_tokens"] or 0,
        )
        if cost <= 0:
            continue
        key = (r["day"], r["cwd"])
        cells[key] = cells.get(key, 0.0) + cost
    items = [(d, c, cost) for (d, c), cost in cells.items() if cost > 0]
    if not items:
        return []
    costs = sorted(cost for _, _, cost in items)
    pct_idx = min(int(len(costs) * (percentile / 100)), len(costs) - 1)
    threshold = costs[pct_idx]
    eager = [item for item in items if item[2] >= threshold]
    eager.sort(key=lambda c: -c[2])
    return eager[:max_cells]
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_summarizer.py -v`
Expected: 9 prior + 5 new = 14 passed.

- [ ] **Step 5: Commit**

```bash
git add summarizer.py tests/test_summarizer.py
git commit -m "feat(summarizer): add rank_cells_by_cost with percentile + cap"
```

---

## Task 5: summarizer.py — `run_claude` (subprocess + parsing)

**Files:**
- Modify: `summarizer.py`
- Modify: `tests/test_summarizer.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_summarizer.py`:

```python
import subprocess
from unittest.mock import patch, MagicMock


def _mock_claude_response(stdout, returncode=0):
    return MagicMock(returncode=returncode, stdout=stdout, stderr="")


def test_run_claude_parses_successful_json(monkeypatch):
    response = json.dumps({"result": json.dumps({
        "activities": ["Refactored X", "Added tests for Y"],
    })})
    with patch("subprocess.run", return_value=_mock_claude_response(response)):
        activities, err = summarizer.run_claude("some prompt", model="haiku")
    assert err is None
    assert activities == ["Refactored X", "Added tests for Y"]


def test_run_claude_constructs_argv_correctly(monkeypatch):
    response = json.dumps({"result": json.dumps({"activities": ["A"]})})
    with patch("subprocess.run", return_value=_mock_claude_response(response)) as m:
        summarizer.run_claude("hello", model="haiku")
    argv = m.call_args[0][0]
    assert argv[0] == "claude"
    assert "-p" in argv
    assert "hello" in argv
    assert "--model" in argv and "haiku" in argv
    assert "--no-session-persistence" in argv
    assert "--disable-slash-commands" in argv
    assert "--output-format" in argv and "json" in argv
    assert "--system-prompt" in argv


def test_run_claude_handles_file_not_found(monkeypatch):
    with patch("subprocess.run", side_effect=FileNotFoundError):
        activities, err = summarizer.run_claude("hi", model="haiku")
    assert activities is None
    assert err == "claude_not_installed"


def test_run_claude_handles_timeout(monkeypatch):
    with patch("subprocess.run",
               side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=60)):
        activities, err = summarizer.run_claude("hi", model="haiku")
    assert activities is None
    assert err == "timeout"


def test_run_claude_handles_nonzero_exit(monkeypatch):
    bad = MagicMock(returncode=1, stdout="", stderr="auth failed")
    with patch("subprocess.run", return_value=bad):
        activities, err = summarizer.run_claude("hi", model="haiku")
    assert activities is None
    assert err.startswith("cli_error:")
    assert "auth failed" in err


def test_run_claude_handles_invalid_json(monkeypatch):
    with patch("subprocess.run",
               return_value=_mock_claude_response("not json at all")):
        activities, err = summarizer.run_claude("hi", model="haiku")
    assert activities is None
    assert err == "parse_error"


def test_run_claude_handles_missing_activities_key(monkeypatch):
    response = json.dumps({"result": json.dumps({"unrelated": "field"})})
    with patch("subprocess.run", return_value=_mock_claude_response(response)):
        activities, err = summarizer.run_claude("hi", model="haiku")
    assert activities is None
    assert err == "parse_error"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_summarizer.py::test_run_claude_parses_successful_json -v`
Expected: FAIL with `AttributeError`.

- [ ] **Step 3: Implement `run_claude`**

Append to `summarizer.py`:

```python
def run_claude(prompt_text, model=None, timeout=SUBPROCESS_TIMEOUT):
    """
    Invoke `claude -p` with the given prompt text and structured-output schema.
    Returns (activities_list, None) on success or (None, error_code) on failure.
    Never raises.
    """
    if model is None:
        model = os.environ.get("SUMMARY_MODEL", DEFAULT_MODEL)
    argv = [
        "claude", "-p", prompt_text,
        "--model", model,
        "--output-format", "json",
        "--json-schema", json.dumps(SUMMARY_SCHEMA),
        "--no-session-persistence",
        "--disable-slash-commands",
        "--system-prompt", SYSTEM_PROMPT,
    ]
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError:
        return None, "claude_not_installed"
    except subprocess.TimeoutExpired:
        return None, "timeout"
    if proc.returncode != 0:
        first_err_line = (proc.stderr or "").strip().splitlines()
        msg = first_err_line[0] if first_err_line else f"exit {proc.returncode}"
        return None, f"cli_error: {msg}"
    try:
        outer = json.loads(proc.stdout)
        # `claude -p --output-format json` returns {"result": "<inner JSON string>"}
        inner_raw = outer.get("result")
        if not isinstance(inner_raw, str):
            return None, "parse_error"
        inner = json.loads(inner_raw)
        activities = inner.get("activities")
        if not isinstance(activities, list) or not activities:
            return None, "parse_error"
        return [str(a) for a in activities], None
    except (json.JSONDecodeError, AttributeError):
        return None, "parse_error"
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_summarizer.py -v`
Expected: 14 prior + 7 new = 21 passed.

- [ ] **Step 5: Commit**

```bash
git add summarizer.py tests/test_summarizer.py
git commit -m "feat(summarizer): add run_claude with structured output parsing"
```

---

## Task 6: summarizer.py — `summarize_cell` orchestrator

**Files:**
- Modify: `summarizer.py`
- Modify: `tests/test_summarizer.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_summarizer.py`:

```python
def _seed_jsonl_for_cell(projects_dir, cwd, date, prompts):
    proj_dir = projects_dir / cwd.replace("/", "-")
    proj_dir.mkdir(parents=True, exist_ok=True)
    records = [
        {"type": "user",
         "timestamp": f"{date}T10:0{i}:00Z",
         "message": {"content": p}}
        for i, p in enumerate(prompts)
    ]
    (proj_dir / "session.jsonl").write_text(
        "\n".join(json.dumps(r) for r in records),
    )


def test_summarize_cell_calls_claude_and_writes_cache(tmp_path):
    import scanner
    db = tmp_path / "u.db"
    scanner.init_db(db).close()
    proj = tmp_path / "projects"
    proj.mkdir()
    _seed_jsonl_for_cell(proj, "/Users/x/myproj", "2026-04-25",
                         ["refactor the api", "add tests for the new endpoint"])
    fake = json.dumps({"result": json.dumps({"activities": ["Refactored API"]})})
    with patch("subprocess.run", return_value=_mock_claude_response(fake)):
        result = summarizer.summarize_cell(
            date="2026-04-25", cwd="/Users/x/myproj", cost_usd=1.23,
            db_path=db, projects_dirs=[proj],
        )
    assert result["activities"] == ["Refactored API"]
    assert result["cached"] is False
    assert result["error"] is None
    # Verify written to DB
    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT activities, cost_usd FROM daily_summaries WHERE summary_date=?",
        ("2026-04-25",),
    ).fetchone()
    conn.close()
    assert json.loads(row[0]) == ["Refactored API"]
    assert row[1] == 1.23


def test_summarize_cell_returns_cache_hit(tmp_path):
    import scanner
    db = tmp_path / "u.db"
    scanner.init_db(db).close()
    proj = tmp_path / "projects"
    proj.mkdir()
    _seed_jsonl_for_cell(proj, "/Users/x/myproj", "2026-04-25",
                         ["refactor the api"])
    text = summarizer.collect_prompts("2026-04-25", "/Users/x/myproj", [proj])
    h = summarizer.prompt_hash(text)
    conn = sqlite3.connect(db)
    conn.execute("""
        INSERT INTO daily_summaries
          (summary_date, project_path, prompt_hash, activities, cost_usd, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, ("2026-04-25", "/Users/x/myproj", h,
          json.dumps(["Cached activity"]), 1.0, time.time()))
    conn.commit()
    conn.close()
    with patch("subprocess.run") as m:  # must not be called
        result = summarizer.summarize_cell(
            date="2026-04-25", cwd="/Users/x/myproj", cost_usd=1.0,
            db_path=db, projects_dirs=[proj],
        )
    assert result["cached"] is True
    assert result["activities"] == ["Cached activity"]
    m.assert_not_called()


def test_summarize_cell_invalidates_on_hash_mismatch(tmp_path):
    import scanner
    db = tmp_path / "u.db"
    scanner.init_db(db).close()
    proj = tmp_path / "projects"
    proj.mkdir()
    _seed_jsonl_for_cell(proj, "/Users/x/myproj", "2026-04-25",
                         ["original prompt"])
    # Cache with stale hash
    conn = sqlite3.connect(db)
    conn.execute("""
        INSERT INTO daily_summaries
          (summary_date, project_path, prompt_hash, activities, cost_usd, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, ("2026-04-25", "/Users/x/myproj", "stale-hash",
          json.dumps(["old"]), 1.0, time.time()))
    conn.commit()
    conn.close()
    fake = json.dumps({"result": json.dumps({"activities": ["fresh"]})})
    with patch("subprocess.run", return_value=_mock_claude_response(fake)):
        result = summarizer.summarize_cell(
            date="2026-04-25", cwd="/Users/x/myproj", cost_usd=1.0,
            db_path=db, projects_dirs=[proj],
        )
    assert result["cached"] is False
    assert result["activities"] == ["fresh"]


def test_summarize_cell_does_not_cache_errors(tmp_path):
    import scanner
    db = tmp_path / "u.db"
    scanner.init_db(db).close()
    proj = tmp_path / "projects"
    proj.mkdir()
    _seed_jsonl_for_cell(proj, "/Users/x/myproj", "2026-04-25",
                         ["a real prompt"])
    with patch("subprocess.run", side_effect=FileNotFoundError):
        result = summarizer.summarize_cell(
            date="2026-04-25", cwd="/Users/x/myproj", cost_usd=1.0,
            db_path=db, projects_dirs=[proj],
        )
    assert result["error"] == "claude_not_installed"
    assert result["activities"] is None
    # Verify nothing was written
    conn = sqlite3.connect(db)
    rows = conn.execute("SELECT * FROM daily_summaries").fetchall()
    conn.close()
    assert rows == []


def test_summarize_cell_skips_when_no_prompts(tmp_path):
    import scanner
    db = tmp_path / "u.db"
    scanner.init_db(db).close()
    proj = tmp_path / "projects"
    proj.mkdir()
    with patch("subprocess.run") as m:
        result = summarizer.summarize_cell(
            date="2026-04-25", cwd="/Users/x/empty", cost_usd=1.0,
            db_path=db, projects_dirs=[proj],
        )
    assert result["error"] == "no_prompts"
    assert result["activities"] is None
    m.assert_not_called()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_summarizer.py::test_summarize_cell_calls_claude_and_writes_cache -v`
Expected: FAIL with `AttributeError`.

- [ ] **Step 3: Implement `summarize_cell`**

Append to `summarizer.py`:

```python
def summarize_cell(date, cwd, cost_usd, db_path, projects_dirs, model=None):
    """
    Orchestrate one (date, cwd) summary: collect prompts, check cache,
    invoke claude if needed, persist result. Errors are returned, not raised.
    """
    text = collect_prompts(date, cwd, projects_dirs)
    if not text:
        return {"activities": None, "cached": False, "error": "no_prompts"}
    h = prompt_hash(text)
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT prompt_hash, activities FROM daily_summaries "
            "WHERE summary_date=? AND project_path=?",
            (date, cwd),
        ).fetchone()
        if row is not None and row[0] == h:
            return {
                "activities": json.loads(row[1]),
                "cached": True,
                "error": None,
            }
        activities, err = run_claude(text, model=model)
        if err is not None:
            return {"activities": None, "cached": False, "error": err}
        conn.execute("""
            INSERT OR REPLACE INTO daily_summaries
              (summary_date, project_path, prompt_hash,
               activities, cost_usd, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (date, cwd, h, json.dumps(activities), cost_usd, time.time()))
        conn.commit()
        return {"activities": activities, "cached": False, "error": None}
    finally:
        conn.close()
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_summarizer.py -v`
Expected: 21 prior + 5 new = 26 passed.

- [ ] **Step 5: Run full suite**

Run: `python3 -m pytest tests/ -q`
Expected: 105 prior + 26 new (which includes the 2 from Task 1) = 130 passed. (Adjust this expectation if you've split tests differently — the count must equal previous total + new tests added.)

- [ ] **Step 6: Commit**

```bash
git add summarizer.py tests/test_summarizer.py
git commit -m "feat(summarizer): add summarize_cell orchestrator with cache"
```

---

## Task 7: cli.py — eager pass after scan

**Files:**
- Modify: `cli.py` (`cmd_dashboard` function around line 390)
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_cli.py`:

```python
def test_cmd_dashboard_runs_eager_summarizer_pass(tmp_path, monkeypatch, capsys):
    """cmd_dashboard should call summarizer.run_eager_pass after the scan."""
    import cli, summarizer
    db = tmp_path / "u.db"
    proj = tmp_path / "projects"
    proj.mkdir()
    monkeypatch.setattr(cli, "DB_PATH", db)

    # Stub cmd_scan, serve, and webbrowser so we don't scan, start a server,
    # or open a browser tab on the developer's machine
    monkeypatch.setattr(cli, "cmd_scan", lambda **kw: None)
    monkeypatch.setattr(
        "dashboard.serve",
        lambda host=None, port=None: None,
        raising=False,
    )
    monkeypatch.setattr("webbrowser.open", lambda *a, **kw: None)

    called = {"count": 0, "args": None}
    def fake_eager(db_path, projects_dirs, progress_callback=None):
        called["count"] += 1
        called["args"] = (db_path, projects_dirs)
        if progress_callback:
            progress_callback(1, 1)
        return {"summarized": 1, "skipped": 0, "errors": 0}
    monkeypatch.setattr(summarizer, "run_eager_pass", fake_eager)

    cli.cmd_dashboard(projects_dir=str(proj))
    assert called["count"] == 1
    assert called["args"][0] == db


def test_cmd_dashboard_eager_pass_writes_progress_to_stderr(monkeypatch, capsys, tmp_path):
    import cli, summarizer
    db = tmp_path / "u.db"
    proj = tmp_path / "projects"
    proj.mkdir()
    monkeypatch.setattr(cli, "DB_PATH", db)
    monkeypatch.setattr(cli, "cmd_scan", lambda **kw: None)
    monkeypatch.setattr(
        "dashboard.serve",
        lambda host=None, port=None: None,
        raising=False,
    )
    monkeypatch.setattr("webbrowser.open", lambda *a, **kw: None)
    def fake_eager(db_path, projects_dirs, progress_callback=None):
        progress_callback(1, 3)
        progress_callback(2, 3)
        progress_callback(3, 3)
        return {"summarized": 3, "skipped": 0, "errors": 0}
    monkeypatch.setattr(summarizer, "run_eager_pass", fake_eager)

    cli.cmd_dashboard(projects_dir=str(proj))
    captured = capsys.readouterr()
    assert "Summarizing" in captured.err
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_cli.py::test_cmd_dashboard_runs_eager_summarizer_pass -v`
Expected: FAIL — `summarizer.run_eager_pass` doesn't exist yet.

- [ ] **Step 3: Implement `run_eager_pass` in summarizer.py**

Append to `summarizer.py`:

```python
def run_eager_pass(db_path, projects_dirs, progress_callback=None):
    """
    Summarize the eager set: top-20% (date, cwd) cells by cost, capped at
    SUMMARY_MAX_CELLS. Returns a dict with summary counts.
    """
    cells = rank_cells_by_cost(db_path)
    total = len(cells)
    counts = {"summarized": 0, "skipped": 0, "errors": 0}
    for i, (date, cwd, cost) in enumerate(cells, start=1):
        result = summarize_cell(
            date=date, cwd=cwd, cost_usd=cost,
            db_path=db_path, projects_dirs=projects_dirs,
        )
        if result["error"]:
            counts["errors"] += 1
        elif result["cached"]:
            counts["skipped"] += 1
        else:
            counts["summarized"] += 1
        if progress_callback is not None:
            progress_callback(i, total)
    return counts
```

- [ ] **Step 4: Wire it into `cmd_dashboard`**

In `cli.py`, replace the body of `cmd_dashboard` (around lines 390–410) with:

```python
def cmd_dashboard(projects_dir=None, host=None, port=None):
    import webbrowser
    import threading
    import time as _time
    import sys
    import scanner, summarizer

    print("Running scan first...")
    cmd_scan(projects_dir=projects_dir)

    print("\nGenerating activity summaries...")
    is_tty = sys.stderr.isatty()
    def progress(done, total):
        if total == 0:
            return
        if is_tty:
            pct = 100 * done // total
            sys.stderr.write(f"\rSummarizing… {done} / {total} cells ({pct}%)")
            sys.stderr.flush()
        else:
            if done == 1 or done == total or done % 5 == 0:
                sys.stderr.write(f"Summarizing… {done} / {total} cells\n")
    projects_dirs = (
        [projects_dir] if projects_dir else scanner.DEFAULT_PROJECTS_DIRS
    )
    counts = summarizer.run_eager_pass(
        db_path=DB_PATH,
        projects_dirs=projects_dirs,
        progress_callback=progress,
    )
    if is_tty:
        sys.stderr.write("\n")
    print(f"  {counts['summarized']} summarized, "
          f"{counts['skipped']} cached, {counts['errors']} errors")

    print("\nStarting dashboard server...")
    from dashboard import serve

    host = host or os.environ.get("HOST", "localhost")
    port = int(port or os.environ.get("PORT", "8080"))

    def open_browser():
        _time.sleep(1.0)
        webbrowser.open(f"http://{host}:{port}")

    t = threading.Thread(target=open_browser, daemon=True)
    t.start()
    serve(host=host, port=port)
```

- [ ] **Step 5: Run tests**

Run: `python3 -m pytest tests/test_cli.py -v`
Expected: prior tests + 2 new = all pass.

- [ ] **Step 6: Run full suite**

Run: `python3 -m pytest tests/ -q`
Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add cli.py summarizer.py tests/test_cli.py
git commit -m "feat(cli): run eager summarizer pass after scan in cmd_dashboard"
```

---

## Task 8: dashboard.py — `/api/daily-summaries` endpoint

**Files:**
- Modify: `dashboard.py` (`DashboardHandler.do_GET` around line 1410)
- Test: `tests/test_dashboard.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_dashboard.py`:

```python
def test_api_daily_summaries_returns_cached_cells(tmp_path, monkeypatch):
    import dashboard, scanner, summarizer
    db = tmp_path / "u.db"
    scanner.init_db(db).close()
    monkeypatch.setattr(dashboard, "DB_PATH", db)

    # Seed two turns and two cached summaries for 2026-04-25
    conn = sqlite3.connect(db)
    conn.execute("""
        INSERT INTO turns (session_id, timestamp, model, input_tokens, cwd)
        VALUES ('s1', '2026-04-25T10:00:00Z', 'claude-haiku-4-5', 1000000, '/p/A')
    """)
    for cwd, acts, cost in [
        ("/p/A", ["Did A1", "Did A2"], 1.5),
        ("/p/B", ["Did B"], 0.5),
    ]:
        conn.execute("""
            INSERT INTO daily_summaries
              (summary_date, project_path, prompt_hash, activities, cost_usd, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, ("2026-04-25", cwd, "h", json.dumps(acts), cost, 0.0))
    conn.commit()
    conn.close()

    # Mock summarize_cell so the lazy path doesn't actually call claude
    monkeypatch.setattr(
        summarizer, "summarize_cell",
        lambda **kw: {"activities": None, "cached": False, "error": "stub"},
    )

    response = dashboard.get_daily_summaries("2026-04-25", db_path=db,
                                              projects_dirs=[tmp_path])
    assert response["date"] == "2026-04-25"
    cells_by_proj = {c["project"]: c for c in response["cells"]}
    assert cells_by_proj["/p/A"]["activities"] == ["Did A1", "Did A2"]
    assert cells_by_proj["/p/A"]["error"] is None
    assert cells_by_proj["/p/B"]["activities"] == ["Did B"]


def test_api_daily_summaries_triggers_lazy_summarization(tmp_path, monkeypatch):
    import dashboard, scanner, summarizer
    db = tmp_path / "u.db"
    scanner.init_db(db).close()
    # One turn but no cached summary → triggers lazy path
    conn = sqlite3.connect(db)
    conn.execute("""
        INSERT INTO turns (session_id, timestamp, model, input_tokens, cwd)
        VALUES ('s1', '2026-04-25T10:00:00Z', 'claude-haiku-4-5', 1000000, '/p/A')
    """)
    conn.commit()
    conn.close()

    called = {"count": 0}
    def fake_summarize(date, cwd, cost_usd, db_path, projects_dirs, model=None):
        called["count"] += 1
        return {"activities": ["lazy result"], "cached": False, "error": None}
    monkeypatch.setattr(summarizer, "summarize_cell", fake_summarize)

    response = dashboard.get_daily_summaries("2026-04-25", db_path=db,
                                              projects_dirs=[tmp_path])
    assert called["count"] == 1
    cells_by_proj = {c["project"]: c for c in response["cells"]}
    assert cells_by_proj["/p/A"]["activities"] == ["lazy result"]


def test_api_daily_summaries_endpoint_serves_json(tmp_path, monkeypatch):
    """Smoke test the actual HTTP route returns JSON."""
    import dashboard, scanner, summarizer
    from http.server import HTTPServer
    import threading, urllib.request

    db = tmp_path / "u.db"
    scanner.init_db(db).close()
    monkeypatch.setattr(dashboard, "DB_PATH", db)
    monkeypatch.setattr(
        summarizer, "summarize_cell",
        lambda **kw: {"activities": None, "cached": False, "error": "stub"},
    )

    server = HTTPServer(("127.0.0.1", 0), dashboard.DashboardHandler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/daily-summaries?date=2026-04-25",
        ) as r:
            body = json.loads(r.read())
        assert body["date"] == "2026-04-25"
        assert body["cells"] == []
    finally:
        server.shutdown()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_dashboard.py::test_api_daily_summaries_returns_cached_cells -v`
Expected: FAIL — `dashboard.get_daily_summaries` doesn't exist yet.

- [ ] **Step 3: Implement `get_daily_summaries`**

In `dashboard.py`, near the existing `get_dashboard_data` function (around line 24), add:

```python
def get_daily_summaries(date, db_path=None, projects_dirs=None):
    """
    Return cached + lazily-summarized cells for a single date. Triggers
    summarize_cell synchronously for any (date, cwd) with activity but no
    cached summary. Relies on ThreadingHTTPServer so other requests aren't
    blocked while a lazy summary runs.
    """
    import summarizer, scanner
    if db_path is None:
        db_path = DB_PATH
    if projects_dirs is None:
        projects_dirs = scanner.DEFAULT_PROJECTS_DIRS
    if not _date_is_valid(date):
        return {"date": date, "cells": [], "error": "invalid_date"}

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        # All cells with activity that day
        rows = conn.execute("""
            SELECT cwd, model,
                   SUM(input_tokens) AS inp,
                   SUM(output_tokens) AS out,
                   SUM(cache_read_tokens) AS cr,
                   SUM(cache_creation_tokens) AS cw
            FROM turns
            WHERE substr(timestamp, 1, 10) = ?
              AND cwd IS NOT NULL AND cwd != ''
            GROUP BY cwd, model
        """, (date,)).fetchall()
        cell_costs = {}
        from cli import calc_cost
        for r in rows:
            cost = calc_cost(r["model"], r["inp"] or 0, r["out"] or 0,
                             r["cr"] or 0, r["cw"] or 0)
            cell_costs[r["cwd"]] = cell_costs.get(r["cwd"], 0.0) + cost

        cached_rows = conn.execute("""
            SELECT project_path, activities
            FROM daily_summaries
            WHERE summary_date = ?
        """, (date,)).fetchall()
        cached = {r["project_path"]: json.loads(r["activities"])
                  for r in cached_rows}

        eager_set = {(d, c) for d, c, _ in summarizer.rank_cells_by_cost(db_path)}
    finally:
        conn.close()

    cells = []
    for cwd in sorted(cell_costs.keys()):
        cost = cell_costs[cwd]
        is_eager = (date, cwd) in eager_set
        if cwd in cached:
            cells.append({
                "project": cwd, "cost": round(cost, 4),
                "activities": cached[cwd], "error": None, "eager": is_eager,
            })
        else:
            result = summarizer.summarize_cell(
                date=date, cwd=cwd, cost_usd=cost,
                db_path=db_path, projects_dirs=projects_dirs,
            )
            cells.append({
                "project": cwd, "cost": round(cost, 4),
                "activities": result["activities"],
                "error": result["error"], "eager": is_eager,
            })
    return {"date": date, "cells": cells}


def _date_is_valid(date):
    if not isinstance(date, str) or len(date) != 10:
        return False
    try:
        datetime.strptime(date, "%Y-%m-%d")
        return True
    except ValueError:
        return False
```

Make sure `from datetime import datetime` is already imported at the top of `dashboard.py` (it is — but verify).

- [ ] **Step 4: Add the route to `do_GET`**

In `dashboard.py`, inside `DashboardHandler.do_GET` (around line 1410), add a new `elif` branch after the `/api/data` branch:

```python
        elif path == "/api/daily-summaries":
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            date = qs.get("date", [""])[0]
            data = get_daily_summaries(date)
            body = json.dumps(data).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
```

- [ ] **Step 5: Run tests**

Run: `python3 -m pytest tests/test_dashboard.py -v`
Expected: prior tests + 3 new = all pass.

- [ ] **Step 6: Run full suite**

Run: `python3 -m pytest tests/ -q`
Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add dashboard.py tests/test_dashboard.py
git commit -m "feat(dashboard): add /api/daily-summaries endpoint with lazy fetch"
```

---

## Task 9: dashboard.py — UI section + JS

**Files:**
- Modify: `dashboard.py` (HTML_TEMPLATE)

This task has no Python test (no JS test harness in this repo, matches existing convention). It ends with manual testing.

- [ ] **Step 1: Add CSS for the new section**

In `dashboard.py`, find the `<style>` block in `HTML_TEMPLATE` and append these rules just before the closing `</style>`:

```css
#daily-activities { margin-top: 32px; }
#daily-activities h2 { margin-bottom: 12px; }
#daily-activities .day-row { border: 1px solid #e0e0e0; border-radius: 4px; margin-bottom: 8px; padding: 0; background: #fff; }
#daily-activities .day-row summary { padding: 10px 14px; cursor: pointer; font-weight: 500; display: flex; gap: 12px; align-items: center; }
#daily-activities .day-row summary::-webkit-details-marker { display: none; }
#daily-activities .day-row summary::before { content: "▶"; font-size: 0.7em; color: #888; transition: transform 0.15s; }
#daily-activities .day-row[open] summary::before { transform: rotate(90deg); }
#daily-activities .day-meta { color: #888; font-weight: normal; font-size: 0.9em; }
#daily-activities .day-cost { margin-left: auto; font-variant-numeric: tabular-nums; }
#daily-activities .project-block { padding: 8px 14px 8px 32px; border-top: 1px solid #f0f0f0; }
#daily-activities .project-name { font-weight: 500; display: flex; align-items: center; gap: 6px; }
#daily-activities .project-cost { color: #888; font-variant-numeric: tabular-nums; margin-left: auto; }
#daily-activities .star { color: #f5a623; }
#daily-activities ul.activities { margin: 6px 0 0 0; padding-left: 20px; }
#daily-activities ul.activities li { margin: 2px 0; }
#daily-activities .spinner { color: #888; font-style: italic; padding: 4px 0; }
#daily-activities .err { color: #c0392b; padding: 4px 0; }
#daily-activities .err button { margin-left: 8px; font-size: 0.85em; }
#daily-activities .banner { padding: 10px 14px; background: #fff3cd; border: 1px solid #ffe599; border-radius: 4px; margin-bottom: 12px; }
```

- [ ] **Step 2: Add the HTML section**

In `dashboard.py`, find the line containing `<div class="section-header"><div class="section-title">Recent Sessions</div>` (around line 332). Insert this block immediately *before* the surrounding wrapper of that section header (the parent `<section>` or `<div class="card">` — verify by reading 5 lines above line 332):

```html
<section id="daily-activities">
  <h2>Daily Activities</h2>
  <div id="daily-banner" class="banner" style="display:none"></div>
  <div id="daily-list">
    <p class="spinner">Loading…</p>
  </div>
</section>
```

- [ ] **Step 3: Add the JS to render the section**

In `dashboard.py`, inside the existing `<script>` block in `HTML_TEMPLATE`, append at the end (before the closing `</script>`):

```javascript
const dailyState = { fetchedDates: new Set(), inFlight: new Map() };

function renderDailyList(data) {
  const list = document.getElementById('daily-list');
  if (!data.days.length) {
    list.innerHTML = '<p class="spinner">No activity in the selected range.</p>';
    return;
  }
  list.innerHTML = data.days.map(day => `
    <details class="day-row" data-date="${day.date}">
      <summary>
        <span>${day.date}</span>
        <span class="day-meta">${day.project_count} project${day.project_count === 1 ? '' : 's'}</span>
        <span class="day-cost">$${day.cost.toFixed(2)}</span>
      </summary>
      <div class="day-body">
        <p class="spinner">Click to load activities…</p>
      </div>
    </details>
  `).join('');
  list.querySelectorAll('details.day-row').forEach(d => {
    d.addEventListener('toggle', () => {
      if (d.open) loadDayActivities(d);
    });
  });
}

async function loadDayActivities(detailsEl) {
  const date = detailsEl.dataset.date;
  if (dailyState.fetchedDates.has(date)) return;
  if (dailyState.inFlight.has(date)) return;
  dailyState.inFlight.set(date, true);
  const body = detailsEl.querySelector('.day-body');
  body.innerHTML = '<p class="spinner">Summarizing…</p>';
  try {
    const resp = await fetch(`/api/daily-summaries?date=${encodeURIComponent(date)}`);
    const data = await resp.json();
    // Stamp the date onto each cell so renderProjectBlock can wire Retry buttons.
    data.cells.forEach(c => { c.__date = date; });
    body.innerHTML = data.cells.map(c => renderProjectBlock(c)).join('');
    dailyState.fetchedDates.add(date);
  } catch (e) {
    body.innerHTML = `<p class="err">Failed to load: ${e.message}</p>`;
  } finally {
    dailyState.inFlight.delete(date);
  }
}

function renderProjectBlock(cell) {
  const star = cell.eager ? '<span class="star" title="Pre-summarized">★</span>' : '';
  if (cell.error === 'claude_not_installed') {
    return `<div class="project-block">
      <div class="project-name">${escapeHtml(cell.project)} ${star}<span class="project-cost">$${cell.cost.toFixed(2)}</span></div>
      <p class="err">Daily Activities requires the <code>claude</code> CLI on PATH.</p>
    </div>`;
  }
  if (cell.error) {
    const date = cell.__date || '';
    return `<div class="project-block">
      <div class="project-name">${escapeHtml(cell.project)} ${star}<span class="project-cost">$${cell.cost.toFixed(2)}</span></div>
      <p class="err">Summary unavailable: ${escapeHtml(cell.error)}
        <button onclick="retryDay('${escapeHtml(date)}')">Retry</button></p>
    </div>`;
  }
  if (!cell.activities || !cell.activities.length) {
    return `<div class="project-block">
      <div class="project-name">${escapeHtml(cell.project)} ${star}<span class="project-cost">$${cell.cost.toFixed(2)}</span></div>
      <p class="spinner">No activities inferred.</p>
    </div>`;
  }
  const bullets = cell.activities.map(a => `<li>${escapeHtml(a)}</li>`).join('');
  return `<div class="project-block">
    <div class="project-name">${escapeHtml(cell.project)} ${star}<span class="project-cost">$${cell.cost.toFixed(2)}</span></div>
    <ul class="activities">${bullets}</ul>
  </div>`;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

function retryDay(date) {
  dailyState.fetchedDates.delete(date);
  const detailsEl = document.querySelector(
    `#daily-list details.day-row[data-date="${date}"]`,
  );
  if (detailsEl && detailsEl.open) loadDayActivities(detailsEl);
}

function buildDailyDataFromCharts(rangeData) {
  // Group session rows by day, using the same range filter as the rest.
  // Session objects don't carry a cost field — compute it via calcCost(),
  // which is the same helper applyFilter() already uses.
  const dayMap = new Map();
  for (const s of rangeData.sessions || []) {
    const d = s.last_date;
    if (!d) continue;
    if (!dayMap.has(d)) dayMap.set(d, { date: d, projects: new Set(), cost: 0 });
    const day = dayMap.get(d);
    day.projects.add(s.project);
    day.cost += calcCost(s.model, s.input, s.output, s.cache_read, s.cache_creation);
  }
  const days = Array.from(dayMap.values())
    .map(d => ({ date: d.date, project_count: d.projects.size, cost: d.cost }))
    .sort((a, b) => b.date.localeCompare(a.date));
  return { days };
}

// Hook into the existing render pipeline. Find the render() / loadData()
// function in this file and call renderDailyList(buildDailyDataFromCharts(filteredData))
// right after the existing chart updates.
```

**Important integration step:** the render function is `applyFilter()` (around line 787–862 in `dashboard.py`). Its tail block at line ~858–861 looks like:

```javascript
renderSessionsTable(lastFilteredSessions.slice(0, 20));
renderModelCostTable(byModel);
renderProjectCostTable(lastByProject.slice(0, 20));
renderProjectBranchCostTable(lastByProjectBranch.slice(0, 20));
```

Append immediately after that last call (still inside `applyFilter()`):

```javascript
renderDailyList(buildDailyDataFromCharts({ sessions: lastFilteredSessions }));
```

`lastFilteredSessions` is the post-range-filter session list this codebase already uses (declared near line 412). `buildDailyDataFromCharts` reads `rangeData.sessions` — wrap it in an object as shown.

- [ ] **Step 4: Manual smoke test**

Run: `python3 cli.py dashboard`

Verify in this order:
1. Terminal shows `Scanning…` then `Summarizing… N / M cells` progress bar.
2. After progress ends, terminal shows `N summarized, M cached, 0 errors`.
3. Browser opens to `http://localhost:8080`.
4. Scroll past the existing charts. The "Daily Activities" section is visible.
5. Day rows are listed with the date, project count, and total cost. Most recent day on top.
6. Click a day row — it expands; spinner appears briefly; bullets render.
7. Eager-set cells are marked with ★. Lazy ones aren't.
8. Click a different day → loads independently. Re-clicking an already-expanded day shows cached data instantly (no fetch).
9. Switch range filter to "7d" — only the last 7 days appear in the section.

If any of those fail, fix the JS and re-load the page (no commit yet).

- [ ] **Step 5: Manual `claude` not installed test**

Temporarily rename `claude` to confirm graceful degradation:

```bash
which claude   # note the path, e.g. /opt/homebrew/bin/claude
sudo mv /opt/homebrew/bin/claude /opt/homebrew/bin/claude.bak
python3 cli.py dashboard
```

Verify:
1. Terminal eager pass prints `0 summarized, 0 cached, N errors` (or skips, depending on existing cached state).
2. Daily Activities section still loads, but each project block shows `Daily Activities requires the claude CLI on PATH`.
3. Other parts of the dashboard (charts, sessions table) work normally.

Then restore: `sudo mv /opt/homebrew/bin/claude.bak /opt/homebrew/bin/claude`

- [ ] **Step 6: Commit**

```bash
git add dashboard.py
git commit -m "feat(dashboard): add Daily Activities section with lazy expand"
```

---

## Task 10: CHANGELOG + tag release

**Files:**
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Run the full test suite**

Run: `python3 -m pytest tests/ -v`
Expected: all tests pass.

- [ ] **Step 2: Add CHANGELOG entry**

In `CHANGELOG.md`, add a new section at the top:

```markdown
## 2026-04-27

- Add Daily Activities view: per-day, per-project bulleted activity summaries inferred by Haiku via the local `claude` CLI
- Eager pass at dashboard startup summarizes top-20% (day, project) cells by cost (capped at 50)
- Lazy pass on `/api/daily-summaries?date=…` summarizes other days on demand when expanded
- Cache invalidated by sha256 hash of the day's user prompts
- New env vars: `SUMMARY_MODEL` (default: `haiku`), `SUMMARY_MAX_CELLS` (default: `50`)
- New `daily_summaries` table (auto-created via `CREATE TABLE IF NOT EXISTS`)
```

- [ ] **Step 3: Commit and tag**

```bash
git add CHANGELOG.md
git commit -m "docs: update CHANGELOG for v0.3.0-launchmetrics.1"
git tag -a v0.3.0-launchmetrics.1 -m "Daily Activities: AI-inferred daily activity summaries"
```

- [ ] **Step 4: Push branch and tag (with user confirmation)**

Push the branch and tag to origin (after user confirms — do not push without explicit go-ahead):

```bash
git push -u origin feature/daily-activity-summaries
git push origin v0.3.0-launchmetrics.1
```

Then open a PR against `main` (after the v0.2.0 PR has merged, or against the v0.2.0 branch if it hasn't).

---

## Notes for the engineer

- **TDD discipline:** every code-changing task starts with a failing test. Run the test, see it fail, then implement, then see it pass. Do not skip the "see it fail" step.
- **One commit per task:** each task ends with a commit. If you discover a problem mid-task, fix it before committing.
- **Backwards compatibility:** existing DBs (without `daily_summaries`) get the new table on next scan via `CREATE TABLE IF NOT EXISTS`. No migration script needed.
- **No new dependencies:** all changes within Python stdlib + the existing `claude` CLI on PATH. Do not add `requests`, `httpx`, or any other package.
- **Existing test patterns to reuse:**
  - `monkeypatch.setattr(scanner, "DB_PATH", db_path)` for DB redirection
  - `tests/test_dashboard.py` already has the `ThreadingHTTPServer`-in-a-thread pattern (line ~132) — copy it for the endpoint test in Task 8 if needed
  - Use `pytest.approx` for float comparisons
- **Subprocess mocking:** Task 5 patches `subprocess.run` directly. Make sure no test calls `claude` for real (it would burn quota and slow CI).
- **`cli.py` import in `summarizer.py`:** the import `from cli import calc_cost` happens *inside* `rank_cells_by_cost` (function-scoped) to keep `summarizer.py` importable even if `cli.py` ever changes shape.
- **Sequential summarization:** the eager pass runs cells one at a time (no `concurrent.futures`). This is intentional — keeps the terminal output clean and avoids hammering the user's Claude quota with parallel calls.
- **`claude -p` output shape:** confirmed via `--output-format json` — returns `{"result": "<inner JSON string>"}` where the inner string is the LLM's structured output. Both the outer wrapper and the inner JSON need to be parsed (see Task 5 implementation).
