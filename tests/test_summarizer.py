import json
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
