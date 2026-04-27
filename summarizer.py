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
