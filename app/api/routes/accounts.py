"""
/api/accounts — CRUD for accounts + net-worth snapshots.
"""
from __future__ import annotations

from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.engine.holdings_view import latest_holdings as _latest_holdings
from app.models.schemas import (
    Account, ClosedHolding, Holding, NetWorthSnapshot, Owner, Transaction,
)

router = APIRouter(prefix="/api/accounts", tags=["accounts"])


def _owner_ids(db: Session, owner_id: Optional[str]) -> list[str] | None:
    """Return a list of owner_ids to filter on, or None to include all owners."""
    if not owner_id or owner_id == "family":
        return None
    return [owner_id]


@router.get("/")
def list_accounts(
    db:       Annotated[Session, Depends(get_db)],
    owner_id: Optional[str] = None,
):
    q = db.query(Account).filter_by(is_active=True)
    if owner_id:
        q = q.filter_by(owner_id=owner_id)
    return [
        {
            "id":           str(a.id),
            "owner_id":     a.owner_id,
            "institution":  a.institution,
            "account_type": a.account_type,
            "account_ref":  a.account_ref,
            "is_active":    a.is_active,
        }
        for a in q.all()
    ]


@router.get("/networth")
def networth_history(
    db:       Annotated[Session, Depends(get_db)],
    owner_id: str = "family",
    limit:    int = 365,
):
    rows = (
        db.query(NetWorthSnapshot)
        .filter_by(owner_id=owner_id)
        .order_by(NetWorthSnapshot.snapshot_date.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "date":              str(r.snapshot_date),
            "bank_balance":      float(r.bank_balance or 0),
            "equity_value":      float(r.equity_value or 0),
            "mf_value":          float(r.mf_value or 0),
            "ppf_value":         float(r.ppf_value or 0),
            "fd_value":          float(r.fd_value or 0),
            "real_estate_value": float(r.real_estate_value or 0),
            "other_assets":      float(r.other_assets or 0),
            "total_assets":      float(r.total_assets or 0),
            "total_liabilities": float(r.total_liabilities or 0),
            "net_worth":         float(r.net_worth or 0),
        }
        for r in reversed(rows)
    ]


def _cost_known(h) -> bool:
    return (getattr(h, "cost_basis_quality", None) != "partial"
            and h.invested_value is not None and float(h.invested_value) > 0)


@router.get("/holdings")
def latest_holdings(
    db:       Annotated[Session, Depends(get_db)],
    owner_id: Optional[str] = None,
):
    ids = _owner_ids(db, owner_id)
    holdings = _latest_holdings(db, owner_ids=ids)
    return [
        {
            "account_id":      str(h.account_id),
            "snapshot_date":   str(h.snapshot_date),
            "instrument_name": _amfi_name(h.isin) or h.instrument_name,          # the scheme's own name, not the statement's abbreviation
            "symbol":          h.symbol,
            "isin":            h.isin,
            "units":           float(h.units or 0),
            # Cost is only shown when every unit's purchase is traced. Anything else is "unknown",
            # not 0: a 0 here used to render as "+0 / 0.0%" next to a real value.
            "avg_cost_nav":    float(h.avg_cost_nav) if _cost_known(h) else None,
            "current_nav":     float(h.current_nav or 0),
            "current_value":   float(h.current_value or 0),
            "invested_value":  float(h.invested_value) if _cost_known(h) else None,
            "gain_loss":       float(h.gain_loss) if _cost_known(h) and h.gain_loss is not None else None,
            "gain_loss_pct":   float(h.gain_loss_pct) if _cost_known(h) and h.gain_loss_pct is not None else None,
            "cost_basis_quality": getattr(h, "cost_basis_quality", None),
            "price_source":    getattr(h, "price_source", None),
            "holding_type":    h.holding_type,
            "goal_tag":        h.goal_tag,
            "goal_tag_source": getattr(h, "goal_tag_source", None),
            "id":              str(h.id),
        }
        for h in holdings
    ]


def _amfi_name(isin):
    from app.engine.amfi_isin import scheme_name
    return scheme_name(isin) if isin else None


class GoalTagUpdate(BaseModel):
    goal_tag: str | None = None


