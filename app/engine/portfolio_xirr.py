"""
Portfolio-level XIRR / CAGR aggregator.

Builds a unified cashflow stream across every investment vehicle an owner
holds, so XIRR reflects the actual money-weighted return.

Sign convention (matches xirr_engine):
  buy / contribution / deposit  → outflow  (negative)
  sell / redeem / dividend      → inflow   (positive)
  current portfolio value       → terminal inflow on today

Coverage:
  • Stocks / MF / bonds (Zerodha demat, CAS, etc.) — every transaction +
    latest-snapshot current_value.
  • Manual assets with start_date OR contribution rows: gold, silver, SGB,
    NPS, FD, PPF, RD, real_estate, insurance.
  • Bank savings/current accounts → EXCLUDED (cash, not an investment).
  • Manual assets without any dated cashflow → EXCLUDED with a warning.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date
from decimal import Decimal
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.engine.holdings_view import latest_holdings as _latest_holdings
from app.engine.xirr_engine import xirr, cagr
from app.models.schemas import (
    Account, Holding, ManualAsset, ManualAssetContribution, Transaction,
)

logger = logging.getLogger(__name__)

ZERO = Decimal(0)

# Account types whose transactions feed XIRR (everything else is excluded — bank
# savings, current accounts, etc. — because cash flows there are not investments).
_INVESTING_ACCOUNT_TYPES = {"stocks", "mutual_fund", "demat"}

# Manual asset types that are real investments (the rest — e.g. liabilities — are skipped).
_INVESTING_ASSET_TYPES = {
    "fd", "rd", "ppf", "nps",
    "gold", "silver", "sgb",
    "real_estate", "insurance", "other",
}


def _account_cashflows(
    db: Session, account: Account, today: date,
) -> tuple[list[tuple[date, float]], Decimal, Decimal]:
    """
    Returns (cashflows, invested, current_value) for a single investing account.

    cashflows:
        every transaction signed (buy=-, sell/dividend=+) plus the latest-snapshot
        current_value as a terminal positive cashflow on `today`.
    invested:
        sum of debit (buy) transaction amounts. Used as a CAGR denominator.
    current_value:
        sum of current_value across the latest holdings snapshot.
    """
    # Latest snapshot for this account
    latest_date = (
        db.query(func.max(Holding.snapshot_date))
        .filter(Holding.account_id == account.id)
        .scalar()
    )
    current_value = ZERO
    if latest_date:
        rows = (
            db.query(func.sum(Holding.current_value))
            .filter(Holding.account_id == account.id,
                    Holding.snapshot_date == latest_date)
            .scalar()
        )
        current_value = rows or ZERO

    txns = (
        db.query(Transaction)
        .filter(Transaction.account_id == account.id)
        .order_by(Transaction.date)
        .all()
    )

    cfs: list[tuple[date, float]] = []
    invested = ZERO
    for t in txns:
        amt = float(t.amount or 0)
        if amt <= 0:
            continue
        # Zerodha monthly-demat statements emit transactions where `amount` is
        # the share/unit COUNT, not rupees (see zerodha.py _parse_monthly_demat).
        # Those rows start with "DEMAT " — exclude from cashflow XIRR.
        desc = (t.description or "").upper()
        if desc.startswith("DEMAT "):
            continue
        if t.transaction_type == "debit":
            cfs.append((t.date, -amt))
            invested += Decimal(str(amt))
        elif t.transaction_type == "credit":
            cfs.append((t.date, amt))

    if current_value > 0:
        cfs.append((today, float(current_value)))

    return cfs, invested, current_value


def _manual_asset_cashflows(
    db: Session, asset: ManualAsset, today: date,
) -> Optional[tuple[list[tuple[date, float]], Decimal, Decimal]]:
    """
    Returns (cashflows, invested, current_value) for a manual asset, or None if
    the asset has no usable cashflow data (excluded from XIRR).

    Cashflow construction:
        1. If contribution rows exist → each row is an outflow on its date.
        2. Else if start_date + invested_amount → a single outflow on start_date.
        3. Else → return None (asset is excluded with a warning).

        current_value is appended as a terminal inflow on `today`.
    """
    contribs = (
        db.query(ManualAssetContribution)
        .filter(ManualAssetContribution.asset_id == asset.id)
        .order_by(ManualAssetContribution.date)
        .all()
    )

    cfs: list[tuple[date, float]] = []
    invested = ZERO

    if contribs:
        for c in contribs:
            amt = float(c.amount or 0)
            if amt <= 0:
                continue
            cfs.append((c.date, -amt))
            invested += Decimal(str(amt))
    elif asset.start_date and asset.invested_amount and asset.invested_amount > 0:
        amt = float(asset.invested_amount)
        cfs.append((asset.start_date, -amt))
        invested = Decimal(str(amt))
    else:
        logger.info(
            "Skipping manual asset '%s' (%s): no start_date or contributions",
            asset.name, asset.asset_type,
        )
        return None

    current = asset.current_value or ZERO

    # ── FD / RD interest accrual ──────────────────────────────────────────────
    # The SBI parser stores current_value = principal (no interest growth) for
    # term deposits. Compound the principal at the FD's stated rate from
    # start_date to min(today, maturity_date) so XIRR sees real growth.
    # PPF is typically already credited correctly via the year-end snapshot.
    if asset.asset_type in ("fd", "rd") \
            and asset.start_date and asset.interest_rate \
            and asset.invested_amount and asset.invested_amount > 0:
        principal = float(asset.invested_amount)
        rate      = float(asset.interest_rate) / 100.0   # decimal, e.g. 0.065
        end_date  = asset.maturity_date if (asset.maturity_date and asset.maturity_date < today) else today
        years_elapsed = max(0.0, (end_date - asset.start_date).days / 365.25)
        # Quarterly compounding — matches SBI's standard FD interest convention.
        n = 4
        synthetic = principal * (1.0 + rate / n) ** (n * years_elapsed)
        synthetic_dec = Decimal(f"{synthetic:.2f}")
        # Use the synthesized value if it's higher than the parser-provided one
        # (which is usually just the principal). If a fresh statement has reported
        # a higher current_value already, keep that.
        current = max(current, synthetic_dec)

    if current > 0:
        cfs.append((today, float(current)))

    return cfs, invested, current


def _account_cashflows_by_bucket(
    db: Session, account: Account, today: date,
) -> dict[str, tuple[list[tuple[date, float]], Decimal, Decimal]]:
    """
    Per-bucket cashflow / invested / current breakdown for one account.

    Each transaction is routed to a bucket via its ISIN's holding_type in the
    latest holdings snapshot. Transactions without an ISIN fall back to the
    account's majority bucket (by current_value).

    Returns: { bucket_name -> (cfs, invested, current_value) }

    This is the per-ISIN-attribution path mentioned in the original comment —
    it replaces the previous "dump everything into the majority bucket" logic
    that caused Zerodha equity contract-note buys to be mis-attributed to MF.
    """
    # Latest holdings snapshot — used both for ISIN→holding_type lookup and
    # to know current value per bucket.
    latest = (
        db.query(func.max(Holding.snapshot_date))
        .filter(Holding.account_id == account.id)
        .scalar()
    )
    isin_to_type: dict[str, str] = {}
    type_value:   dict[str, Decimal] = {}
    if latest:
        rows = (
            db.query(Holding)
            .filter(Holding.account_id == account.id, Holding.snapshot_date == latest)
            .all()
        )
        for h in rows:
            ht = (h.holding_type or "equity")
            type_value[ht] = type_value.get(ht, ZERO) + (h.current_value or ZERO)
            if h.isin:
                isin_to_type[h.isin] = ht

    # Majority bucket — fallback for txns without an ISIN.
    majority = (
        max(type_value.items(), key=lambda kv: kv[1])[0]
        if type_value else "equity"
    )

    txns = (
        db.query(Transaction)
        .filter(Transaction.account_id == account.id)
        .order_by(Transaction.date)
        .all()
    )

    per_bucket: dict[str, dict] = defaultdict(lambda: {"cfs": [], "invested": ZERO})
    for t in txns:
        amt = float(t.amount or 0)
        if amt <= 0:
            continue
        desc = (t.description or "").upper()
        # Zerodha monthly-demat statements emit transactions where `amount`
        # is the share/unit COUNT, not rupees. Exclude.
        if desc.startswith("DEMAT "):
            continue
        bucket = isin_to_type.get(t.isin or "", majority) if t.isin else majority
        if t.transaction_type == "debit":
            per_bucket[bucket]["cfs"].append((t.date, -amt))
            per_bucket[bucket]["invested"] += Decimal(str(amt))
        elif t.transaction_type == "credit":
            per_bucket[bucket]["cfs"].append((t.date, amt))

    # Stitch in current-value terminal inflow per bucket.
    all_buckets = set(per_bucket.keys()) | set(type_value.keys())
    result: dict[str, tuple[list[tuple[date, float]], Decimal, Decimal]] = {}
    for bucket in all_buckets:
        cfs = list(per_bucket.get(bucket, {}).get("cfs", []))
        inv = per_bucket.get(bucket, {}).get("invested", ZERO)
        cur = type_value.get(bucket, ZERO)
        if cur > 0:
            cfs.append((today, float(cur)))
        result[bucket] = (cfs, inv, cur)
    return result


def _bucket_for_account(account: Account, holding_types: dict[str, Decimal]) -> str:
    """Label an account's contribution by asset class for the breakdown view."""
    if account.account_type == "stocks":
        # If it's a Zerodha-style mixed account, attribute by majority holding_type
        if holding_types:
            return max(holding_types.items(), key=lambda kv: kv[1])[0] or "equity"
        return "equity"
    if account.account_type == "mutual_fund":
        return "mutual_fund"
    if account.account_type == "demat":
        return "demat"
    return account.account_type or "other"


