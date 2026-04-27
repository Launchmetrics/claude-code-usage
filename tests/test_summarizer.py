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


def test_is_noise_filters_command_and_system_artefacts():
    # slash-command wrappers
    assert summarizer._is_noise(
        "<command-name>/plugin</command-name>\n<command-message>plugin</command-message>"
    )
    # bash input/output stored as user text
    assert summarizer._is_noise("<bash-input>ls -la</bash-input>")
    assert summarizer._is_noise("<bash-stdout>foo\nbar</bash-stdout>")
    # local-command artefacts
    assert summarizer._is_noise(
        "<local-command-caveat>Caveat: The messages below were generated by the user...</local-command-caveat>"
    )
    assert summarizer._is_noise("<local-command-stdout>(no content)</local-command-stdout>")
    # task notifications and system reminders that landed in the user stream
    assert summarizer._is_noise("<task-notification>\n<task-id>abc</task-id>\n</task-notification>")
    assert summarizer._is_noise("<system-reminder>\nSomething\n</system-reminder>")
    # tool-use chrome
    assert summarizer._is_noise("[Request interrupted by user]")
    # auto-context-continuation prelude
    assert summarizer._is_noise(
        "This session is being continued from a previous conversation that ran out of context. The summary below..."
    )
    # real prose still passes
    assert not summarizer._is_noise("refactor the epic correlation script")


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
    # The user prompt is wrapped in a <prompts> block so the model treats
    # it as data, not as an instruction directed at it.
    p_index = argv.index("-p")
    assert "<prompts>" in argv[p_index + 1] and "hello" in argv[p_index + 1]
    assert "--model" in argv and "haiku" in argv
    assert "--no-session-persistence" in argv
    assert "--disable-slash-commands" in argv
    assert "--output-format" in argv and "json" in argv
    assert "--system-prompt" in argv
    # We no longer pass --json-schema; that flag returned an empty result
    # field on the current Claude Code CLI. JSON shape is enforced via the
    # system prompt and parsed from the result string instead.
    assert "--json-schema" not in argv


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
