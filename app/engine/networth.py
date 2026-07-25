"""
Net-worth computation engine.
Aggregates bank balances, holdings, and manual assets into daily snapshots
for each owner individually and the family as a whole.
"""
from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models.schemas import (
    Account, Balance, Holding, ManualAsset,
    NetWorthSnapshot, Owner,
)

logger = logging.getLogger(__name__)

ZERO = Decimal(0)


def _latest_balance(db: Session, account_id) -> Decimal:
    row = (
        db.query(Balance.balance)
        .filter_by(account_id=account_id)
        .order_by(Balance.snapshot_date.desc())
        .first()
    )
    return row[0] if row else ZERO


def _latest_holdings_value(db: Session, account_id) -> Decimal:
    row = (
        db.query(func.sum(Holding.current_value))
        .filter(Holding.account_id == account_id)
        .filter(
            Holding.snapshot_date == (
                db.query(func.max(Holding.snapshot_date))
                .filter(Holding.account_id == account_id)
                .scalar_subquery()
            )
        )
        .first()
    )
    return row[0] or ZERO


def _latest_holdings_by_type(db: Session, account_id) -> tuple[Decimal, Decimal]:
    """Return (equity_value, mf_value) split by holding_type for the latest snapshot."""
    latest_date = (
        db.query(func.max(Holding.snapshot_date))
        .filter(Holding.account_id == account_id)
        .scalar()
    )
    if not latest_date:
        return ZERO, ZERO

    rows = (
        db.query(Holding.holding_type, func.sum(Holding.current_value))
        .filter(Holding.account_id == account_id, Holding.snapshot_date == latest_date)
        .group_by(Holding.holding_type)
        .all()
    )

    equity = ZERO
    mf     = ZERO
    for htype, val in rows:
        v = val or ZERO
        if htype == "mutual_fund":
            mf += v
        else:
            equity += v
    return equity, mf


def _deduped_investment_values(
    db: Session, owner_id: str, as_of: date | None = None
) -> tuple[Decimal, Decimal]:
    """
    Aggregate equity and MF values across all investment accounts for an owner,
    deduplicating by ISIN so the same security is never counted twice.

    as_of: use the most recent snapshot on or before this date per account.
           Defaults to the absolute latest snapshot.
    Sort key (lowest = processed first = wins the seen dict):
      1. account_type priority  (stocks=0 > demat=1 > mutual_fund=2)
      2. institution priority   (cdsl/cams/kfintech=0 > zerodha=1 > others=2)
    This ensures CDSL beats Zerodha for equity and CAMS beats Zerodha Coin for MFs,
    matching the dedup logic in the latest_holdings_deduped view.
    """
    ACCT_PRIORITY = {"stocks": 0, "demat": 1, "mutual_fund": 2}
    INST_PRIORITY = {"cdsl": 0, "cams": 0, "kfintech": 0, "zerodha": 1}

    accounts = (
        db.query(Account)
        .filter_by(owner_id=owner_id, is_active=True)
        .filter(Account.account_type.in_(["stocks", "demat", "mutual_fund"]))
        .all()
    )

    seen: dict[str, tuple[Decimal, str]] = {}
    no_isin_equity = ZERO
    no_isin_mf     = ZERO

    sort_key = lambda a: (  # noqa: E731
        ACCT_PRIORITY.get(a.account_type, 99),
        INST_PRIORITY.get((a.institution or "").lower(), 2),
    )
    for acc in sorted(accounts, key=sort_key):
        q = db.query(func.max(Holding.snapshot_date)).filter_by(account_id=acc.id)
        if as_of:
            q = q.filter(Holding.snapshot_date <= as_of)
        latest = q.scalar()
        if not latest:
            continue
        rows = (
            db.query(Holding.isin, Holding.current_value, Holding.holding_type)
            .filter_by(account_id=acc.id, snapshot_date=latest)
            .all()
        )
        for isin, val, htype in rows:
            val = val or ZERO
            if isin:
                if isin not in seen:
                    seen[isin] = (val, htype or "equity")
            else:
                if htype == "mutual_fund":
                    no_isin_mf += val
                else:
                    no_isin_equity += val

    equity = no_isin_equity
    mf     = no_isin_mf
    for val, htype in seen.values():
        if htype == "mutual_fund":
            mf += val
        else:
            equity += val

    return equity, mf