def _holding_type_split(db: Session, account_id) -> dict[str, Decimal]:
    """Latest-snapshot current_value grouped by holding_type for one account."""
    latest = (
        db.query(func.max(Holding.snapshot_date))
        .filter(Holding.account_id == account_id)
        .scalar()
    )
    if not latest:
        return {}
    rows = (
        db.query(Holding.holding_type, func.sum(Holding.current_value))
        .filter(Holding.account_id == account_id, Holding.snapshot_date == latest)
        .group_by(Holding.holding_type)
        .all()
    )
    return {ht or "equity": (val or ZERO) for ht, val in rows}


def _years_from_first_cf(cashflows: list[tuple[date, float]], today: date) -> float:
    """Years between the earliest cashflow and today (used for CAGR)."""
    if not cashflows:
        return 0.0
    first = min(d for d, _ in cashflows)
    return (today - first).days / 365.25


def compute_owner_returns(db: Session, owner_id: str, today: Optional[date] = None) -> dict:
    """
    Returns:
      {
        owner_id, as_of,
        total: {invested, current, gain, gain_pct, xirr_pct, cagr_pct, years},
        by_class: [
          {class, invested, current, gain, gain_pct, xirr_pct, cagr_pct, years, count}
        ],
        skipped: [ {kind, name, reason}, ... ],
      }

    Pass owner_id="family" to aggregate cashflows across every real owner.
    """
    today = today or date.today()

    # ── Family view: collect cashflows across every real owner ──────────────
    if owner_id == "family":
        from app.models.schemas import Owner
        real_owners = db.query(Owner).filter(Owner.id != "family").all()
        owner_ids = [o.id for o in real_owners]
    else:
        owner_ids = [owner_id]

    # Per-asset-class accumulator
    class_cfs:      dict[str, list[tuple[date, float]]] = defaultdict(list)
    class_invested: dict[str, Decimal] = defaultdict(lambda: ZERO)
    class_current:  dict[str, Decimal] = defaultdict(lambda: ZERO)
    class_count:    dict[str, int]     = defaultdict(int)

    skipped: list[dict] = []

    # ── Investment accounts ──────────────────────────────────────────────────
    accounts = (
        db.query(Account)
        .filter(Account.owner_id.in_(owner_ids), Account.is_active == True)  # noqa: E712
        .all()
    )
    for acc in accounts:
        if acc.account_type not in _INVESTING_ACCOUNT_TYPES:
            continue

        # Mixed account (Zerodha stocks holds equity + MF + bonds): split
        # cashflows per-ISIN across buckets so contract-note equity buys land
        # in "equity" and Coin SIPs land in "mutual_fund" rather than getting
        # collapsed into a single majority bucket.
        if acc.account_type == "stocks":
            per_bucket = _account_cashflows_by_bucket(db, acc, today)
            if not per_bucket:
                skipped.append({"kind": "account", "name": f"{acc.institution} ({acc.account_type})",
                                "reason": "no cashflows"})
                continue
            for bucket, (cfs, invested, current) in per_bucket.items():
                if not cfs:
                    continue
                class_cfs[bucket].extend(cfs)
                class_invested[bucket] += invested
                class_current[bucket]  += current
                class_count[bucket]    += 1
            continue

        # Single-type accounts (e.g. CDSL demat = pure equity snapshot, or
        # dedicated MF accounts) keep the old single-bucket path.
        cfs, invested, current = _account_cashflows(db, acc, today)
        if not cfs:
            skipped.append({"kind": "account", "name": f"{acc.institution} ({acc.account_type})",
                            "reason": "no cashflows"})
            continue
        types = _holding_type_split(db, acc.id)
        primary = _bucket_for_account(acc, types)
        class_cfs[primary].extend(cfs)
        class_invested[primary] += invested
        class_current[primary]  += current
        class_count[primary]    += 1

    # ── Manual assets ────────────────────────────────────────────────────────
    manuals = db.query(ManualAsset).filter(ManualAsset.owner_id.in_(owner_ids)).all()
    for asset in manuals:
        if asset.asset_type not in _INVESTING_ASSET_TYPES:
            continue
        # Skip matured term deposits — their proceeds were rolled into either a
        # new FD (separate row) or savings (excluded from XIRR). Including them
        # would attribute the post-maturity dead time to FD bucket, dragging XIRR.
        if asset.asset_type in ("fd", "rd") \
                and asset.maturity_date and asset.maturity_date < today:
            skipped.append({"kind": "manual_asset", "name": asset.name or asset.asset_type,
                            "reason": "matured before today"})
            continue
        out = _manual_asset_cashflows(db, asset, today)
        if out is None:
            skipped.append({"kind": "manual_asset", "name": asset.name or asset.asset_type,
                            "reason": "no start_date or contributions"})
            continue
        cfs, invested, current = out
        bucket = asset.asset_type
        class_cfs[bucket].extend(cfs)
        class_invested[bucket] += invested
        class_current[bucket]  += current
        class_count[bucket]    += 1

    # ── Per-class metrics ────────────────────────────────────────────────────
    by_class: list[dict] = []
    total_cfs:      list[tuple[date, float]] = []
    total_invested = ZERO
    total_current  = ZERO

    for cls in sorted(class_cfs.keys()):
        cfs   = class_cfs[cls]
        inv   = class_invested[cls]
        cur   = class_current[cls]
        years = _years_from_first_cf(cfs, today)
        # A class with no cost basis (e.g. Zerodha equity where buys live only in
        # monthly-demat statements that carry share *counts*, not rupees) has just
        # a terminal positive cashflow. Including it in the total stream creates a
        # phantom inflow with no matching outflow, inflating overall XIRR.
        # Show the class with null returns and EXCLUDE it from totals.
        no_cost_basis = (inv <= ZERO)
        by_class.append({
            "class":     cls,
            "count":     class_count[cls],
            "invested":  float(inv),
            "current":   float(cur),
            "gain":      float(cur - inv) if not no_cost_basis else None,
            "gain_pct":  round(float((cur - inv) / inv * 100), 2) if inv > 0 else None,
            "years":     round(years, 2),
            "xirr_pct":  None if no_cost_basis else xirr(cfs),
            "cagr_pct":  cagr(float(inv), float(cur), years) if inv > 0 else None,
            "excluded_from_total": no_cost_basis,
            "exclusion_reason":    "no cost basis (missing purchase cashflows)" if no_cost_basis else None,
        })
        if no_cost_basis:
            skipped.append({
                "kind": "class", "name": cls,
                "reason": f"no cost basis — current ₹{float(cur):,.0f} excluded from total XIRR",
            })
            continue
        total_cfs.extend(cfs)
        total_invested += inv
        total_current  += cur

    total_years = _years_from_first_cf(total_cfs, today)
    total = {
        "invested":  float(total_invested),
        "current":   float(total_current),
        "gain":      float(total_current - total_invested),
        "gain_pct":  round(float((total_current - total_invested) / total_invested * 100), 2)
                     if total_invested > 0 else None,
        "years":     round(total_years, 2),
        "xirr_pct":  xirr(total_cfs),
        "cagr_pct":  cagr(float(total_invested), float(total_current), total_years)
                     if total_invested > 0 else None,
    }

    # ── Per-instrument XIRR (current holdings, by ISIN) ──────────────────────
    instruments = _per_instrument_returns(db, owner_ids, today)

    # ── Per-goal XIRR (group holdings by goal_tag) ───────────────────────────
    by_goal = _per_goal_returns(db, owner_ids, today)

    return {
        "owner_id":    owner_id,
        "as_of":       today.isoformat(),
        "total":       total,
        "by_class":    by_class,
        "instruments": instruments,
        "by_goal":     by_goal,
        "skipped":     skipped,
    }


