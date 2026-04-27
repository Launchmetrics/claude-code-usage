import json
import time
import pytest
import sqlite3

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


def _write_jsonl(path, records):
    path.write_text("\n".join(json.dumps(r) for r in records))


def test_collect_prompts_filters_noise_and_dedupes(tmp_path):
    proj_dir = tmp_path / "-Users-test-myproj"
    proj_dir.mkdir()
    _write_jsonl(proj_dir / "session.jsonl", [
        {"type": "user", "timestamp": "2026-04-25T10:00:00Z",
         "message": {"content": "refactor the epic correlation script"}},
        {"type": "user", "timestamp": "2026-04-25T10:05:00Z",
         "message": {"content": "yes"}},
        {"type": "user", "timestamp": "2026-04-25T10:10:00Z",
         "message": {"content": "hi"}},
        {"type": "user", "timestamp": "2026-04-25T10:15:00Z",
         "message": {"content": "refactor the epic correlation script"}},
        {"type": "user", "timestamp": "2026-04-25T10:20:00Z",
         "message": {"content": "add unit tests for the new endpoint"}},
        {"type": "assistant", "timestamp": "2026-04-25T10:30:00Z",
         "message": {"content": "should not be included"}},
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


def test_encoded_dirname_replaces_dots_and_spaces():
    # Claude Code encodes /, ., and whitespace all as "-"
    assert summarizer._encoded_dirname(
        "/Users/pau.montero/Projectes/claude-costs-dashboard"
    ) == "-Users-pau-montero-Projectes-claude-costs-dashboard"
    assert summarizer._encoded_dirname(
        "/Users/pau.montero/Projectes/launchmetrics/AIpril retrospectives"
    ) == "-Users-pau-montero-Projectes-launchmetrics-AIpril-retrospectives"
    # /. produces "--" (consecutive dashes preserved)
    assert summarizer._encoded_dirname(
        "/Users/pau.montero/.claude"
    ) == "-Users-pau-montero--claude"


def test_collect_prompts_finds_dir_when_cwd_has_dots(tmp_path):
    # Regression: cwd with "." in segment names must match Claude Code's
    # encoded dir which replaces "." with "-".
    proj_dir = tmp_path / "-Users-pau-montero-Projectes-claude-costs-dashboard"
    proj_dir.mkdir()
    _write_jsonl(proj_dir / "session.jsonl", [
        {"type": "user", "timestamp": "2026-04-27T10:00:00Z",
         "message": {"content": "wire up the calendar picker"}},
    ])
    text = summarizer.collect_prompts(
        date="2026-04-27",
        cwd="/Users/pau.montero/Projectes/claude-costs-dashboard",
        projects_dirs=[tmp_path],
    )
    assert text == "wire up the calendar picker"


def _seed_turns(db_path, rows):
    """rows: list of (timestamp, cwd, model, input, output, cache_read, cache_write)"""
    import scanner
    conn = scanner.get_db(db_path)
    scanner.init_db(conn)
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
    for i in range(10):
        rows.append(
            (f"2026-04-{i+1:02d}T10:00:00Z", f"/proj/{i}",
             "claude-haiku-4-5", (i + 1) * 1_000_000, 0, 0, 0)
        )
    _seed_turns(db, rows)
    cells = summarizer.rank_cells_by_cost(db, max_cells=100, percentile=80)
    assert len(cells) == 2
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
    conn = scanner.get_db(db)
    scanner.init_db(conn)
    conn.close()
    assert summarizer.rank_cells_by_cost(db, max_cells=10) == []


def test_rank_cells_percentile_zero_returns_all_positive(tmp_path):
    db = tmp_path / "u.db"
    _seed_turns(db, [
        ("2026-04-25T10:00:00Z", "/proj/A", "claude-haiku-4-5", 1_000_000, 0, 0, 0),
        ("2026-04-25T11:00:00Z", "/proj/B", "claude-haiku-4-5",   500_000, 0, 0, 0),
        ("2026-04-25T12:00:00Z", "/proj/C", "claude-haiku-4-5",   100_000, 0, 0, 0),
    ])
    cells = summarizer.rank_cells_by_cost(db, max_cells=10, percentile=0)
    assert len(cells) == 3


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


def _seed_jsonl_for_cell(projects_dir, cwd, date, prompts):
    proj_dir = projects_dir / cwd.replace("/", "-")
    proj_dir.mkdir(parents=True, exist_ok=True)
    records = [
        {"type": "user",
         "timestamp": f"{date}T10:{i:02d}:00Z",
         "message": {"content": p}}
        for i, p in enumerate(prompts)
    ]
    (proj_dir / "session.jsonl").write_text(
        "\n".join(json.dumps(r) for r in records),
    )


def test_summarize_cell_calls_claude_and_writes_cache(tmp_path):
    import scanner
    db = tmp_path / "u.db"
    conn = scanner.get_db(db)
    scanner.init_db(conn)
    conn.close()
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
    conn = scanner.get_db(db)
    scanner.init_db(conn)
    conn.close()
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
    with patch("subprocess.run") as m:
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
    conn = scanner.get_db(db)
    scanner.init_db(conn)
    conn.close()
    proj = tmp_path / "projects"
    proj.mkdir()
    _seed_jsonl_for_cell(proj, "/Users/x/myproj", "2026-04-25",
                         ["original prompt"])
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
    conn = scanner.get_db(db)
    scanner.init_db(conn)
    conn.close()
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
    conn = sqlite3.connect(db)
    rows = conn.execute("SELECT * FROM daily_summaries").fetchall()
    conn.close()
    assert rows == []


def test_summarize_cell_skips_when_no_prompts(tmp_path):
    import scanner
    db = tmp_path / "u.db"
    conn = scanner.get_db(db)
    scanner.init_db(conn)
    conn.close()
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
