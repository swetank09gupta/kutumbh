"""
Normaliser — takes a ParseResult and persists it to PostgreSQL.
Handles account upsert, transaction dedup, and holdings snapshot upsert.
"""
from __future__ import annotations

import logging
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.etl.categorizer import categorise
from app.etl.dedup import _normalize_ref, equity_demat_fingerprint, transaction_fingerprint
from app.ingestion.parsers.base import ParseResult
from app.models.schemas import (
    Account, Balance, ClosedHolding, Holding, ManualAsset, Owner, Transaction,
)

logger = logging.getLogger(__name__)


def _merge_duplicate_holdings(holdings: list) -> list:
    """
    Aggregate holdings that share the same ISIN/symbol key (e.g. two lots of
    the same stock in one statement).  Units and values are summed; cost/nav
    figures become weighted averages.  The constraint uq_holding enforces one
    row per (account, date, isin), so we must collapse duplicates before
    writing to the DB.
    """
    from decimal import Decimal
    from app.ingestion.parsers.base import ParsedHolding

    seen: dict[str, ParsedHolding] = {}
    for ph in holdings:
        key = ph.isin or ph.symbol or ph.instrument_name[:12]
        if key not in seen:
            seen[key] = ph
            continue

        existing = seen[key]
        old_units = existing.units or Decimal(0)
        new_units = ph.units or Decimal(0)
        total_units = old_units + new_units

        def wavg(a, b):
            """Weighted average of two per-unit figures; None if both absent."""
            if a is None and b is None:
                return None
            a = a or Decimal(0)
            b = b or Decimal(0)
            if total_units == 0:
                return a
            return (a * old_units + b * new_units) / total_units

        existing.units         = total_units
        existing.current_value = (existing.current_value or Decimal(0)) + (ph.current_value or Decimal(0))
        existing.invested_value = (
            None if existing.invested_value is None and ph.invested_value is None
            else (existing.invested_value or Decimal(0)) + (ph.invested_value or Decimal(0))
        )
        existing.gain_loss = (
            None if existing.gain_loss is None and ph.gain_loss is None
            else (existing.gain_loss or Decimal(0)) + (ph.gain_loss or Decimal(0))
        )
        existing.avg_cost_nav = wavg(existing.avg_cost_nav, ph.avg_cost_nav)
        existing.current_nav  = wavg(existing.current_nav,  ph.current_nav)
        # gain_loss_pct: recompute from merged gain_loss / invested_value
        if existing.gain_loss is not None and existing.invested_value:
            existing.gain_loss_pct = (existing.gain_loss / existing.invested_value) * 100
        else:
            existing.gain_loss_pct = None

        logger.debug(
            "Merged duplicate holding %s: %.3f + %.3f = %.3f units",
            key, float(old_units), float(new_units), float(total_units),
        )

    return list(seen.values())


# Higher number = more authoritative source for equity holdings.
# CDSL is the depository itself (ground truth); Zerodha is just a broker view.
_HOLDING_PRIORITY: dict[str, int] = {
    "cdsl":          10,
    "cams/kfintech": 10,
    "cams":          10,
    "kfintech":      10,
    "zerodha":        5,
}

# Institutions whose equity holdings are sourced from a demat depository.
# Used to decide whether to apply cross-account ISIN dedup.
_DEMAT_INSTITUTIONS = {"cdsl", "zerodha"}


# ─────────────────────────────────────────────────────────────────────────────
def _upsert_owner(db: Session, owner_id: str) -> None:
    """Ensure owner row exists."""
    if not db.get(Owner, owner_id):
        db.add(Owner(id=owner_id, name=owner_id.capitalize()))
        db.flush()


def _find_owner_holding(
    db: Session, owner_id: str, snapshot_date: date, isin: str
) -> Holding | None:
    """Return an existing holding for this owner+date+isin across ANY account."""
    return (
        db.query(Holding)
        .join(Account, Holding.account_id == Account.id)
        .filter(
            Account.owner_id   == owner_id,
            Holding.snapshot_date == snapshot_date,
            Holding.isin       == isin,
        )
        .first()
    )


