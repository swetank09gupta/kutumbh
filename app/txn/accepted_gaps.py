"""
Statement months the owner has confirmed do not exist at the bank (never mailed, not downloadable).

The balance chain (app/txn/continuity.py) breaks across such a month by that month's net movement. Nothing can fill it and
nothing is invented: the account's balance today is its newest statement or alert, which does not depend on the missing month.
Accepting the gap says so, so the check stops warning about it. Private, one JSON list in `config/accepted_gaps.json`
(override with ACCEPTED_GAPS_FILE):

    [{"owner": "user", "account": "SBI 8660", "month": "2024-10", "note": "SBI never issued it"}]
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


def _path() -> Path:
    return Path(os.getenv("ACCEPTED_GAPS_FILE", "config/accepted_gaps.json"))


def load() -> list[dict]:
    p = _path()
    try:
        rows = json.loads(p.read_text())
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        logger.warning("accepted gaps file %s unreadable: %s", p, exc)
        return []
    return [r for r in rows if isinstance(r, dict) and r.get("owner") and r.get("account") and r.get("month")]


def accepted(owner: str, account: str, month: str) -> dict | None:
    return next((r for r in load() if (r["owner"], r["account"], r["month"]) == (owner, account, month)), None)
