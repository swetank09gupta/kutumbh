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

# {(account_id, isin)} that WIN the owner's de-duplicated current-holdings view. Several accounts
# can show the same ISIN (demat statement, CDSL CAS, a stale folio statement); only the winner
# counts toward "current", otherwise the same fund is valued twice. Set by compute_owner_returns.
from contextvars import ContextVar
_WINNERS: ContextVar[Optional[set]] = ContextVar("xirr_winners", default=None)


def _only_current(rows, account_id):
    win = _WINNERS.get()
    if win is None:
        return list(rows)
    return [h for h in rows if not h.isin or (str(account_id), h.isin) in win]

# Account types whose transactions feed XIRR (everything else is excluded — bank
# savings, current accounts, etc. — because cash flows there are not investments).
#
# Note: "demat" (CDSL/NSDL CAS) is intentionally EXCLUDED. CDSL statements are
# position snapshots only — no buy/sell cashflows — so they cannot contribute to
# XIRR. Their ISINs overlap with the broker (Zerodha "stocks") accounts which
# already capture the same positions WITH cost basis via contract notes.
# Including CDSL produced a phantom "demat" bucket with current_value but
# invested=0, which inflated displayed AUM and showed an empty Demat XIRR card.
_INVESTING_ACCOUNT_TYPES = {"stocks", "mutual_fund"}

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
        snap = (
            db.query(Holding)
            .filter(Holding.account_id == account.id, Holding.snapshot_date == latest_date)
            .all()
        )
        current_value = sum((h.current_value or ZERO for h in _only_current(snap, account.id)), ZERO)

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


# ── SGB tranche calendar ──────────────────────────────────────────────────────
# Sovereign Gold Bonds have an 8-year tenor with a known issue date per tranche.
# For holdings whose buys aren't traceable to a bank/broker transaction (typical
# when SGBs were subscribed via the bank's SGB application rather than via the
# broker), we synthesize a purchase cashflow at the tranche's issue date using
# the recorded avg_cost_nav × units as the rupee amount.
#
# Source: RBI issuance calendar. Keys = ISIN. Values = (issue_date, redemption_date).
# When an ISIN is not in this table, we fall back to parsing the maturity year
# from the instrument name and assume issue_date = redemption_date − 8 years.
import re as _re_sgb
from datetime import date as _date

# Each entry: (issue_date, redemption_date, issue_price_per_gram_inr).
# Issue prices include the ₹50/gram online subscription discount, sourced from
# the RBI press release for each tranche.
_SGB_TRANCHES: dict[str, tuple[_date, _date, float]] = {
    # 2021-22 Series VII — issued 02-Nov-2021 @ ₹4,761/gm (online price)
    "IN0020210178": (_date(2021, 11, 2),  _date(2029, 11, 2),  4761.0),
    # 2022-23 Series III — issued 27-Dec-2022 @ ₹5,409/gm
    "IN0020220110": (_date(2022, 12, 27), _date(2030, 12, 27), 5409.0),
    # 2023-24 Series II — issued 14-Sep-2023 @ ₹5,873/gm
    "IN0020230069": (_date(2023, 9, 14),  _date(2031, 9, 14),  5873.0),
}


def _sgb_issue_date(isin: str, instrument_name: str) -> Optional[_date]:
    """Best-effort issue date for an SGB tranche."""
    if isin in _SGB_TRANCHES:
        return _SGB_TRANCHES[isin][0]
    # Fallback: parse trailing maturity year from instrument name and subtract 8.
    m = _re_sgb.search(r'\bSGB\b.*?\b(20\d{2})\b', instrument_name or "", _re_sgb.IGNORECASE)
    if m:
        maturity_year = int(m.group(1))
        return _date(maturity_year - 8, 6, 1)
    return None


def _sgb_issue_price(isin: str) -> Optional[float]:
    """Per-unit issue price (₹/gram) for known SGB tranches; None otherwise."""
    if isin in _SGB_TRANCHES:
        return _SGB_TRANCHES[isin][2]
    return None