def _get_or_create_account(
    db: Session, owner_id: str, institution: str, account_type: str, account_ref: str | None
) -> Account:
    ref = account_ref or "unknown"

    # Exact match first (covers both real refs and "unknown").
    acc = (
        db.query(Account)
        .filter_by(owner_id=owner_id, institution=institution,
                   account_type=account_type, account_ref=ref)
        .first()
    )
    if acc:
        return acc

    # When the parser couldn't extract an account number (ref=="unknown"),
    # prefer an existing account for the same (owner, institution, type)
    # with a REAL reference number over creating a brand-new "unknown" row.
    # This prevents a fresh UUID being minted each reprocess, which would
    # cause fingerprint mismatches and re-insert transactions as duplicates.
    if ref == "unknown":
        real_acc = (
            db.query(Account)
            .filter(
                Account.owner_id    == owner_id,
                Account.institution == institution,
                Account.account_type == account_type,
                Account.account_ref  != "unknown",
            )
            .order_by(Account.account_ref)   # deterministic choice
            .first()
        )
        if real_acc:
            logger.debug(
                "Reusing canonical account %s for %s/%s/%s (no ref in statement)",
                real_acc.account_ref, owner_id, institution, account_type,
            )
            return real_acc

    acc = Account(owner_id=owner_id, institution=institution,
                  account_type=account_type, account_ref=ref)
    db.add(acc)
    db.flush()
    logger.info("Created account: %s / %s / %s / %s", owner_id, institution, account_type, ref)
    return acc