def _normalize_fund_name(s: str) -> str:
    """
    Reduce a fund/instrument name to a canonical token sequence so that
    Coin's "MIRAE ASSET ELSS TAX SAVER FUND - DIRECT GROWTH" and the
    monthly demat's "MIRAE ASSET MF-MIRAE ASSET ELSS TAX SAVER FUND-DIRECT-GROWTH"
    collapse to the same key.
    """
    if not s:
        return ""
    import re
    txt = s.upper()
    # Strip AMC prefix patterns like "ABSL MF-", "ICICI PRUD AMC LTD#ICICI PRUD MF-"
    txt = re.sub(r'^[A-Z][A-Z0-9 &]*?(MF|AMC[^#]*)[#\-]+', '', txt)
    # Drop common noise tokens
    noise = [
        'DIRECT', 'REGULAR', 'GROWTH', 'IDCW', 'DIVIDEND', 'PLAN', 'OPTION',
        'PAYOUT', 'REINVESTMENT', 'BONUS', 'PROGRAMME', 'SCHEME',
    ]
    txt = re.sub(r'[^A-Z0-9]+', ' ', txt)  # punctuation → space
    parts = [p for p in txt.split() if p and p not in noise]
    return ' '.join(parts)


def _per_instrument_returns(
    db: Session, owner_ids: list[str], today: date,
) -> list[dict]:
    """
    For every ISIN in the latest holdings snapshot, build a private cashflow
    stream from the transactions tagged with that ISIN and compute XIRR/CAGR.
    Coin transactions don't carry ISIN — match by fund-name normalization instead.
    """
    holdings = _latest_holdings(db, owner_ids=owner_ids)

    # Pre-fetch ALL relevant transactions (with or without ISIN — Coin records
    # have folio + counterparty but no ISIN; we'll match those by fund-name).
    txns = (
        db.query(Transaction)
        .join(Account, Account.id == Transaction.account_id)
        .filter(Account.owner_id.in_(owner_ids))
        .order_by(Transaction.date)
        .all()
    )

    # Build holding ISIN ↔ normalized name map for the ISIN-less Coin txns.
    name_to_isin: dict[str, str] = {}
    for h in holdings:
        if h.isin:
            key = _normalize_fund_name(h.instrument_name or h.symbol or "")
            if key:
                name_to_isin.setdefault(key, h.isin)

    txns_by_isin: dict[str, list[Transaction]] = defaultdict(list)
    for t in txns:
        if (t.description or "").upper().startswith("DEMAT "):
            continue   # unit-count rows, not rupees
        isin = t.isin
        if not isin:
            # Coin path: try counterparty fund-name match.
            key = _normalize_fund_name(t.counterparty or "")
            isin = name_to_isin.get(key)
            if not isin:
                # Fuzzy fallback — substring containment in either direction
                for nk, ni in name_to_isin.items():
                    if nk and key and (nk in key or key in nk):
                        isin = ni
                        break
        if isin:
            txns_by_isin[isin].append(t)

    out: list[dict] = []
    for h in holdings:
        if not h.current_value or float(h.current_value) <= 0:
            continue
        cur = float(h.current_value)
        cfs: list[tuple[date, float]] = []
        invested = 0.0
        for t in txns_by_isin.get(h.isin or "", []):
            amt = float(t.amount or 0)
            if amt <= 0:
                continue
            if t.transaction_type == "debit":
                cfs.append((t.date, -amt))
                invested += amt
            elif t.transaction_type == "credit":
                cfs.append((t.date, amt))

        # Always append terminal current_value
        cfs.append((today, cur))

        # Year-span for CAGR
        if cfs:
            first = min(d for d, _ in cfs)
            years = max(0.0, (today - first).days / 365.25)
        else:
            years = 0.0

        out.append({
            "isin":       h.isin,
            "name":       h.instrument_name or h.symbol or h.isin,
            "type":       h.holding_type or "equity",
            "owner":      h.owner_id,
            "units":      float(h.units or 0),
            "current":    cur,
            "invested":   invested if invested > 0 else float(h.invested_value or 0),
            "gain":       cur - (invested if invested > 0 else float(h.invested_value or 0)),
            "years":      round(years, 2),
            "xirr_pct":   xirr(cfs) if invested > 0 else None,
            "cagr_pct":   cagr(invested, cur, years) if invested > 0 else (
                          cagr(float(h.avg_cost_nav or 0), float(h.current_nav or 0),
                               (today - h.first_buy_date).days / 365.25)
                          if (h.avg_cost_nav and h.current_nav and h.first_buy_date) else None
                          ),
            "goal_tag":   h.goal_tag,
        })

    out.sort(key=lambda x: x["current"], reverse=True)
    return out


