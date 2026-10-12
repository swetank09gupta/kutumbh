"""
Spending the alerts already show, before the statement arrives (ADR-0037, revising ADR-0025).

A pending alert (newer than the newest statement row of its bank) is counted as spending ONLY when it is certainly spending:
a UPI payment out to a named payee that is not the household itself and that the categoriser does not read as a transfer,
investment or EMI. Everything else stays out until the statement is read: own-account transfers (an alert only names the
payee, often truncated: "Swetan"), sweeps and standing instructions (no payee), SIP/ACH debits (investments are decided by
arithmetic against the units), credits of any kind. So the provisional figure is a floor of real spending, never an overcount.
When the statement lands the alert becomes 'matched' (or 'dup') and leaves the total in the same run: nothing is counted twice.
"""
from __future__ import annotations

import re

from sqlalchemy.orm import Session

from app.etl.categorizer import categorise
from app.models.schemas import Owner

_NOT_SPENDING = {"transfer", "investment", "emi", "income", "insurance_premium"}


def own_tokens(db: Session) -> set[str]:
    """Upper-case name fragments of the household: every name word of 5+ letters and the first five letters of the first word
    (alert payees are truncated to a few letters: "Swetan")."""
    out: set[str] = set()
    for (name,) in db.query(Owner.name).all():
        words = re.findall(r"[A-Za-z]{3,}", name or "")
        out |= {w.upper() for w in words if len(w) >= 5}
        if words and len(words[0]) >= 5:
            out.add(words[0].upper()[:5])
    return out


def classify(kind: str, direction: str, payee: str | None, vpa: str | None, narration: str | None,
             tokens: set[str]) -> tuple[str, str | None, bool]:
    """(category, subcategory, counted as provisional spending) for one alert."""
    text = " ".join(x for x in (payee, vpa, narration) if x)
    cat = categorise(text)
    if direction != "debit" or kind != "upi" or not (payee or "").strip():
        return cat.category, cat.subcategory, False
    flat = re.sub(r"[^A-Z]", "", f"{payee} {vpa or ''}".upper())
    if any(t in flat for t in tokens) or cat.category in _NOT_SPENDING:
        return cat.category, cat.subcategory, False
    return cat.category, cat.subcategory, True