def _live_sgb_price() -> Optional[float]:
    """Current SGB market value per unit (₹/gram), via metal_prices feed."""
    try:
        from app.engine.metal_prices import get_metal_prices
        p = get_metal_prices() or {}
        v = p.get("sgb_inr_per_unit") or p.get("gold_24k_inr_per_gram")
        return float(v) if v else None
    except Exception as exc:
        logger.debug("Live SGB price unavailable: %s", exc)
        return None


def _synthesize_bond_purchase_cfs(
    db: Session, account: Account, per_bucket: dict, isin_to_type: dict[str, str],
    type_value: dict[str, Decimal],
) -> None:
    """
    For each SGB-style bond holding in this account that has no traced purchase
    cashflows in `per_bucket`, append a synthetic outflow at the tranche's
    issue date computed as units × avg_cost_nav.

    Mutates `per_bucket` in place. No-op for holdings without avg_cost_nav.
    """
    latest = (
        db.query(func.max(Holding.snapshot_date))
        .filter(Holding.account_id == account.id)
        .scalar()
    )
    if not latest:
        return

    bond_rows = _only_current(
        db.query(Holding)
        .filter(Holding.account_id == account.id,
                Holding.snapshot_date == latest,
                Holding.holding_type == "bonds")
        .all(), account.id)
    if not bond_rows:
        return

    # Index existing per-bucket cashflows by approximate ISIN to detect "already
    # traced". Since cashflows don't carry ISIN, we instead check whether the
    # account already has *any* debit txns matching this ISIN.
    isin_has_txn: dict[str, bool] = {}
    txns_with_isin = (
        db.query(Transaction.isin)
        .filter(Transaction.account_id == account.id,
                Transaction.isin.isnot(None),
                Transaction.transaction_type == "debit")
        .all()
    )
    for (isin,) in txns_with_isin:
        if isin:
            isin_has_txn[isin] = True

    live_price = _live_sgb_price()
    for h in bond_rows:
        if not h.isin or isin_has_txn.get(h.isin):
            continue
        issue_dt = _sgb_issue_date(h.isin, h.instrument_name or "")
        if not issue_dt:
            continue
        # Prefer the published RBI issue price; fall back to avg_cost_nav from
        # the holdings row (which can be a current-value placeholder rather than
        # a true purchase price — known issue with Zerodha demat statements).
        unit_cost = _sgb_issue_price(h.isin)
        cost_source = "RBI tranche"
        if unit_cost is None and h.avg_cost_nav and h.avg_cost_nav > 0:
            unit_cost = float(h.avg_cost_nav)
            cost_source = "broker avg_cost"
        if not unit_cost or unit_cost <= 0:
            continue
        units = float(h.units or 0)
        cost  = units * unit_cost
        if cost <= 0:
            continue
        bucket = isin_to_type.get(h.isin, "bonds")
        slot = per_bucket.setdefault(bucket, {"cfs": [], "invested": ZERO})
        slot["cfs"].append((issue_dt, -cost))
        slot["invested"] = slot["invested"] + Decimal(f"{cost:.2f}")
        logger.info(
            "Synthesized SGB purchase: %s units=%s @ ₹%.2f/g (%s) on %s → cost ₹%.0f",
            h.isin, h.units, unit_cost, cost_source, issue_dt, cost,
        )
        # Mark-to-market: override the stored current_value (which equals cost
        # for SGB holdings on Zerodha statements — no real market price column)
        # with the live SGB spot from the metal_prices feed.
        if live_price and live_price > 0:
            new_current = units * live_price
            old_current = type_value.get(bucket, ZERO)
            # Add the delta between live MTM and stored value to the bucket total
            # so the per-class current / total_current reflect reality.
            stored_for_this_holding = float(h.current_value or 0)
            type_value[bucket] = old_current - Decimal(str(stored_for_this_holding)) \
                                              + Decimal(f"{new_current:.2f}")
            logger.info(
                "SGB MTM: %s stored ₹%.0f → live ₹%.0f (×%s @ ₹%.2f)",
                h.isin, stored_for_this_holding, new_current, units, live_price,
            )


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
        rows = _only_current(
            db.query(Holding)
            .filter(Holding.account_id == account.id, Holding.snapshot_date == latest)
            .all(), account.id)
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
        if t.isin:
            if t.isin in isin_to_type:
                bucket = isin_to_type[t.isin]
            elif t.isin.startswith("INF"):
                bucket = "mutual_fund"
            elif t.isin.startswith("IN0"):
                bucket = "bonds"
            else:
                bucket = "equity"
        else:
            bucket = majority
        if t.transaction_type == "debit":
            per_bucket[bucket]["cfs"].append((t.date, -amt))
            per_bucket[bucket]["invested"] += Decimal(str(amt))
        elif t.transaction_type == "credit":
            per_bucket[bucket]["cfs"].append((t.date, amt))

    # Backfill SGB / bond purchases that never landed as transactions
    # (typical for SGBs subscribed via bank SGB-application rather than via
    # the broker). Uses the holding's avg_cost_nav × units at the tranche's
    # known issue date.
    _synthesize_bond_purchase_cfs(db, account, per_bucket, isin_to_type, type_value)

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
    out: dict[str, Decimal] = {}
    for h in _only_current(
            db.query(Holding).filter(Holding.account_id == account_id, Holding.snapshot_date == latest).all(),
            account_id):
        out[h.holding_type or "equity"] = out.get(h.holding_type or "equity", ZERO) + (h.current_value or ZERO)
    return out


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
        from app.engine.networth import real_owners
        owner_ids = [o.id for o in real_owners(db)]
    else:
        owner_ids = [owner_id]

    _WINNERS.set({(str(h.account_id), h.isin) for h in _latest_holdings(db, owner_ids=owner_ids) if h.isin})

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
    # Cost coverage: a class whose holdings include units with no traced purchase (bought before
    # our history, transfers) has terminal value without matching outflows, so XIRR/gain computed
    # from the traced cashflows would be inflated (a wife's fund showed 486%). Such a class
    # reports its value and says why returns are withheld, instead of a wrong number.
    value_by_class: dict[str, float] = defaultdict(float)
    partial_by_class: dict[str, float] = defaultdict(float)
    for h in _latest_holdings(db, owner_ids=owner_ids):
        c = h.holding_type or "equity"
        v = float(h.current_value or 0)
        value_by_class[c] += v
        if getattr(h, "cost_basis_quality", None) == "partial":
            partial_by_class[c] += v

    by_class: list[dict] = []
    total_cfs:      list[tuple[date, float]] = []
    total_invested = ZERO
    total_current  = ZERO
    total_proceeds = 0.0

    for cls in sorted(class_cfs.keys()):
        cfs   = class_cfs[cls]
        inv   = class_invested[cls]
        cur   = class_current[cls]
        years = _years_from_first_cf(cfs, today)
        # Sale / redemption / dividend proceeds = every inflow except the terminal current value.
        proceeds = max(0.0, sum(a for _, a in cfs if a > 0) - float(cur))
        profit   = float(cur) + proceeds - float(inv)          # unrealised + realised
        total_v  = value_by_class.get(cls, 0.0)
        coverage = (1.0 - partial_by_class.get(cls, 0.0) / total_v) if (total_v > 0 and cls in ("equity", "mutual_fund")) else None
        withheld = coverage is not None and coverage < 0.95
        no_cost_basis = (inv <= ZERO)
        excluded = no_cost_basis or withheld
        class_xirr = None if excluded else xirr(cfs)
        by_class.append({
            "class":     cls,
            "count":     class_count[cls],
            "invested":  float(inv),
            "current":   float(cur),
            "proceeds":  round(proceeds, 2),
            # profit counts what was sold as well as what is still held
            "gain":      round(profit, 2) if not excluded else None,
            "gain_pct":  round(profit / float(inv) * 100, 2) if (inv > 0 and not excluded) else None,
            "years":     round(years, 2),
            "xirr_pct":  class_xirr,
            "cagr_pct":  cagr(float(inv), float(cur), years) if (inv > 0 and not excluded) else None,
            "cost_coverage_pct": round(coverage * 100, 1) if coverage is not None else None,
            "excluded_from_total": excluded,
            "exclusion_reason": ("purchase history incomplete for "
                                 f"{100 - coverage * 100:.0f}% of the value — returns withheld" if withheld
                                 else "no cost basis (missing purchase cashflows)" if no_cost_basis
                                 else "XIRR is not meaningful here (positions opened and closed within days); see gain" if class_xirr is None
                                 else None),
        })
        if excluded:
            skipped.append({
                "kind": "class", "name": cls,
                "reason": (f"purchase history incomplete for {100 - coverage * 100:.0f}% of value — "
                           f"current ₹{float(cur):,.0f} excluded from total returns" if withheld
                           else f"no cost basis — current ₹{float(cur):,.0f} excluded from total XIRR"),
            })
            continue
        total_cfs.extend(cfs)
        total_invested += inv
        total_current  += cur
        total_proceeds += proceeds

    total_years = _years_from_first_cf(total_cfs, today)
    total_profit = float(total_current) + total_proceeds - float(total_invested)
    total = {
        "invested":  float(total_invested),
        "current":   float(total_current),
        "proceeds":  round(total_proceeds, 2),
        "gain":      round(total_profit, 2),
        "gain_pct":  round(total_profit / float(total_invested) * 100, 2) if total_invested > 0 else None,
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

        # CAGR: prefer per-unit avg_cost_nav vs current_nav when both are stored.
        # Transaction-based cagr(invested, cur) overcounts cost when some units
        # were sold (invested accumulates all buys but cur is only remaining
        # value) — e.g. Silver ETF after partial redemption reads -60% instead
        # of -20%.  NAV-based CAGR uses the actual average purchase price of
        # the *remaining* units, which is always correct for open positions.
        _nav_cost   = float(h.avg_cost_nav  or 0)
        _nav_cur    = float(h.current_nav   or 0)
        _first_dt   = h.first_buy_date
        # Prefer txn first date when the stored first_buy_date is unreliable
        # (e.g. set to the CAS statement date rather than actual purchase date).
        if cfs:
            _txn_dates = [d for d, _ in cfs if d < today]  # exclude terminal cf
            _txn_first = min(_txn_dates) if _txn_dates else None
            if _first_dt is None or _first_dt >= today:
                _first_dt = _txn_first

        if _nav_cost > 0 and _nav_cur > 0 and _first_dt and _first_dt < today:
            _cagr_years = max(0.0, (today - _first_dt).days / 365.25)
            h_cagr = cagr(_nav_cost, _nav_cur, _cagr_years) if _cagr_years > 0.08 else None
        elif invested > 0 and years > 0.08:
            h_cagr = cagr(invested, cur, years)
        else:
            h_cagr = None

        # Display invested = stored cost of remaining holdings when available
        # (avg_cost_nav × units or invested_value). Transaction sum overcounts
        # when units were partially sold.
        _h_inv = float(h.invested_value or 0)
        if _h_inv <= 0 and _nav_cost > 0 and float(h.units or 0) > 0:
            _h_inv = _nav_cost * float(h.units)
        disp_invested = _h_inv if _h_inv > 0 else (invested if invested > 0 else 0)
        # Some units have no traced purchase: any return would be computed on the traced part only.
        partial = getattr(h, "cost_basis_quality", None) == "partial"
        out.append({
            "isin":       h.isin,
            "name":       h.instrument_name or h.symbol or h.isin,
            "type":       h.holding_type or "equity",
            "owner":      h.owner_id,
            "units":      float(h.units or 0),
            "current":    cur,
            "invested":   None if partial else disp_invested,
            "gain":       None if partial else cur - disp_invested,
            "years":      round(years, 2),
            "xirr_pct":   None if partial else (xirr(cfs) if invested > 0 else None),
            "cagr_pct":   None if partial else h_cagr,
            "goal_tag":   h.goal_tag,
            "cost_quality": getattr(h, "cost_basis_quality", None),
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
    holdings_by_goal: dict[str, list]  = defaultdict(list)
    for h in holdings:
        tag = h.goal_tag or "untagged"
        if h.isin:
            isins_by_goal[tag].add(h.isin)
        cur_by_goal[tag] += float(h.current_value or 0)
        count_by_goal[tag] += 1
        holdings_by_goal[tag].append(h)

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
        isins_with_debit: set[str] = set()
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
                isins_with_debit.add(ti)
            elif t.transaction_type == "credit":
                cfs.append((t.date, amt))

        # SGB purchase synthesis: bond holdings whose buys never landed as txns
        # (SGB subscribed via the bank SGB-application rather than the broker)
        # get a synthetic outflow at the RBI tranche's issue date. Without this,
        # SGB-heavy goals (e.g. Child marriage) show invested=0 and XIRR=None.
        for h in holdings_by_goal[tag]:
            if (h.holding_type or "") != "bonds" or not h.isin:
                continue
            if h.isin in isins_with_debit:
                continue
            issue_dt = _sgb_issue_date(h.isin, h.instrument_name or "")
            if not issue_dt:
                continue
            unit_cost = _sgb_issue_price(h.isin) or float(h.avg_cost_nav or 0)
            units = float(h.units or 0)
            cost  = units * unit_cost
            if cost <= 0:
                continue
            cfs.append((issue_dt, -cost))
            invested += cost
            isins_with_debit.add(h.isin)

        # Cost-basis fallback for non-bond holdings: if a goal-tagged holding has
        # no matched buy cashflow (ISIN not on the txn, or fund-name match
        # failed for non-Coin imports), try broker-reported invested_value, then
        # avg_cost_nav × units. Without these fallbacks, the holding's
        # current_value still flows into the terminal cf but invested stays at
        # 0, inflating per-goal XIRR (the bug that made Retirement read 83%
        # when the underlying equity bucket was 6%).
        excluded_current = 0.0
        for h in holdings_by_goal[tag]:
            if not h.isin or h.isin in isins_with_debit:
                continue
            inv_val = float(getattr(h, "invested_value", 0) or 0)
            if inv_val <= 0:
                # Some imports (Zerodha holdings PDF) leave invested_value NULL
                # but report avg_cost_nav × units, which equals cost basis.
                avg_cost = float(getattr(h, "avg_cost_nav", 0) or 0)
                units    = float(getattr(h, "units", 0) or 0)
                inv_val  = avg_cost * units
            if inv_val <= 0:
                # No cost basis available — exclude this holding's current_value
                # from the goal's terminal cf so it doesn't masquerade as
                # phantom appreciation.
                excluded_current += float(h.current_value or 0)
                continue
            buy_dt = getattr(h, "first_buy_date", None) or today
            cfs.append((buy_dt, -inv_val))
            invested += inv_val
            isins_with_debit.add(h.isin)

        cur = cur_by_goal[tag] - excluded_current
        if cur > 0:
            cfs.append((today, cur))

        if cfs:
            first = min(d for d, _ in cfs)
            years = max(0.0, (today - first).days / 365.25)
        else:
            years = 0.0

        partial_v = sum(float(h.current_value or 0) for h in holdings_by_goal[tag]
                        if getattr(h, "cost_basis_quality", None) == "partial")
        withheld = cur_by_goal[tag] > 0 and partial_v / cur_by_goal[tag] > 0.05
        proceeds = max(0.0, sum(a for _, a in cfs if a > 0) - cur)
        profit   = cur + proceeds - invested
        out.append({
            "goal_tag":   tag,
            "count":      count_by_goal[tag],
            "invested":   invested,
            "current":    cur,
            "proceeds":   round(proceeds, 2),
            "gain":       None if withheld else round(profit, 2),
            "gain_pct":   round(profit / invested * 100, 2) if (invested > 0 and not withheld) else None,
            "years":      round(years, 2),
            "xirr_pct":   xirr(cfs) if (invested > 0 and not withheld) else None,
            "cagr_pct":   cagr(invested, cur, years) if (invested > 0 and years > 0.08 and not withheld) else None,
            "returns_withheld": "purchase history incomplete" if withheld else None,
        })

    out.sort(key=lambda x: x["current"], reverse=True)
    return out
