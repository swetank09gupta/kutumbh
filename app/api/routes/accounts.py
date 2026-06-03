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
            "instrument_name": h.instrument_name,
            "symbol":          h.symbol,
            "isin":            h.isin,
            "units":           float(h.units or 0),
            "avg_cost_nav":    float(h.avg_cost_nav or 0),
            "current_nav":     float(h.current_nav or 0),
            "current_value":   float(h.current_value or 0),
            "invested_value":  float(h.invested_value or 0),
            "gain_loss":       float(h.gain_loss or 0),
            "gain_loss_pct":   float(h.gain_loss_pct or 0),
            "holding_type":    h.holding_type,
            "goal_tag":        h.goal_tag,
            "id":              str(h.id),
        }
        for h in holdings
    ]


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
    if h.isin:
        updated = (
            db.query(Holding)
            .filter(Holding.isin == h.isin)
            .update({"goal_tag": new_tag}, synchronize_session=False)
        )
    else:
        h.goal_tag = new_tag
        updated = 1
    db.commit()
    return {"id": holding_id, "goal_tag": new_tag, "rows_updated": updated}


@router.post("/holdings/auto-tag")
def auto_tag_all_holdings(
    db:       Annotated[Session, Depends(get_db)],
    owner_id: Optional[str] = None,
):
    """
    Auto-assign goal_tag to all untagged holdings using the ISIN map + fund-name rules.
    Holdings that already have a manually-set goal_tag are never overwritten.
    """
    from app.engine.holding_tagger import auto_tag_holdings
    tagged = auto_tag_holdings(db, owner_id)
    return {"tagged": tagged, "message": f"Tagged {tagged} previously untagged holdings."}


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

    inv_types = ("stocks", "mutual_fund")

    # ── Current holdings from the view (deduplicated) ─────────────────────────
    holdings = _latest_holdings(db, owner_ids=ids, account_types=inv_types)

    if not holdings:
        return {
            "portfolio": None,
            "by_type":   {},
            "holdings":  [],
            "closed":    [],
        }

    # ── Fetch all investment accounts — needed to pull full transaction history ─
    # (transactions come from all accounts of that type, not just deduped ones)
    acct_q = db.query(Account).filter(Account.account_type.in_(inv_types))
    if ids:
        acct_q = acct_q.filter(Account.owner_id.in_(ids))
    acct_ids = [a.id for a in acct_q.all()]

    # ── Transactions ──────────────────────────────────────────────────────────
    txns = (
        db.query(Transaction)
        .filter(Transaction.account_id.in_(acct_ids))
        .order_by(Transaction.date)
        .all()
    )
    # Monthly demat PDF transactions store share *quantity* as amount (not ₹).
    # Only tradebook CSV / contract-note transactions carry ₹ values and are
    # valid cash-flow inputs for XIRR.  Demat descriptions always start "DEMAT ".
    txns = [t for t in txns if not (t.description or "").upper().startswith("DEMAT ")]

    # ── Closed holdings ───────────────────────────────────────────────────────
    closed = (
        db.query(ClosedHolding)
        .filter(ClosedHolding.account_id.in_(acct_ids))
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
    portfolio_cfs = build_investment_cashflows(
        [{"date": t.date, "amount": float(t.amount), "transaction_type": t.transaction_type}
         for t in txns],
        total_current_value,
        today,
    )
    portfolio_xirr = xirr(portfolio_cfs)
    portfolio_total_return = total_return_pct(total_invested_value, total_current_value)

    # ── Per-type XIRR ─────────────────────────────────────────────────────────
    # account_type is now a column on the view row — no separate map needed
    acct_type_map = {a.id: a.account_type for a in db.query(Account).filter(Account.id.in_(acct_ids)).all()}
    type_txns: dict[str, list] = defaultdict(list)
    for t in txns:
        atype = acct_type_map.get(t.account_id, "other")
        type_txns[atype].append(t)

    type_current: dict[str, float] = defaultdict(float)
    type_invested: dict[str, float] = defaultdict(float)
    for h in holdings:
        atype = h.account_type or "other"
        type_current[atype]  += float(h.current_value or 0)
        type_invested[atype] += _effective_invested(h)

    by_type: dict[str, dict] = {}
    for atype, t_list in type_txns.items():
        cfs = build_investment_cashflows(
            [{"date": t.date, "amount": float(t.amount),
              "transaction_type": t.transaction_type} for t in t_list],
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
    isin_txns: dict[str, list] = defaultdict(list)
    for t in txns:
        if t.isin:
            isin_txns[t.isin].append(t)

    instrument_returns: list[dict] = []
    for h in holdings:
        inst_xirr = None
        if h.isin and h.isin in isin_txns:
            cfs = build_investment_cashflows(
                [{"date": t.date, "amount": float(t.amount),
                  "transaction_type": t.transaction_type}
                 for t in isin_txns[h.isin]],
                float(h.current_value or 0),
                today,
            )
            inst_xirr = xirr(cfs)

        h_cagr = holding_cagr(
            float(h.avg_cost_nav or 0),
            float(h.current_nav or 0),
            h.first_buy_date,
            today,
        )
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
            "invested_value":   _effective_invested(h),
            "gain_loss":        float(h.gain_loss or 0),
            "gain_loss_pct":    float(h.gain_loss_pct or 0),
            "xirr_pct":         inst_xirr,
            "cagr_pct":         h_cagr,
            "first_buy_date":   str(h.first_buy_date) if h.first_buy_date else None,
            "status":           "open",
        })

    # ── Closed positions ──────────────────────────────────────────────────────
    closed_returns: list[dict] = []
    for ch in closed:
        ch_xirr = None
        if ch.isin and ch.isin in isin_txns:
            cfs = build_investment_cashflows(
                [{"date": t.date, "amount": float(t.amount),
                  "transaction_type": t.transaction_type}
                 for t in isin_txns[ch.isin]],
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