@router.patch("/holdings/{holding_id}/goal-tag")
def set_holding_goal_tag(
    holding_id: str,
    body: GoalTagUpdate,
    db: Annotated[Session, Depends(get_db)],
):
    """Tag a holding to a specific goal. If the holding has an ISIN, all rows
    with the same ISIN are updated so the tag is consistent across all snapshot dates."""
    h = db.query(Holding).filter_by(id=holding_id).first()
    if not h:
        raise HTTPException(404, "Holding not found")
    new_tag = body.goal_tag or None
    source = "manual" if new_tag else None                  # a tag you set is never moved; clearing it hands the holding back to the matcher
    if h.isin:
        updated = (
            db.query(Holding)
            .filter(Holding.isin == h.isin)
            .update({"goal_tag": new_tag, "goal_tag_source": source}, synchronize_session=False)
        )
    else:
        h.goal_tag, h.goal_tag_source = new_tag, source
        updated = 1
    db.commit()
    if new_tag is None:
        try:
            from app.planning import matching
            matching.run(db, only_untagged=True)
            from app.engine.goals import refresh_goals
            refresh_goals(db)
            new_tag = db.query(Holding.goal_tag).filter_by(id=holding_id).scalar()
        except Exception:
            pass
    return {"id": holding_id, "goal_tag": new_tag, "goal_tag_source": "auto" if (new_tag and not source) else source, "rows_updated": updated}


@router.post("/holdings/auto-tag")
def auto_tag_all_holdings(
    db:       Annotated[Session, Depends(get_db)],
    owner_id: Optional[str] = None,
    dry_run:  bool = False,
):
    """
    Match holdings to goals (app/planning/matching.py): untagged ones, and ones the matcher or the old rule tagged, are re-checked against each
    goal's horizon and funding gap. Tags you set yourself are never moved. `dry_run` lists the moves without writing them.
    """
    from app.planning import matching
    out = matching.run(db, owner_id, apply=not dry_run)
    if out["moves"] and not dry_run:
        from app.engine.goals import refresh_goals
        refresh_goals(db)                                   # corpus and SIP per goal follow the new tags at once
    return {"tagged": len(out["moves"]), "moves": out["moves"], "goals": out["goals"], "applied": out["applied"],
            "message": f"{'Would move' if dry_run else 'Moved'} {len(out['moves'])} holding(s)."}


