"""Tests for cli.py - pricing, formatting, and cost calculation."""

import unittest
from cli import get_pricing, calc_cost, fmt, fmt_cost, PRICING


class TestGetPricing(unittest.TestCase):
    def test_exact_model_match(self):
        p = get_pricing("claude-opus-4-6")
        self.assertEqual(p["input"], 5.00)
        self.assertEqual(p["output"], 25.00)

    def test_all_known_models_have_pricing(self):
        for model in ("claude-opus-4-7", "claude-opus-4-6", "claude-opus-4-5",
                       "claude-sonnet-4-7", "claude-sonnet-4-6", "claude-sonnet-4-5",
                       "claude-haiku-4-7", "claude-haiku-4-6", "claude-haiku-4-5"):
            p = get_pricing(model)
            self.assertGreater(p["input"], 0, f"Missing input price for {model}")
            self.assertGreater(p["output"], 0, f"Missing output price for {model}")

    def test_opus_4_7_has_explicit_entry(self):
        """Regression guard for issue #61 — Opus 4.7 must be present."""
        p = get_pricing("claude-opus-4-7")
        self.assertEqual(p["input"], 5.00)
        self.assertEqual(p["output"], 25.00)

    def test_opus_4_7_with_date_suffix(self):
        """Model strings from JSONL often have date suffixes."""
        p = get_pricing("claude-opus-4-7-20260215")
        self.assertEqual(p["input"], 5.00)
        self.assertEqual(p["output"], 25.00)

    def test_prefix_match(self):
        # A model name with a suffix should still match the base
        p = get_pricing("claude-sonnet-4-6-20260401")
        self.assertEqual(p["input"], 3.00)
        self.assertEqual(p["output"], 15.00)

    def test_substring_match_opus(self):
        p = get_pricing("new-opus-5-model")
        self.assertEqual(p["input"], 5.00)
        self.assertEqual(p["output"], 25.00)

    def test_substring_match_sonnet(self):
        p = get_pricing("custom-sonnet-variant")
        self.assertEqual(p["input"], 3.00)
        self.assertEqual(p["output"], 15.00)

    def test_substring_match_haiku(self):
        p = get_pricing("experimental-haiku-fast")
        self.assertEqual(p["input"], 1.00)
        self.assertEqual(p["output"], 5.00)

    def test_substring_match_case_insensitive(self):
        p = get_pricing("Claude-Opus-Next")
        self.assertEqual(p["input"], 5.00)

    def test_prefix_takes_precedence_over_substring(self):
        # Exact prefix match should win over substring fallback
        p = get_pricing("claude-opus-4-6-preview")
        self.assertEqual(p["input"], 5.00)
        self.assertEqual(p["output"], 25.00)

    def test_unknown_model_returns_none(self):
        self.assertIsNone(get_pricing("glm-5.1"))
        self.assertIsNone(get_pricing("gpt-4o"))
        self.assertIsNone(get_pricing("some-unknown-model"))

    def test_none_model_returns_none(self):
        self.assertIsNone(get_pricing(None))

    def test_empty_string_returns_none(self):
        self.assertIsNone(get_pricing(""))


class TestCalcCost(unittest.TestCase):
    def test_basic_cost_calculation(self):
        # 1M input tokens of Sonnet at $3/MTok = $3.00
        cost = calc_cost("claude-sonnet-4-6", 1_000_000, 0, 0, 0)
        self.assertAlmostEqual(cost, 3.00)

    def test_output_tokens(self):
        # 1M output tokens of Sonnet at $15/MTok = $15.00
        cost = calc_cost("claude-sonnet-4-6", 0, 1_000_000, 0, 0)
        self.assertAlmostEqual(cost, 15.00)

    def test_cache_read_discount(self):
        # Cache read = 10% of input price
        # 1M cache_read of Opus at $5 * 0.10 = $0.50
        cost = calc_cost("claude-opus-4-6", 0, 0, 1_000_000, 0)
        self.assertAlmostEqual(cost, 0.50)

    def test_cache_creation_premium(self):
        # Cache creation = 125% of input price
        # 1M cache_creation of Opus at $5 * 1.25 = $6.25
        cost = calc_cost("claude-opus-4-6", 0, 0, 0, 1_000_000)
        self.assertAlmostEqual(cost, 6.25)

    def test_combined_cost(self):
        cost = calc_cost("claude-haiku-4-5",
                         inp=500_000, out=100_000,
                         cache_read=200_000, cache_creation=50_000)
        expected = (
            500_000 * 1.00 / 1_000_000 +   # input
            100_000 * 5.00 / 1_000_000 +    # output
            200_000 * 1.00 * 0.10 / 1_000_000 +  # cache read
            50_000 * 1.00 * 1.25 / 1_000_000     # cache creation
        )
        self.assertAlmostEqual(cost, expected)

    def test_zero_tokens(self):
        cost = calc_cost("claude-opus-4-6", 0, 0, 0, 0)
        self.assertEqual(cost, 0.0)

    def test_unknown_model_costs_zero(self):
        cost = calc_cost("glm-5.1", 1_000_000, 500_000, 100_000, 50_000)
        self.assertEqual(cost, 0.0)

    def test_non_anthropic_model_costs_zero(self):
        cost = calc_cost("gpt-4o", 1_000_000, 500_000, 0, 0)
        self.assertEqual(cost, 0.0)


