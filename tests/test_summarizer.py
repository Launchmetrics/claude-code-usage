import json

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
