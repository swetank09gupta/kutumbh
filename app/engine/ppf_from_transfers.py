"""
Synthesise a PPF ManualAsset for owners whose PPF contributions show up as
transfer debits in another bank's savings statement.

Use case: the user's PPF lives in Union Bank, but no PPF-specific statement
is ever emailed. The savings statement, however, contains every monthly /
ad-hoc transfer to the PPF account, e.g.:

    "MOBFT to: SUN DEP- ONLINE PPF /430623373587"
    "eTXN/To:<PPF_ACCT_NUM>/PPF/3 <REF_CODE>"

This module:
  1. Scans each owner's bank transactions for debits whose description matches
     a PPF-transfer pattern.
  2. Aggregates them into a synthetic ManualAsset row (asset_type='ppf') and
     a ManualAssetContribution row per detected transfer.
  3. Computes a compounded current_value at the configured rate (7.1% annual,
     quarterly compounding to match PPF convention).

Idempotency:
  - One synthetic asset per (owner, source institution) keyed by name
    "<institution> PPF (synthesised from transfers)".
  - Re-running deletes prior synthetic contribution rows and rebuilds from the
    latest set of transfer txns, so corrections (e.g. mis-categorised txns)
    flow through cleanly.
  - Any ManualAsset whose name does NOT contain "(synthesised from transfers)"
    is left untouched — user-entered PPF rows take precedence.

Skipped when:
  - No PPF-transfer debits exist for that owner.
  - The user already has a real (non-synthesised) PPF row for the same
    institution — we don't want to double-count when the user later imports
    or manually adds the real balance.
"""
from __future__ import annotations

import logging
import re
from datetime import date, timedelta
from decimal import Decimal
from typing import Optional

from sqlalchemy.orm import Session

from app.models.schemas import (
    Account, ManualAsset, ManualAssetContribution, Transaction,
)

logger = logging.getLogger(__name__)

# Matches descriptions like:
#   "MOBFT to: SUN DEP- ONLINE PPF /430623373587"
#   "eTXN/To:<PPF_ACCT_NUM>/PPF/3 <REF_CODE>"
#   "TRF TO PPF A/C 317902..."
_PPF_TRANSFER_RE = re.compile(
    r"\bPPF\b(?:\s*A/?C)?|\bPUBLIC\s+PROVIDENT\b|/PPF/|SUN\s+DEP[- ]+ONLINE\s+PPF",
    re.IGNORECASE,
)

# Default PPF rate. Government revises quarterly; 7.1% has been steady since
# 2020 except for very minor moves. Override per-asset via ManualAsset.interest_rate.
_PPF_RATE_DEFAULT = Decimal("7.1")

# PPF compounds annually (officially), but accumulating quarterly-credited
# interest gives a close-enough running balance for the dashboard.
_COMPOUND_N = 4


def _current_rate() -> float:
    from app.engine.small_savings import ppf_current_rate
    return ppf_current_rate()


def _ppf_contribution_txns_for(
    db: Session, owner_id: str,
) -> list[tuple[date, Decimal, str, str]]:
    """
    Return debit transactions on the owner's bank accounts whose description
    matches the PPF-transfer pattern.

    Returns: list of (date, amount, description, institution).
    """
    rows = (
        db.query(Transaction.date, Transaction.amount, Transaction.description,
                 Account.institution)
        .join(Account, Transaction.account_id == Account.id)
        .filter(
            Account.owner_id == owner_id,
            Account.account_type == "bank",
            Transaction.transaction_type == "debit",
            Transaction.amount > 0,
        )
        .order_by(Transaction.date)
        .all()
    )
    out: list[tuple[date, Decimal, str, str]] = []
    for r in rows:
        desc = r.description or ""
        if _PPF_TRANSFER_RE.search(desc):
            out.append((r.date, Decimal(str(r.amount)), desc, r.institution))
    return out