# ─────────────────────────────────────────────────────────────────────────────
# Returns / XIRR endpoint
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/returns")
def portfolio_returns(
    db:       Annotated[Session, Depends(get_db)],
    owner_id: Optional[str] = None,
):
    """
    Computes XIRR / CAGR for:
      - Overall investment portfolio (all equity + MF transactions, incl. past exits)
      - Per holding-type (equity, mutual_fund)
      - Per instrument (CAGR from avg_cost_nav + first_buy_date for current holdings;
        transaction-level XIRR for instruments with isin on transactions)
      - Closed positions (instruments that were fully exited)

    Cash-flow convention:
      debit  = money out (buy)   → negative
      credit = money in (sell)   → positive
      terminal = today's portfolio value → positive
    """
    from datetime import date
    from collections import defaultdict
    from app.engine.xirr_engine import (
        build_investment_cashflows, cagr, holding_cagr, total_return_pct, xirr,
    )

    today = date.today()
    ids   = _owner_ids(db, owner_id)

    # Holdings come from multiple account types: 'stocks', 'mutual_fund' (Zerodha),
    # 'demat' (CDSL CAS), 'cams', 'kfintech', 'cdsl'.  Fetch all and filter by
    # holding_type so nothing is silently excluded.
    inv_holding_types = {"stocks", "equity", "mutual_fund", "bonds"}

    holdings_all = _latest_holdings(db, owner_ids=ids)
    holdings = [h for h in holdings_all if (h.holding_type or "") in inv_holding_types]

    if not holdings:
        return {
            "portfolio": None,
            "by_type":   {},
            "holdings":  [],
            "closed":    [],
        }

    # ── Fetch ALL accounts for this owner set — ISIN-tagged SIP transactions
    # arrive via bank accounts, not just investment-type accounts.
    acct_q = db.query(Account)
    if ids:
        acct_q = acct_q.filter(Account.owner_id.in_(ids))
    all_acct_ids = [a.id for a in acct_q.all()]

    # ── Transactions ──────────────────────────────────────────────────────────
    txns = (
        db.query(Transaction)
        .filter(Transaction.account_id.in_(all_acct_ids))
        .order_by(Transaction.date)
        .all()
    )
    # Monthly demat PDF transactions store share *quantity* as amount (not ₹).
    # Only tradebook CSV / contract-note transactions carry ₹ values and are
    # valid cash-flow inputs for XIRR.  Demat descriptions always start "DEMAT ".
    txns = [t for t in txns if not (t.description or "").upper().startswith("DEMAT ")]

    # Restrict portfolio-level cashflows to investment-type accounts only
    # (bank account debits/credits unrelated to investments would skew the aggregate).
    inv_acct_types = {"stocks", "mutual_fund", "demat", "cams", "kfintech", "cdsl", "zerodha"}
    inv_acct_ids = {
        a.id for a in db.query(Account)
        .filter(Account.id.in_(all_acct_ids), Account.account_type.in_(inv_acct_types)).all()
    }
    inv_txns = [t for t in txns if t.account_id in inv_acct_ids]

    # ── Closed holdings ───────────────────────────────────────────────────────
    closed = (
        db.query(ClosedHolding)
        .filter(ClosedHolding.account_id.in_(all_acct_ids))
        .order_by(ClosedHolding.close_date.desc())
        .all()
    )

    # invested_value is sometimes missing from statements (avg_cost not in PDF).
    # Fall back to deriving it as current_value - gain_loss when the stored value is 0.
    def _effective_invested(h) -> float:
        iv = float(h.invested_value or 0)
        if iv == 0 and h.gain_loss is not None and h.current_value is not None:
            iv = max(0.0, float(h.current_value) - float(h.gain_loss))
        return iv

    total_current_value  = sum(float(h.current_value or 0) for h in holdings)
    total_invested_value = sum(_effective_invested(h) for h in holdings)

    # ── Portfolio-level XIRR ──────────────────────────────────────────────────
    # Build isin→owner lookup so we can detect coverage gaps below (shared with
    # per-instrument block; built early here to avoid duplication).
    _acct_owner_map_early = {a.id: a.owner_id for a in db.query(Account).filter(Account.id.in_(all_acct_ids)).all()}
    _isin_owner_covered: set[tuple] = set()
    for t in inv_txns:
        if t.isin:
            _isin_owner_covered.add((t.isin, _acct_owner_map_early.get(t.account_id, "")))

    # For holdings with no matching investment-account transactions (e.g. SGBs
    # bought via demat subscription), inject a synthetic outflow so the XIRR
    # denominator includes those investments rather than treating them as free gains.
    synthetic_outflows: list[dict] = []
    for h in holdings:
        if h.isin and (h.isin, h.owner_id) not in _isin_owner_covered:
            eff_inv = _effective_invested(h)
            if eff_inv > 0 and h.first_buy_date:
                synthetic_outflows.append({
                    "date": h.first_buy_date,
                    "amount": eff_inv,
                    "transaction_type": "debit",
                })

    all_portfolio_flows = (
        [{"date": t.date, "amount": float(t.amount), "transaction_type": t.transaction_type}
         for t in inv_txns]
        + synthetic_outflows
    )
    portfolio_cfs = build_investment_cashflows(all_portfolio_flows, total_current_value, today)
    portfolio_xirr = xirr(portfolio_cfs)
    portfolio_total_return = total_return_pct(total_invested_value, total_current_value)

    # ── Per-type XIRR ─────────────────────────────────────────────────────────
    # Group by holding_type (not account_type) for consistent bucketing.
    type_txns: dict[str, list] = defaultdict(list)
    for t in inv_txns:
        # Use holding_type of the matched ISIN as bucket key, fallback to account_type.
        atype = t.account_type if hasattr(t, "account_type") else "other"
        type_txns[atype].append(t)

    type_current: dict[str, float] = defaultdict(float)
    type_invested: dict[str, float] = defaultdict(float)
    for h in holdings:
        atype = h.holding_type or "other"
        type_current[atype]  += float(h.current_value or 0)
        type_invested[atype] += _effective_invested(h)

    by_type: dict[str, dict] = {}
    for atype in type_current:
        atype_txns = [
            t for t in inv_txns
            if any(hh.holding_type == atype for hh in holdings if hh.isin and t.isin == hh.isin)
        ]
        cfs = build_investment_cashflows(
            [{"date": t.date, "amount": float(t.amount),
              "transaction_type": t.transaction_type} for t in atype_txns],
            type_current[atype],
            today,
        )
        by_type[atype] = {
            "xirr_pct":         xirr(cfs),
            "total_return_pct": total_return_pct(type_invested[atype], type_current[atype]),
            "invested":         round(type_invested[atype], 2),
            "current_value":    round(type_current[atype], 2),
            "gain_loss":        round(type_current[atype] - type_invested[atype], 2),
        }

    # ── Per-instrument XIRR ───────────────────────────────────────────────────
    # Include ISIN-tagged transactions from ALL accounts (SIP debits come via bank accounts).
    # Key by (isin, owner_id) so joint holdings (e.g. same ETF held by two owners)
    # don't mix each other's cashflows.
    isin_owner_txns: dict[tuple, list] = defaultdict(list)
    # Build owner_id map for all accounts
    acct_owner_map = {a.id: a.owner_id for a in db.query(Account).filter(Account.id.in_(all_acct_ids)).all()}
    for t in txns:
        if t.isin:
            oid = acct_owner_map.get(t.account_id, "")
            isin_owner_txns[(t.isin, oid)].append(t)

    instrument_returns: list[dict] = []
    for h in holdings:
        inst_xirr = None
        txn_invested = 0.0
        txn_first_date = None

        if h.isin:
            h_txns = isin_owner_txns.get((h.isin, h.owner_id), [])
            # Compute invested from transaction debits when holding lacks it.
            txn_invested = sum(float(t.amount or 0) for t in h_txns if t.transaction_type == "debit")
            txn_first_date = min((t.date for t in h_txns if t.transaction_type == "debit"), default=None)

            cfs = build_investment_cashflows(
                [{"date": t.date, "amount": float(t.amount),
                  "transaction_type": t.transaction_type}
                 for t in h_txns],
                float(h.current_value or 0),
                today,
            )
            inst_xirr = xirr(cfs)

        # invested_value: prefer stored value, fall back to sum-of-debits from transactions.
        eff_invested = _effective_invested(h) or txn_invested
        # first_buy_date: transaction date is the actual first purchase; h.first_buy_date for
        # CAS/demat holdings is the statement date, not the real purchase date.
        first_buy = txn_first_date or h.first_buy_date

        h_cagr = holding_cagr(
            float(h.avg_cost_nav or 0),
            float(h.current_nav or 0),
            first_buy,
            today,
        )
        # CAGR fallback: if avg_cost_nav is missing, use invested/units as avg cost.
        if h_cagr is None and eff_invested > 0 and float(h.units or 0) > 0 and first_buy:
            implied_nav = eff_invested / float(h.units)
            h_cagr = holding_cagr(implied_nav, float(h.current_nav or 0), first_buy, today)

        gain = float(h.current_value or 0) - eff_invested
        gain_pct = (gain / eff_invested * 100) if eff_invested > 0 else float(h.gain_loss_pct or 0)

        instrument_returns.append({
            "isin":             h.isin,
            "instrument_name":  h.instrument_name,
            "symbol":           h.symbol,
            "holding_type":     h.holding_type,
            "goal_tag":         h.goal_tag,
            "units":            float(h.units or 0),
            "avg_cost_nav":     float(h.avg_cost_nav or 0),
            "current_nav":      float(h.current_nav or 0),
            "current_value":    float(h.current_value or 0),
            "invested_value":   round(eff_invested, 2),
            "gain_loss":        round(gain, 2),
            "gain_loss_pct":    round(gain_pct, 2),
            "xirr_pct":         inst_xirr,
            "cagr_pct":         h_cagr,
            "first_buy_date":   str(first_buy) if first_buy else None,
            "status":           "open",
        })

    # ── Closed positions ──────────────────────────────────────────────────────
    closed_returns: list[dict] = []
    for ch in closed:
        ch_xirr = None
        ch_owner = acct_owner_map.get(ch.account_id, "")
        ch_txns = isin_owner_txns.get((ch.isin, ch_owner), []) if ch.isin else []
        if ch_txns:
            cfs = build_investment_cashflows(
                [{"date": t.date, "amount": float(t.amount),
                  "transaction_type": t.transaction_type}
                 for t in ch_txns],
                0.0,
                ch.close_date,
            )
            if ch.close_value:
                cfs.append((ch.close_date, float(ch.close_value)))
            ch_xirr = xirr(cfs)

        ch_cagr = None
        if ch.avg_cost_nav and ch.close_nav and ch.first_seen_date and ch.close_date:
            yrs = (ch.close_date - ch.first_seen_date).days / 365.25
            ch_cagr = cagr(float(ch.avg_cost_nav), float(ch.close_nav), yrs)

        closed_returns.append({
            "isin":             ch.isin,
            "instrument_name":  ch.instrument_name,
            "symbol":           ch.symbol,
            "holding_type":     ch.holding_type,
            "units":            float(ch.units or 0),
            "avg_cost_nav":     float(ch.avg_cost_nav or 0),
            "close_nav":        float(ch.close_nav or 0),
            "invested_value":   float(ch.invested_value or 0),
            "close_value":      float(ch.close_value or 0),
            "gain_loss":        round(float(ch.close_value or 0) - float(ch.invested_value or 0), 2),
            "xirr_pct":         ch_xirr,
            "cagr_pct":         ch_cagr,
            "close_date":       str(ch.close_date),
            "first_seen_date":  str(ch.first_seen_date) if ch.first_seen_date else None,
            "status":           "closed",
        })

    return {
        "portfolio": {
            "xirr_pct":         portfolio_xirr,
            "total_return_pct": portfolio_total_return,
            "invested":         round(total_invested_value, 2),
            "current_value":    round(total_current_value, 2),
            "gain_loss":        round(total_current_value - total_invested_value, 2),
        },
        "by_type":  by_type,
        "holdings": instrument_returns,
        "closed":   closed_returns,
    }
