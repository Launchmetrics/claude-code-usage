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