class TestFmt(unittest.TestCase):
    def test_millions(self):
        self.assertEqual(fmt(1_500_000), "1.50M")
        self.assertEqual(fmt(1_000_000), "1.00M")

    def test_thousands(self):
        self.assertEqual(fmt(1_500), "1.5K")
        self.assertEqual(fmt(1_000), "1.0K")

    def test_small_numbers(self):
        self.assertEqual(fmt(999), "999")
        self.assertEqual(fmt(0), "0")


class TestFmtCost(unittest.TestCase):
    def test_formatting(self):
        self.assertEqual(fmt_cost(3.0), "$3.0000")
        self.assertEqual(fmt_cost(0.0001), "$0.0001")
        self.assertEqual(fmt_cost(0), "$0.0000")


class TestPricingConsistency(unittest.TestCase):
    """Ensure CLI pricing matches known Anthropic API rates."""

    def test_opus_pricing(self):
        for model in ("claude-opus-4-7", "claude-opus-4-6", "claude-opus-4-5"):
            p = get_pricing(model)
            self.assertEqual(p["input"], 5.00, f"{model} input price wrong")
            self.assertEqual(p["output"], 25.00, f"{model} output price wrong")

    def test_sonnet_pricing(self):
        for model in ("claude-sonnet-4-7", "claude-sonnet-4-6", "claude-sonnet-4-5"):
            p = get_pricing(model)
            self.assertEqual(p["input"], 3.00, f"{model} input price wrong")
            self.assertEqual(p["output"], 15.00, f"{model} output price wrong")

    def test_haiku_pricing(self):
        for model in ("claude-haiku-4-7", "claude-haiku-4-6", "claude-haiku-4-5"):
            p = get_pricing(model)
            self.assertEqual(p["input"], 1.00, f"{model} input price wrong")
            self.assertEqual(p["output"], 5.00, f"{model} output price wrong")


if __name__ == "__main__":
    unittest.main()


# ── pytest-style tests for cmd_scan progress callback ─────────────────────────

def test_cmd_scan_progress_callback_writes_in_place_to_stderr(tmp_path, capsys, monkeypatch):
    """When stderr is a TTY, progress should use carriage returns."""
    import cli
    import scanner
    import io
    import sys

    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()
    for i in range(2):
        sub = projects_dir / f"p{i}"
        sub.mkdir()
        (sub / f"s{i}.jsonl").write_text("")

    db_path = tmp_path / "test.db"

    class FakeTTY(io.StringIO):
        def isatty(self): return True
    fake_err = FakeTTY()
    monkeypatch.setattr("sys.stderr", fake_err)

    cli.cmd_scan(projects_dir=projects_dir, db_path=db_path)

    output = fake_err.getvalue()
    assert "\r" in output, f"expected carriage return in stderr output: {output!r}"
    assert "Scanning" in output, f"expected 'Scanning' in stderr output: {output!r}"


def test_cmd_scan_progress_non_tty_uses_newlines(tmp_path, monkeypatch):
    """When stderr is not a TTY, progress should use newlines (no carriage returns)."""
    import cli
    import io

    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()
    for i in range(60):  # > 50 files so periodic logging triggers
        sub = projects_dir / f"p{i}"
        sub.mkdir()
        (sub / f"s{i}.jsonl").write_text("")

    db_path = tmp_path / "test.db"

    class FakeNonTTY(io.StringIO):
        def isatty(self): return False
    fake_err = FakeNonTTY()
    monkeypatch.setattr("sys.stderr", fake_err)

    cli.cmd_scan(projects_dir=projects_dir, db_path=db_path)

    output = fake_err.getvalue()
    assert "\r" not in output, f"non-TTY output should not contain carriage returns: {output!r}"


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