def _compound_to_today(
    contribs: list[tuple[date, Decimal]],
    annual_rate_pct: Decimal,
    today: date,
) -> Decimal:
    """
    Today's balance of a contribution stream by PPF's own rule and the officially notified rate history
    (app.engine.small_savings): lowest-balance-between-5th-and-month-end interest, credited every 31 March.
    Interest earned since the last 31 March (completed months) is included as accrued.
    """
    from app.engine.small_savings import ppf_balance
    credited, accrued = ppf_balance(contribs, today)
    return credited + accrued


def sync_ppf_from_transfers(
    db: Session, owner_id: Optional[str] = None, today: Optional[date] = None,
) -> dict:
    """
    Synthesize / refresh PPF ManualAsset rows from PPF-transfer debits.

    Pass owner_id=None to sweep every owner.
    """
    today = today or date.today()

    # Sweep owners
    if owner_id is None:
        owner_ids = [r[0] for r in db.query(Account.owner_id).distinct().all()]
    else:
        owner_ids = [owner_id]

    summary = {"owners_processed": 0, "assets_upserted": 0, "contribs_total": 0}

    for oid in owner_ids:
        txns = _ppf_contribution_txns_for(db, oid)
        if not txns:
            continue

        # Group by source institution — keeps a separate synthetic asset per
        # bank in case the user funds PPF from multiple savings accounts.
        by_inst: dict[str, list[tuple[date, Decimal]]] = {}
        for d, amt, _desc, inst in txns:
            by_inst.setdefault(inst, []).append((d, amt))

        for inst, flows in by_inst.items():
            # Skip if a real (non-synthesised) PPF asset for this owner already
            # exists — user-imported / SBI-parsed PPF rows take precedence.
            real_ppf = (
                db.query(ManualAsset)
                .filter(
                    ManualAsset.owner_id == oid,
                    ManualAsset.asset_type == "ppf",
                    ~ManualAsset.name.ilike("%synthesised from transfers%"),
                    # Same institution → definitely the same PPF
                    ManualAsset.institution == inst,
                )
                .first()
            )
            if real_ppf:
                logger.info(
                    "PPF sync: skipping %s (%s) — real PPF row exists",
                    oid, inst,
                )
                continue

            name = f"{inst} PPF (synthesised from transfers)"

            # Locate (or create) the synthetic asset row
            asset = (
                db.query(ManualAsset)
                .filter(ManualAsset.owner_id == oid,
                        ManualAsset.asset_type == "ppf",
                        ManualAsset.name == name)
                .first()
            )
            if asset is None:
                asset = ManualAsset(
                    owner_id=oid, asset_type="ppf", institution=inst,
                    name=name, interest_rate=Decimal(str(_current_rate())),
                    goal_tag="retirement",
                )
                db.add(asset)
                db.flush()  # need asset.id for contributions

            # Wipe + replace contribution rows so re-running is idempotent and
            # picks up txn corrections.
            (
                db.query(ManualAssetContribution)
                .filter(ManualAssetContribution.asset_id == asset.id)
                .delete(synchronize_session=False)
            )
            for d, amt in flows:
                db.add(ManualAssetContribution(
                    asset_id=asset.id, date=d, amount=amt,
                    notes="auto-detected from bank PPF transfer",
                ))

            total_invested = sum(amt for _, amt in flows)
            start_date = min(d for d, _ in flows)
            current = _compound_to_today(flows, asset.interest_rate
                                                or _PPF_RATE_DEFAULT, today)

            asset.invested_amount = total_invested
            asset.current_value   = current
            asset.start_date      = start_date
            asset.source_statement_date = today
            if asset.goal_tag is None:
                asset.goal_tag = "retirement"

            logger.info(
                "PPF sync (%s / %s): %d contributions, invested ₹%s → current ₹%s",
                oid, inst, len(flows), total_invested, current,
            )
            summary["assets_upserted"] += 1
            summary["contribs_total"]  += len(flows)

        summary["owners_processed"] += 1

    db.commit()
    return summary