# ─────────────────────────────────────────────────────────────────────────────
def persist_parse_result(result: ParseResult, owner_id: str, db: Session) -> dict[str, int]:
    """
    Persist a ParseResult into the DB. Returns counts of inserted rows.
    """
    for w in result.parse_warnings:
        logger.warning("PARSE WARNING [%s / %s]: %s", owner_id, result.source_file, w)

    _upsert_owner(db, owner_id)
    account = _get_or_create_account(
        db, owner_id, result.institution, result.account_type, result.account_ref
    )
    account_id_str = str(account.id)

    txn_count     = 0
    holding_count = 0

    inst_key   = result.institution.lower()
    is_demat   = inst_key in _DEMAT_INSTITUTIONS
    my_priority = _HOLDING_PRIORITY.get(inst_key, 1)

    # ── Transactions ──────────────────────────────────────────────────────────
    # Build all transaction rows first, then use ON CONFLICT DO NOTHING so
    # duplicate fingerprints from re-processing the same file are silently skipped.
    # For equity demat transactions (isin set), use an owner-scoped fingerprint so
    # that the same ISIN movement from CDSL and Zerodha doesn't create two rows.
    txn_rows: list[dict] = []
    txn_fps:  list[str]  = []      # aligned with result.transactions, for ledger linkage
    import uuid
    from app.ledger.aliases import canonical, load_aliases
    from app.ledger.store import TRADE_KINDS
    _aliases = load_aliases(db)
    _balances_seen: dict[str, list] = {}           # base fingerprint → running balances of the identical rows met so far
    for pt in result.transactions:
        if pt.kind in TRADE_KINDS:                  # learned ISIN aliases fix trade rows only
            pt.isin = canonical(owner_id, pt.isin, _aliases)
        if pt.kind == "cdsl_alert":                 # quantity-only evidence: ledger movement, never a cashflow
            txn_fps.append(None)
            continue
        # Use the owner-scoped equity_demat_fingerprint for any ISIN-tagged
        # row whose description starts with "DEMAT". This covers both CDSL CAS
        # statements (is_demat=True) AND Zerodha monthly holding statements
        # (is_demat=False) which emit "DEMAT CREDIT N.NNN INSTRUMENT_NAME"
        # snapshot rows. The instrument name in the description varies between
        # monthly PDFs (short symbol vs full name), making description-based
        # fingerprinting unstable. The ISIN + qty + date combination is stable.
        is_demat_row = pt.isin and (
            is_demat or (pt.description or "").upper().startswith("DEMAT ")
        )
        # Truncate description before fingerprinting so the stored value and the
        # fingerprint input are always identical.  Previously the fingerprint was
        # computed from the untruncated description, while storage truncated it to
        # 490 chars; maintenance re-computed from the stored value and got a
        # different hash, causing the same transaction to be re-inserted every sync.
        desc     = (pt.description     or "")[:490]
        raw_desc = (pt.raw_description or "")[:490]
        if len(pt.description or "") > 490:
            logger.debug("Truncated long description (%d chars) from %s",
                         len(pt.description), result.source_file)

        if is_demat_row:
            # Quantize to 2dp before fingerprinting: the DB column is NUMERIC(15,2)
            # so the stored amount will be rounded, and maintenance recomputes from
            # stored data.  Using the same precision here prevents a fingerprint
            # mismatch on the next sync (which would re-insert the row as a duplicate).
            fp_qty = Decimal(str(pt.amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            fp = equity_demat_fingerprint(
                owner_id, pt.isin, pt.date, fp_qty, pt.transaction_type
            )
        else:
            ordinal = 1
            if pt.balance_after is not None and not _normalize_ref(pt.reference_number):
                # identical reference-less rows (two ₹1,016 sweep credits the same day) are separate transactions
                # exactly when the statement's running balance differs; the same balance means one row listed twice
                base = transaction_fingerprint(account_id_str, pt.date, pt.amount, pt.transaction_type, desc, None)
                seen = _balances_seen.setdefault(base, [])
                bal = Decimal(str(pt.balance_after))
                if bal not in seen:
                    seen.append(bal)
                ordinal = seen.index(bal) + 1
            fp = transaction_fingerprint(
                account_id_str, pt.date, pt.amount, pt.transaction_type,
                desc, pt.reference_number, ordinal,
            )

        cat = categorise(desc)
        txn_fps.append(fp)
        txn_rows.append({
            "id":               uuid.uuid4(),
            "account_id":       account.id,
            "date":             pt.date,
            "value_date":       pt.value_date,
            "amount":           pt.amount,
            "transaction_type": pt.transaction_type,
            "description":      desc,
            "raw_description":  raw_desc,
            "category":         cat.category,
            "subcategory":      cat.subcategory,
            "counterparty":     pt.counterparty,
            "reference_number": pt.reference_number,
            "balance_after":    pt.balance_after,
            "isin":             pt.isin or None,
            "source_file":      result.source_file,
            "fingerprint":      fp,
        })

    if txn_rows:
        stmt = pg_insert(Transaction.__table__).values(txn_rows)
        stmt = stmt.on_conflict_do_nothing(index_elements=["fingerprint"])
        result_proxy = db.execute(stmt)
        txn_count = result_proxy.rowcount  # rows actually inserted (not skipped)

    # ── Instrument ledger (trades + demat movements) ──────────────────────────
    # Idempotent via natural keys; see docs/adr/0023-instrument-ledger.md.
    from app.ledger.store import record_parse_result
    ledger_counts = record_parse_result(db, result, owner_id, account, txn_fps)
    from app.ledger.store import record_statement_balances
    record_statement_balances(db, result, owner_id)

    # ── Holdings ──────────────────────────────────────────────────────────────
    if result.holdings:
        result.holdings[:] = _merge_duplicate_holdings(result.holdings)
        snap_date = result.statement_date or date.today()

        # For complete snapshots (monthly demat / transaction-with-holding statements),
        # archive any positions that are about to be removed (sold instruments) into
        # closed_holdings, then delete them so stale data doesn't inflate the portfolio.
        if result.complete_holdings_snapshot:
            incoming_isins = {
                (ph.isin or ph.symbol or ph.instrument_name[:12])
                for ph in result.holdings
            }
            existing_today = (
                db.query(Holding)
                .filter(Holding.account_id == account.id,
                        Holding.snapshot_date == snap_date)
                .all()
            )
            for h in existing_today:
                h_key = h.isin or h.symbol or (h.instrument_name or "")[:12]
                if h_key not in incoming_isins:
                    # Position has been fully exited — archive it
                    db.add(ClosedHolding(
                        account_id=account.id,
                        isin=h.isin,
                        symbol=h.symbol,
                        instrument_name=h.instrument_name,
                        holding_type=h.holding_type,
                        units=h.units,
                        avg_cost_nav=h.avg_cost_nav,
                        invested_value=h.invested_value,
                        close_nav=h.current_nav,
                        close_value=h.current_value,
                        close_date=snap_date,
                        first_seen_date=h.first_buy_date or h.snapshot_date,
                        goal_tag=h.goal_tag,
                        source_file=result.source_file,
                    ))
            deleted = (
                db.query(Holding)
                .filter(Holding.account_id == account.id,
                        Holding.snapshot_date == snap_date)
                .delete(synchronize_session=False)
            )
            if deleted:
                logger.info(
                    "Cleared %d stale holdings for %s / %s before full snapshot refresh",
                    deleted, owner_id, result.institution,
                )

        for ph in result.holdings:
            isin_key = ph.isin or ph.symbol or ph.instrument_name[:12]

            # For demat institutions, check cross-account dedup at owner level.
            # CDSL (priority 10) always wins over Zerodha (priority 5).
            # This prevents the same equity position from being double-counted
            # when both a CDSL CAS and a Zerodha monthly statement are imported.
            if is_demat and isin_key:
                owner_holding = _find_owner_holding(db, owner_id, snap_date, isin_key)
                if owner_holding is not None:
                    existing_inst  = (
                        db.query(Account.institution)
                        .filter(Account.id == owner_holding.account_id)
                        .scalar() or ""
                    )
                    existing_priority = _HOLDING_PRIORITY.get(existing_inst.lower(), 1)
                    if my_priority > existing_priority:
                        # Current source is more authoritative — overwrite
                        owner_holding.units          = ph.units
                        owner_holding.current_nav    = ph.current_nav
                        owner_holding.current_value  = ph.current_value
                        owner_holding.invested_value = ph.invested_value
                        owner_holding.gain_loss      = ph.gain_loss
                        owner_holding.gain_loss_pct  = ph.gain_loss_pct
                        owner_holding.source_file    = result.source_file
                        if owner_holding.first_buy_date is None:
                            owner_holding.first_buy_date = snap_date
                        logger.debug(
                            "Holding %s overwritten by higher-priority source %s > %s",
                            isin_key, result.institution, existing_inst,
                        )
                    else:
                        logger.debug(
                            "Holding %s skipped — already present from %s (priority %d >= %d)",
                            isin_key, existing_inst, existing_priority, my_priority,
                        )
                    continue  # don't fall through to per-account check

            # Per-account upsert (non-demat, or demat with no cross-account match)
            existing = (
                db.query(Holding)
                .filter_by(account_id=account.id, snapshot_date=snap_date, isin=isin_key)
                .first()
            )
            if existing:
                existing.units          = ph.units
                existing.current_nav    = ph.current_nav
                existing.current_value  = ph.current_value
                existing.invested_value = ph.invested_value
                existing.gain_loss      = ph.gain_loss
                existing.gain_loss_pct  = ph.gain_loss_pct
                existing.source_file    = result.source_file
                # Preserve earliest buy date we've ever seen for this position
                if existing.first_buy_date is None:
                    existing.first_buy_date = snap_date
            else:
                db.add(Holding(
                    account_id=account.id,
                    snapshot_date=snap_date,
                    symbol=ph.symbol,
                    isin=isin_key,
                    instrument_name=ph.instrument_name,
                    units=ph.units,
                    avg_cost_nav=ph.avg_cost_nav,
                    current_nav=ph.current_nav,
                    current_value=ph.current_value,
                    invested_value=ph.invested_value,
                    gain_loss=ph.gain_loss,
                    gain_loss_pct=ph.gain_loss_pct,
                    holding_type=ph.holding_type,
                    first_buy_date=snap_date,
                    source_file=result.source_file,
                ))
                holding_count += 1

    # ── Balance snapshot ──────────────────────────────────────────────────────
    if result.balance:
        bal = result.balance
        existing_bal = (
            db.query(Balance)
            .filter_by(account_id=account.id, snapshot_date=bal.snapshot_date)
            .first()
        )
        if existing_bal:
            existing_bal.balance     = bal.balance
            existing_bal.source_file = result.source_file
        else:
            db.add(Balance(
                account_id=account.id,
                snapshot_date=bal.snapshot_date,
                balance=bal.balance,
                source_file=result.source_file,
            ))

    # ── Manual assets (FDs, PPF from SBI statements) ─────────────────────────
    manual_count = _persist_manual_assets(result, owner_id, db)

    # ── Auto-tag holdings with goal assignments ──────────────────────────────
    # Run unconditionally — sweeps up any orphan holdings whose tags were never
    # set (older imports, holdings inserted before tag rules existed, etc.).
    # Manually-tagged holdings are preserved (only NULL goal_tag rows are touched).
    from app.engine.holding_tagger import auto_tag_holdings
    auto_tag_holdings(db, owner_id)

    # ── Sync synthetic PPF assets from bank transfer debits ─────────────────
    # If the owner funds their PPF from a bank savings account that we DO
    # parse (e.g. Union Bank), each transfer-to-PPF debit becomes a contribution
    # on a synthetic ManualAsset row. This means a PPF that never sends its
    # own e-statement still shows up in net-worth + retirement corpus.
    try:
        from app.engine.ppf_from_transfers import sync_ppf_from_transfers
        sync_ppf_from_transfers(db)
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning("PPF transfer sync failed: %s", exc)

    # ── Refresh goal corpora from latest tagged holdings ────────────────────
    # Previously the goal page only updated when the user manually clicked
    # "Sync Corpus". Run it after every persist so corpus + on-track % always
    # reflect the most recent holdings snapshot.
    try:
        from app.engine.goals import refresh_corpus_from_networth
        refresh_corpus_from_networth(db, "family")
    except Exception as exc:  # never let goal refresh break ETL
        import logging
        logging.getLogger(__name__).warning("Goal corpus refresh failed: %s", exc)

    db.commit()
    return {"transactions": txn_count, "holdings": holding_count, "manual_assets": manual_count,
            "ledger_trades": ledger_counts["trades"], "ledger_movements": ledger_counts["movements"]}


def _deposit_notes(pa) -> str:
    """"Maturity value: ₹X" (what deposits.maturity_value reads), plus the date the principal is as of when it is the opening one."""
    note = f"Maturity value: ₹{pa.maturity_amount:,.2f}"
    if getattr(pa, "principal_as_of", None):
        note += f". Principal as of {pa.principal_as_of.isoformat()}."
    return note


def _persist_manual_assets(result: ParseResult, owner_id: str, db: Session) -> int:
    """
    Upsert ParsedManualAsset records into manual_assets table.
    Dedup key: owner_id + institution + account_ref.
    Only updates if the statement date is newer than the last recorded update.
    Also deletes ManualAsset rows for FDs that appear in closed_account_refs.
    """
    from datetime import datetime
    count = 0

    # ── Delete closed FDs ─────────────────────────────────────────────────────
    for ref in result.closed_account_refs:
        name_pattern = f"%...{ref}"
        deleted = (
            db.query(ManualAsset)
            .filter(
                ManualAsset.owner_id    == owner_id,
                ManualAsset.institution == result.institution,
                ManualAsset.name.like(name_pattern),
                ManualAsset.asset_type  == "fd",
            )
            .all()
        )
        for row in deleted:
            logger.info("SBI FD closed — removing ManualAsset: %s (%s)", row.name, owner_id)
            db.delete(row)

    # ── Upsert active assets ──────────────────────────────────────────────────
    from datetime import date as _date
    from app.etl.deposit_history import record_snapshots
    record_snapshots(db, owner_id, result.manual_assets)          # the history, whether or not this is the newest statement
    for pa in result.manual_assets:
        # Skip FDs/RDs that have already matured — old statements would resurrect them
        if pa.asset_type in ("fd", "rd") and pa.maturity_date and pa.maturity_date < _date.today():
            continue

        existing = (
            db.query(ManualAsset)
            .filter(
                ManualAsset.owner_id    == owner_id,
                ManualAsset.institution == pa.institution,
                ManualAsset.asset_type  == pa.asset_type,
                ManualAsset.name        == pa.name,
            )
            .first()
        )

        # Always update — statement_date is compared against source_statement_date below,
        # not last_updated (which is the DB write time, not the statement generation date).
        # The email pipeline processes oldest-to-newest so the last write wins correctly.
        if existing and pa.statement_date and existing.source_statement_date:
            fills_accrued = existing.accrued_interest is None and pa.accrued_interest is not None \
                and pa.statement_date == existing.source_statement_date
            fills_anchor = bool(getattr(pa, "principal_as_of", None)) and "Principal as of" not in (existing.notes or "") \
                and pa.statement_date == existing.source_statement_date
            if pa.statement_date <= existing.source_statement_date and not (fills_accrued or fills_anchor):
                continue

        if existing:
            existing.current_value        = pa.current_value
            existing.source_statement_date = pa.statement_date
            if pa.interest_rate is not None:
                existing.interest_rate = pa.interest_rate
            if pa.start_date:
                existing.start_date = pa.start_date
            if pa.maturity_date:
                existing.maturity_date = pa.maturity_date
            if pa.maturity_amount:
                existing.notes = _deposit_notes(pa)
            existing.accrued_interest = pa.accrued_interest
            count += 1
        else:
            db.add(ManualAsset(
                owner_id=owner_id,
                asset_type=pa.asset_type,
                institution=pa.institution,
                name=pa.name,
                current_value=pa.current_value,
                invested_amount=pa.current_value,  # principal = invested for FD
                interest_rate=pa.interest_rate,
                start_date=pa.start_date,
                maturity_date=pa.maturity_date,
                source_statement_date=pa.statement_date,
                notes=_deposit_notes(pa) if pa.maturity_amount else None,
                accrued_interest=pa.accrued_interest,
            ))
            count += 1

    if count or result.closed_account_refs:
        logger.info(
            "Manual assets: %d upserted, %d closed for %s / %s",
            count, len(result.closed_account_refs), owner_id, result.institution,
        )
    return count
