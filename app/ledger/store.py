"""
Ledger persistence: turn parsed trades / demat lines into rows, idempotently.

Every row carries a natural_key; re-processing the same file (or an overlapping
statement) hits ON CONFLICT DO NOTHING, so the ledger cannot grow duplicates.
"""
from __future__ import annotations

import logging
import uuid
from decimal import Decimal

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.etl.dedup import _normalize_amount
from app.models.schemas import DematMovement, InstrumentTrade, StatementBalance

logger = logging.getLogger(__name__)

TRADE_KINDS = {"contract_note", "coin", "tradebook"}


def holding_type_for(isin: str | None) -> str | None:
    if not isin:
        return None
    if isin.startswith("INF"):
        return "mutual_fund"       # MF units and exchange-traded funds / ETFs
    if isin.startswith("IN0"):
        return "bonds"
    return "equity"


def trade_key(owner_id: str, kind: str, *, isin, symbol, ref, trade_date, side, qty, price, amount) -> str:
    """Deterministic identity of one trade within its source."""
    who = isin or symbol or ref or "?"
    q = _normalize_amount(qty)
    if kind == "tradebook" and ref:
        return f"tb|{owner_id}|{ref}"
    if kind == "coin":
        return f"coin|{owner_id}|{who}|{trade_date}|{side}|{q}|{_normalize_amount(amount)}|{ref or ''}"
    return f"cn|{owner_id}|{who}|{trade_date}|{side}|{q}|{_normalize_amount(price) if price is not None else ''}|{ref or ''}"


def movement_key(owner_id: str, isin: str, move_date, qty_in, qty_out, running, desc: str) -> str:
    d = "".join(ch for ch in (desc or "").lower() if ch.isalnum())[:60]
    return (f"dm|{owner_id}|{isin}|{move_date}|{_normalize_amount(qty_in)}|"
            f"{_normalize_amount(qty_out)}|{_normalize_amount(running) if running is not None else ''}|{d}")


def record_parse_result(db: Session, result, owner_id: str, account, txn_fingerprints: list[str]) -> dict[str, int]:
    """
    Write ledger rows for the trades and demat lines in `result`.

    txn_fingerprints[i] is the fingerprint of result.transactions[i] in the
    transactions table, so each trade stays linked to its cashflow row.
    Returns {"trades": n, "movements": n} of rows actually inserted.
    """
    trade_rows: list[dict] = []
    move_rows: list[dict] = []
    from app.ledger.aliases import canonical, load_aliases
    aliases = load_aliases(db)

    for pt, fp in zip(result.transactions, txn_fingerprints):
        if pt.kind in TRADE_KINDS:
            pt.isin = canonical(owner_id, pt.isin, aliases)
        if pt.quantity is None or pt.quantity <= 0 or not pt.kind:
            continue

        if pt.kind in ("demat", "cdsl_alert"):
            if not pt.isin:
                continue
            qty_in = pt.quantity if pt.transaction_type == "credit" else Decimal(0)
            qty_out = pt.quantity if pt.transaction_type == "debit" else Decimal(0)
            move_rows.append({
                "id": uuid.uuid4(), "owner_id": owner_id, "account_id": account.id,
                "isin": pt.isin, "symbol": pt.counterparty, "move_date": pt.date,
                "qty_in": qty_in, "qty_out": qty_out, "running_qty": pt.running_qty,
                "seq": pt.seq, "statement_date": result.statement_date,
                "description": (pt.raw_description or "")[:300],
                "source_file": result.source_file,
                "natural_key": movement_key(owner_id, pt.isin, pt.date, qty_in, qty_out,
                                            pt.running_qty, pt.raw_description or ""),
            })
        elif pt.kind in TRADE_KINDS:
            side = "B" if pt.transaction_type == "debit" else "S"
            name = pt.counterparty or pt.raw_description
            trade_rows.append({
                "id": uuid.uuid4(), "owner_id": owner_id, "account_id": account.id,
                "isin": pt.isin or None, "symbol": pt.counterparty,
                "instrument_name": (name or "")[:200] or None,
                "holding_type": holding_type_for(pt.isin),
                "trade_date": pt.date, "settle_date": pt.settle_date, "side": side, "quantity": pt.quantity,
                "price": pt.price, "amount": pt.amount,
                "source": pt.kind, "source_ref": (pt.reference_number or None),
                "source_file": result.source_file,
                "natural_key": trade_key(owner_id, pt.kind, isin=pt.isin, symbol=pt.counterparty,
                                         ref=pt.reference_number, trade_date=pt.date, side=side,
                                         qty=pt.quantity, price=pt.price, amount=pt.amount),
                "txn_fingerprint": fp,
            })

    inserted = {"trades": 0, "movements": 0}
    if trade_rows:
        stmt = pg_insert(InstrumentTrade.__table__).values(_dedupe_batch(trade_rows))
        stmt = stmt.on_conflict_do_nothing(index_elements=["natural_key"])
        inserted["trades"] = db.execute(stmt).rowcount
    if move_rows:
        stmt = pg_insert(DematMovement.__table__).values(_dedupe_batch(move_rows))
        stmt = stmt.on_conflict_do_nothing(index_elements=["natural_key"])
        inserted["movements"] = db.execute(stmt).rowcount
    return inserted


def _dedupe_batch(rows: list[dict]) -> list[dict]:
    """ON CONFLICT cannot see two identical keys inside one INSERT; drop repeats up front."""
    seen: set[str] = set()
    out = []
    for r in rows:
        if r["natural_key"] in seen:
            continue
        seen.add(r["natural_key"])
        out.append(r)
    return out


_FAMILIES = {"zerodha": "zerodha", "cdsl": "cdsl"}


def record_statement_balances(db: Session, result, owner_id: str) -> int:
    """
    Store the closing balances of a monthly demat statement under its own family.
    Only statements with a real as-on date count (a CSV export dated "today" is not
    a month-end). Idempotent: re-processing refreshes the same rows.
    """
    family = _FAMILIES.get((result.institution or "").lower())
    if not family or not result.statement_date or not result.complete_holdings_snapshot:
        return 0
    totals: dict[str, dict] = {}
    for h in result.holdings:
        if not h.isin:
            continue
        isin = h.isin                                # the depository's own ISIN: never aliased
        row = totals.setdefault(isin, {"qty": Decimal(0), "name": h.instrument_name, "rate": h.current_nav})
        row["qty"] += h.units or Decimal(0)
    rows = [{"id": uuid.uuid4(), "owner_id": owner_id, "family": family, "as_on": result.statement_date,
             "isin": "", "qty": Decimal(0), "name": None, "rate": None, "source_file": result.source_file}]
    rows += [{"id": uuid.uuid4(), "owner_id": owner_id, "family": family, "as_on": result.statement_date,
              "isin": isin, "qty": v["qty"], "name": (v["name"] or "")[:200] or None, "rate": v["rate"],
              "source_file": result.source_file} for isin, v in totals.items()]
    stmt = pg_insert(StatementBalance.__table__).values(rows)
    stmt = stmt.on_conflict_do_update(
        constraint="uq_statement_balance",
        set_={"qty": stmt.excluded.qty, "name": stmt.excluded.name, "rate": stmt.excluded.rate,
              "source_file": stmt.excluded.source_file})
    return db.execute(stmt).rowcount