def _per_goal_returns(
    db: Session, owner_ids: list[str], today: date,
) -> list[dict]:
    """
    Group current holdings by goal_tag, build a combined cashflow stream from
    every transaction whose ISIN matches a holding in that goal, and compute
    XIRR/CAGR per goal.
    """
    holdings = _latest_holdings(db, owner_ids=owner_ids)

    # Build name→ISIN map (for matching Coin txns that lack ISIN)
    name_to_isin: dict[str, str] = {}
    for h in holdings:
        if h.isin:
            key = _normalize_fund_name(h.instrument_name or h.symbol or "")
            if key:
                name_to_isin.setdefault(key, h.isin)

    # Group ISINs by goal_tag (None → "untagged")
    isins_by_goal: dict[str, set[str]] = defaultdict(set)
    cur_by_goal:   dict[str, float]    = defaultdict(float)
    count_by_goal: dict[str, int]      = defaultdict(int)
    for h in holdings:
        tag = h.goal_tag or "untagged"
        if h.isin:
            isins_by_goal[tag].add(h.isin)
        cur_by_goal[tag] += float(h.current_value or 0)
        count_by_goal[tag] += 1

    txns = (
        db.query(Transaction)
        .join(Account, Account.id == Transaction.account_id)
        .filter(Account.owner_id.in_(owner_ids))
        .order_by(Transaction.date)
        .all()
    )

    def _txn_isin(t: Transaction) -> str | None:
        if t.isin:
            return t.isin
        key = _normalize_fund_name(t.counterparty or "")
        if key in name_to_isin:
            return name_to_isin[key]
        for nk, ni in name_to_isin.items():
            if nk and key and (nk in key or key in nk):
                return ni
        return None

    out: list[dict] = []
    for tag, isins in isins_by_goal.items():
        cfs: list[tuple[date, float]] = []
        invested = 0.0
        for t in txns:
            ti = _txn_isin(t)
            if ti not in isins:
                continue
            if (t.description or "").upper().startswith("DEMAT "):
                continue
            amt = float(t.amount or 0)
            if amt <= 0:
                continue
            if t.transaction_type == "debit":
                cfs.append((t.date, -amt))
                invested += amt
            elif t.transaction_type == "credit":
                cfs.append((t.date, amt))

        cur = cur_by_goal[tag]
        if cur > 0:
            cfs.append((today, cur))

        if cfs:
            first = min(d for d, _ in cfs)
            years = max(0.0, (today - first).days / 365.25)
        else:
            years = 0.0

        out.append({
            "goal_tag":   tag,
            "count":      count_by_goal[tag],
            "invested":   invested,
            "current":    cur,
            "gain":       cur - invested,
            "gain_pct":   round((cur - invested) / invested * 100, 2) if invested > 0 else None,
            "years":      round(years, 2),
            "xirr_pct":   xirr(cfs) if invested > 0 else None,
            "cagr_pct":   cagr(invested, cur, years) if invested > 0 and years > 0.08 else None,
        })

    out.sort(key=lambda x: x["current"], reverse=True)
    return out