def compute_for_owner(db: Session, owner_id: str, snap_date: date | None = None) -> dict:
    snap_date = snap_date or date.today()
    accounts  = db.query(Account).filter_by(owner_id=owner_id, is_active=True).all()

    totals = {
        "bank_balance":    ZERO,
        "equity_value":    ZERO,
        "mf_value":        ZERO,
        "ppf_value":       ZERO,
        "fd_value":        ZERO,
        "real_estate_value": ZERO,
        "other_assets":    ZERO,
    }

    # Investment holdings — ISIN-deduped across stocks/demat/mutual_fund accounts
    eq, mf = _deduped_investment_values(db, owner_id, as_of=snap_date)
    totals["equity_value"] += eq
    totals["mf_value"]     += mf

    for acc in accounts:
        atype = acc.account_type
        if atype == "bank":
            totals["bank_balance"] += _latest_balance(db, acc.id)

    # Manual assets (PPF, FD, NPS, …) and liabilities (home loan, etc.)
    total_liabilities = ZERO
    manual = db.query(ManualAsset).filter_by(owner_id=owner_id).all()
    for asset in manual:
        val = asset.current_value or ZERO
        if asset.asset_type == "liability" or val < ZERO:
            total_liabilities += abs(val)
        elif asset.asset_type == "ppf":
            totals["ppf_value"] += val
        elif asset.asset_type in ("fd", "rd"):
            if asset.maturity_date and asset.maturity_date < snap_date:
                continue   # already matured — not part of current net worth
            totals["fd_value"] += val
        elif asset.asset_type == "real_estate":
            totals["real_estate_value"] += val
        else:
            totals["other_assets"] += val

    total_assets = sum(totals.values())
    net_worth    = total_assets - total_liabilities

    return {**totals, "total_assets": total_assets,
            "total_liabilities": total_liabilities, "net_worth": net_worth,
            "snapshot_date": snap_date, "owner_id": owner_id}


def snapshot_all(db: Session) -> list[dict]:
    """Compute and persist net-worth snapshots for all real owners + family aggregate."""
    # Exclude the "family" pseudo-owner — it's computed as the sum below, not individually
    real_owners = db.query(Owner).filter(Owner.id != "family").all()
    results = []
    family = {
        "bank_balance": ZERO, "equity_value": ZERO, "mf_value": ZERO,
        "ppf_value": ZERO, "fd_value": ZERO, "real_estate_value": ZERO, "other_assets": ZERO,
        "total_assets": ZERO, "total_liabilities": ZERO, "net_worth": ZERO,
        "snapshot_date": date.today(), "owner_id": "family",
    }

    for owner in real_owners:
        data = compute_for_owner(db, owner.id)
        _upsert_snapshot(db, data)
        results.append(data)
        for k in ("bank_balance", "equity_value", "mf_value",
                  "ppf_value", "fd_value", "real_estate_value", "other_assets",
                  "total_assets", "total_liabilities", "net_worth"):
            family[k] += data.get(k, ZERO)

    _upsert_snapshot(db, family)
    results.append(family)
    db.commit()
    return results


def backfill_snapshots(db: Session) -> int:
    """
    Compute and persist historical net-worth snapshots for every date where
    we have dated holding data (e.g. monthly CDSL CAS snapshots).
    Skips dates that already have a snapshot. Returns count of new snapshots.
    """
    from sqlalchemy import text

    # All distinct holding snapshot dates across all accounts
    all_dates = [
        r[0] for r in
        db.execute(text("SELECT DISTINCT snapshot_date FROM holdings ORDER BY snapshot_date")).fetchall()
    ]
    # Dates that already have a net-worth snapshot for at least one owner
    existing = {
        r[0] for r in
        db.execute(text("SELECT DISTINCT snapshot_date FROM net_worth_snapshots")).fetchall()
    }

    real_owners = db.query(Owner).filter(Owner.id != "family").all()
    created = 0

    for snap_date in all_dates:
        if snap_date in existing:
            continue  # already computed

        family = {
            "bank_balance": ZERO, "equity_value": ZERO, "mf_value": ZERO,
            "ppf_value": ZERO, "fd_value": ZERO, "real_estate_value": ZERO, "other_assets": ZERO,
            "total_assets": ZERO, "total_liabilities": ZERO, "net_worth": ZERO,
            "snapshot_date": snap_date, "owner_id": "family",
        }
        for owner in real_owners:
            data = compute_for_owner(db, owner.id, snap_date=snap_date)
            _upsert_snapshot(db, data)
            for k in ("bank_balance", "equity_value", "mf_value",
                      "ppf_value", "fd_value", "real_estate_value", "other_assets",
                      "total_assets", "total_liabilities", "net_worth"):
                family[k] += data.get(k, ZERO)
        _upsert_snapshot(db, family)
        created += 1

    db.commit()
    logger.info("backfill_snapshots: created %d historical snapshots", created)
    return created


def _upsert_snapshot(db: Session, data: dict) -> None:
    """Atomic upsert using PostgreSQL ON CONFLICT DO UPDATE — safe for concurrent calls."""
    fields = ("bank_balance", "equity_value", "mf_value", "ppf_value",
              "fd_value", "real_estate_value", "other_assets", "total_assets", "total_liabilities", "net_worth")

    values = {
        "snapshot_date":     data["snapshot_date"],
        "owner_id":          data["owner_id"],
        **{k: data.get(k, ZERO) for k in fields},
    }

    stmt = (
        pg_insert(NetWorthSnapshot)
        .values(**values)
        .on_conflict_do_update(
            constraint="uq_networth_snapshot",
            set_={k: data.get(k, ZERO) for k in fields},
        )
    )
    db.execute(stmt)
