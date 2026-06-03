"""
Deduplication fingerprint generator.
Creates a deterministic SHA-256 hash for each transaction so that
re-importing the same statement doesn't create duplicate rows.
"""
from __future__ import annotations

import hashlib
from datetime import date
from decimal import Decimal


def transaction_fingerprint(
    account_id: str,
    txn_date: date,
    amount: Decimal,
    transaction_type: str,
    description: str,
    reference_number: str | None = None,
) -> str:
    """
    Stable fingerprint for a transaction.
    Uses reference_number when available (more precise),
    otherwise falls back to (date, amount, type, first-60-chars-of-desc).
    """
    if reference_number and reference_number.strip():
        key = f"{account_id}|{txn_date}|{reference_number.strip()}"
    else:
        desc_slug = description.strip()[:60].lower()
        key = f"{account_id}|{txn_date}|{amount}|{transaction_type}|{desc_slug}"

    return hashlib.sha256(key.encode()).hexdigest()


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
    key = f"DEMAT|{owner_id}|{isin.upper()}|{txn_date}|{qty}|{transaction_type}"
    return hashlib.sha256(key.encode()).hexdigest()
