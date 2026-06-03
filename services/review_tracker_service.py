"""Append-only review history persisted as ``review_history.json``.

Each record::

    {
        "repo":          "owner/repo",
        "pr":            123 | None,
        "date":          "2026-05-03T12:34:56+00:00",
        "review_length": 4123,
        "issues_count":  17,
        "posted":        {"posted": 11, "failed": 0, "skipped": 0} | None
    }

We cap history at the most recent 100 entries to keep the file tiny.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger("tracker")

HISTORY_FILE = Path(os.getenv("REVIEW_HISTORY_FILE", "review_history.json"))
MAX_HISTORY = 100


def save_review_record(
    repo: str,
    pr: Optional[int],
    review: str,
    *,
    issues_count: int = 0,
    posted: Optional[dict] = None,
) -> None:
    """Append one record to ``HISTORY_FILE`` (creates it if missing)."""
    history = get_review_history()
    history.append({
        "repo": repo,
        "pr": pr,
        "date": datetime.now(timezone.utc).isoformat(),
        "review_length": len(review or ""),
        "issues_count": issues_count,
        "posted": posted,
    })
    if len(history) > MAX_HISTORY:
        history = history[-MAX_HISTORY:]
    try:
        HISTORY_FILE.write_text(json.dumps(history, indent=2))
        log.info("Saved review record: %s PR#%s", repo, pr)
    except Exception as exc:
        log.warning("Could not write %s: %s", HISTORY_FILE, exc)


def get_review_history() -> list[dict]:
    """Return the history list (or ``[]`` if missing/corrupt)."""
    if not HISTORY_FILE.exists():
        return []
    try:
        data = json.loads(HISTORY_FILE.read_text())
        return data if isinstance(data, list) else []
    except Exception as exc:
        log.warning("Could not read %s: %s", HISTORY_FILE, exc)
        return []
