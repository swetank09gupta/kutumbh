"""
Home-loan outstanding principal, from the lender's own figure.

Anchor  : the newest part-prepayment confirmation ("principal outstanding after this
          prepayment will be Rs. N as on <date>", loan_anchors).
Roll    : every month an EMI was debited after the anchor reduces principal by EMI - principal x rate / 12
          (monthly-rest loan). The EMI applied is the lender's own, as printed in the anchor letter: the amount
          debited can lag a revised EMI by one instalment (Jul and Sep 2026), and HDFC's next letters matched the
          letter EMI to the rupee both times, the debited amount not. The debit proves the month was paid.
          Debits are read from the bank statement rows and the HDFC ACH alerts, one per month.

The result is written to the loan's liability row in Manual Assets, but only when it is newer than
what that row already says — so a figure you type later is never overwritten by an older letter.
"""
from __future__ import annotations

import logging
import re
from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from app.engine.loan_schedule import step
from app.models.schemas import Account, BankAlertRow, LoanAnchor, ManualAsset, Transaction

logger = logging.getLogger(__name__)
_EMI_DESC = re.compile(r"ACH\s*D[-\s].*HDFC\s*BANK|ACHD.*HDFCBANK", re.I)


def _emi_debits(db: Session, owner_id: str, after: date, emi: Decimal,
                upto: date | None = None) -> list[tuple[date, Decimal]]:
    """One EMI per calendar month after `after`: (date, amount) from statements and alerts."""
    lo, hi = emi * Decimal("0.6"), emi * Decimal("1.4")
    found: dict[tuple[int, int], tuple[date, Decimal]] = {}
    q = (db.query(Transaction).join(Account, Account.id == Transaction.account_id)
         .filter(Account.owner_id == owner_id, Account.account_type == "bank", Transaction.date > after,
                 Transaction.transaction_type == "debit", Transaction.amount.between(lo, hi)).all())
    for t in q:
        if _EMI_DESC.search(t.raw_description or ""):
            found.setdefault((t.date.year, t.date.month), (t.date, Decimal(t.amount)))
    for a in db.query(BankAlertRow).filter(BankAlertRow.owner_id == owner_id, BankAlertRow.kind == "ach",
                                           BankAlertRow.direction == "debit", BankAlertRow.txn_date > after,
                                           BankAlertRow.amount.between(lo, hi)).all():
        found.setdefault((a.txn_date.year, a.txn_date.month), (a.txn_date, Decimal(a.amount)))
    return sorted(v for v in found.values() if upto is None or v[0] <= upto)


def roll(db: Session, owner_id: str, a: LoanAnchor, upto: date | None = None) -> tuple[Decimal, date, list[dict]]:
    """Walk one anchor forward over the months an EMI was debited: (principal, date of the last one, instalments)."""
    principal, rate, emi = Decimal(a.principal_after), Decimal(a.rate), Decimal(a.emi)
    last, rows = a.as_on, []
    for d, debited in _emi_debits(db, owner_id, a.as_on, emi, upto):
        interest, repaid, after = step(principal, rate, emi)
        rows.append({"date": d, "emi": emi, "debited": debited, "interest": interest, "principal": repaid,
                     "balance": after, "rate": rate})
        principal, last = after, d
    return principal, last, rows


def outstanding(db: Session, owner_id: str) -> dict | None:
    a = (db.query(LoanAnchor).filter(LoanAnchor.owner_id == owner_id)
         .order_by(LoanAnchor.as_on.desc(), LoanAnchor.principal_after.asc()).first())
    if a is None or not a.emi or not a.rate:
        return None
    principal, last, rows = roll(db, owner_id, a)
    return {"principal": principal.quantize(Decimal("0.01")), "as_of": last, "anchor": a, "emis_applied": len(rows)}


def principal_on(db: Session, owner_id: str, day: date, sanctioned: Decimal | None = None) -> Decimal | None:
    """Principal owed on a past date: the newest anchor on or before it, rolled forward by EMIs up to it.
    Before the first anchor the loan stood at its sanctioned amount (None when that is unknown)."""
    a = (db.query(LoanAnchor).filter(LoanAnchor.owner_id == owner_id, LoanAnchor.as_on <= day)
         .order_by(LoanAnchor.as_on.desc(), LoanAnchor.principal_after.asc()).first())
    if a is None:
        return sanctioned
    if not a.emi or not a.rate:
        return Decimal(a.principal_after)
    return roll(db, owner_id, a, upto=day)[0]


def apply_loan_anchors(db: Session) -> dict:
    out = {}
    for (owner_id,) in db.query(LoanAnchor.owner_id).distinct().all():
        res = outstanding(db, owner_id)
        asset = (db.query(ManualAsset).filter(ManualAsset.owner_id == owner_id, ManualAsset.asset_type == "liability",
                                              ManualAsset.institution == "HDFC").first())
        if res is None or asset is None:
            continue
        m = re.search(r"as of (\d{4}-\d{2}-\d{2})", asset.notes or "")
        known = asset.source_statement_date or (date.fromisoformat(m.group(1)) if m else None)
        if known is not None and res["as_of"] <= known:
            out[owner_id] = {"updated": False, "reason": f"row already dated {known}"}
            continue
        a = res["anchor"]
        asset.current_value = -res["principal"]
        asset.source_statement_date = res["as_of"]
        asset.monthly_payment, asset.interest_rate = a.emi, a.rate
        asset.notes = (f"Outstanding principal as of {res['as_of']} — HDFC prepayment letter of {a.as_on} "
                       f"(₹{a.principal_after:,.0f}), rolled forward {res['emis_applied']} EMI(s). "
                       f"ROI {a.rate}% (floating), loan {a.loan_no}.")
        out[owner_id] = {"updated": True, "principal": float(res["principal"]), "as_of": str(res["as_of"])}
    db.commit()
    if any(v.get("updated") for v in out.values()):
        from app.engine.networth import backfill_snapshots          # history used the old balance
        backfill_snapshots(db, force=True)
    return out
