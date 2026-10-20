"""
Deduplication fingerprint generator.
Creates a deterministic SHA-256 hash for each transaction so that
re-importing the same statement doesn't create duplicate rows.
"""
from __future__ import annotations

import hashlib
import re
from datetime import date
from decimal import Decimal


_NORM_RE = re.compile(r"[^A-Za-z0-9]+")


def _normalize_amount(amount) -> str:
    """Canonical string for an amount used in fingerprint keys.

    The DB stores Decimal values with their original scale (e.g. "25.000")
    but a fresh parser run may produce "25" or "25.0" for the same value.
    Normalise by stripping trailing zeros so all three collapse to "25".

    Uses Decimal.normalize() then fixed-point format:
        Decimal("25.000").normalize() → Decimal("25")  → "25"
        Decimal("597.035").normalize() → Decimal("597.035") → "597.035"
        Decimal("151662").normalize()  → Decimal("151662")  → "151662"
    """
    from decimal import Decimal as _D, InvalidOperation
    try:
        d = _D(str(amount)).normalize()
        return format(d, 'f')
    except (InvalidOperation, TypeError):
        return str(amount)


def _normalize_ref(ref: str | None) -> str:
    """Strip whitespace, punctuation, and leading zeros from a reference number.

    HDFC sometimes emits the same UTR in two formats — once padded with
    zeros ("0000008913231095") and once not ("008913231095") — across
    different sections of the same PDF. Without this normalization both
    rows pass dedup as distinct, doubling the recorded spend.
    """
    if not ref:
        return ""
    return _NORM_RE.sub("", ref).lstrip("0").lower()


def _normalize_desc(desc: str | None) -> str:
    """Collapse whitespace + strip non-alphanumeric for hashing only.

    Prevents "ACH D- HDFC BANK LTD" and "ACHD-HDFCBANKLTD" — same wire,
    different rendering — from getting different fingerprints.
    """
    return _NORM_RE.sub("", desc or "").lower()


def transaction_fingerprint(
    account_id: str,
    txn_date: date,
    amount: Decimal,
    transaction_type: str,
    description: str,
    reference_number: str | None = None,
    ordinal: int = 1,
    with_direction: bool = False,
) -> str:
    """
    Stable fingerprint for a transaction.
    Uses reference_number when available (more precise). For bank accounts (`with_direction`) the key also carries the amount
    and direction: a UPI reversal credit has the same reference and date as the debit it undoes, and a ref-only key silently
    dropped it (ADR-0038). Broker rows keep the ref-only key: the ledger's trades point at their cashflow row by this
    fingerprint, so re-keying them orphaned 216 trades (found Oct 2026).
    Without a reference it falls back to (date, amount, type, normalized-desc).

    `ordinal` is the position among identical reference-less rows of one statement that the running balance
    proves are separate transactions (two ₹1,016 sweep credits on one day); the first keeps the original key.
    """
    ref_norm  = _normalize_ref(reference_number)
    amt_norm  = _normalize_amount(amount)
    if ref_norm:
        key = f"{account_id}|{txn_date}|{ref_norm}" + (f"|{transaction_type}|{amt_norm}" if with_direction else "")
    else:
        desc_slug = _normalize_desc(description)[:80]
        key = f"{account_id}|{txn_date}|{amt_norm}|{transaction_type}|{desc_slug}"
    if ordinal > 1:
        key += f"|#{ordinal}"

    return hashlib.sha256(key.encode()).hexdigest()


def is_ordinal_fingerprint(stored: str, account_id: str, txn_date: date, amount: Decimal, transaction_type: str,
                           description: str, reference_number: str | None, max_ordinal: int = 12) -> bool:
    """True when `stored` is the fingerprint of the 2nd..nth identical row (so maintenance must not merge it away)."""
    if _normalize_ref(reference_number):
        return False
    return any(transaction_fingerprint(account_id, txn_date, amount, transaction_type, description, None, k) == stored
               for k in range(2, max_ordinal + 1))


def equity_demat_fingerprint(
    owner_id: str,
    isin: str,
    txn_date: date,
    qty: Decimal,
    transaction_type: str,
) -> str:
    """
    Source-agnostic fingerprint for equity demat ISIN movements.

    Both CDSL CAS and Zerodha monthly demat report the same underlying
    settlement credits/debits for the same demat account.  Using owner_id
    (not account_id) means the same event won't be inserted twice regardless
    of which source processes it first.
    """
    key = f"DEMAT|{owner_id}|{isin.upper()}|{txn_date}|{_normalize_amount(qty)}|{transaction_type}"
    return hashlib.sha256(key.encode()).hexdigest()
