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
        if size + len(encoded) + 1 > MAX_INPUT_BYTES:
            break
        out.append(p)
        size += len(encoded) + 1
    return "\n".join(out)


def rank_cells_by_cost(db_path, max_cells=None, percentile=None):
    """
    Returns a sorted list of (date, cwd, cost_usd) tuples for the eager set —
    cells whose cost is at or above the Nth-percentile threshold, capped at
    max_cells, sorted descending by cost. Skips cells with cost == 0
    (unknown models).

    Percentile semantics (consistent with NumPy's default linear interpolation):
      • percentile=0   → returns all positive-cost cells (then capped).
      • percentile=80  → returns roughly the top 20% (default).
      • percentile=100 → returns only cells tied at the maximum cost.
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
