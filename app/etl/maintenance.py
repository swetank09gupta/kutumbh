"""
Post-sync maintenance pipeline.

Run after every full-sync or reprocess to keep derived state honest:

  1. dedup_transactions     — merge fingerprint-equivalent duplicates
  2. dedup_unknown_accounts — merge "unknown"-ref account rows into canonical
  3. backfill_coin_isins    — tag Coin txns with ISIN from AMFI
  4. refresh_mf_navs        — restamp MF holdings to today's NAVs
  5. purge_synthetic_redemptions — remove invented break-even credits an older version left (none are created now)
  5b. loan_outstanding      — home-loan liability from the lender's prepayment letters (app.engine.loan)
  6. recategorize           — re-run categorizer on every txn (catches rule updates)

Each step is independent and safe to skip on error. Returns a dict of counts
that the sync route surfaces in /api/sync/status's `last_result.maintenance`.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, text
from sqlalchemy.orm import Session

from app.engine.amfi_isin import (
    get_nav as _amfi_get_nav,
    lookup_isin as _amfi_lookup_isin,
    refresh as _amfi_refresh,
)
from app.etl.categorizer import categorise
from app.etl.dedup import equity_demat_fingerprint, is_ordinal_fingerprint, transaction_fingerprint
from app.models.schemas import (
    Account, Balance, Holding, Transaction,
)

_DEMAT_INSTITUTIONS = {"cdsl", "zerodha"}

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# 1. Duplicate transactions
# ─────────────────────────────────────────────────────────────────────────────
def _dedup_transactions(db: Session) -> dict[str, int]:
    # Join with accounts so we can replicate the normalizer's fingerprint choice:
    # equity_demat_fingerprint for ISIN-tagged demat rows, transaction_fingerprint otherwise.
    pairs = db.query(Transaction, Account).join(Account, Transaction.account_id == Account.id).all()
    by_fp: dict[str, list[Transaction]] = defaultdict(list)
    fp_map: dict[str, str] = {}  # transaction id → recomputed fingerprint
    for r, acc in pairs:
        is_demat_row = r.isin and (
            acc.institution.lower() in _DEMAT_INSTITUTIONS
            or (r.description or "").upper().startswith("DEMAT ")
        )
        if is_demat_row:
            fp = equity_demat_fingerprint(
                acc.owner_id, r.isin, r.date, r.amount, r.transaction_type,
            )
        else:
            if is_ordinal_fingerprint(r.fingerprint, str(r.account_id), r.date, r.amount, r.transaction_type,
                                      r.description or "", r.reference_number):
                continue                   # the 2nd..nth identical row of a statement, proven separate by its balance
            fp = transaction_fingerprint(
                str(r.account_id), r.date, r.amount, r.transaction_type,
                r.description or "", r.reference_number, with_direction=acc.account_type == "bank",
            )
        by_fp[fp].append(r)
        fp_map[str(r.id)] = fp
    deleted = 0
    to_refingerprint: list[Transaction] = []
    for fp, group in by_fp.items():
        if len(group) > 1:
            group.sort(key=lambda t: -len(t.description or ""))
            to_refingerprint.append(group[0])
            for t in group[1:]:
                db.delete(t)
                deleted += 1
        else:
            # Single row — re-fingerprint if stored hash drifted from current
            # normalization (e.g. after a dedup.py logic upgrade).
            r = group[0]
            if r.fingerprint != fp:
                to_refingerprint.append(r)
    db.commit()
    # Re-fingerprint (best-effort; skip on unique-constraint collision).
    updated = 0
    for k in to_refingerprint:
        new_fp = fp_map[str(k.id)]
        if k.fingerprint == new_fp:
            continue
        old_fp = k.fingerprint
        k.fingerprint = new_fp
        try:
            # ledger trades point at their cashflow row by this fingerprint: they must follow it (an unfollowed re-key left 216
            # trades "without a cashflow" and out of XIRR, Oct 2026)
            db.execute(text("UPDATE instrument_trades SET txn_fingerprint = :new WHERE txn_fingerprint = :old"),
                       {"new": new_fp, "old": old_fp})
            db.commit()
            updated += 1
        except Exception:
            db.rollback()
    return {"deleted": deleted, "keepers_refingerprinted": updated, "trades_relinked": _relink_trades(db)}


def _relink_trades(db: Session) -> int:
    """Point a ledger trade whose cashflow link is dead at its cashflow row, when exactly one row of its account has the same
    date, amount and reference (the link was written when the row's fingerprint was different)."""
    res = db.execute(text("""
        UPDATE instrument_trades t SET txn_fingerprint = c.fp
        FROM (SELECT t2.id AS tid, min(x.fingerprint) AS fp, count(*) AS n
                FROM instrument_trades t2
                JOIN transactions x ON x.account_id = t2.account_id AND x.date = t2.trade_date
                                   AND abs(x.amount - t2.amount) < 0.01 AND x.reference_number = t2.source_ref
               WHERE t2.txn_fingerprint IS NOT NULL AND t2.source_ref IS NOT NULL
                 AND NOT EXISTS (SELECT 1 FROM transactions y WHERE y.fingerprint = t2.txn_fingerprint)
               GROUP BY t2.id) c
        WHERE t.id = c.tid AND c.n = 1"""))
    db.commit()
    return res.rowcount


# ─────────────────────────────────────────────────────────────────────────────
# 2. Unknown-ref account dedup
# ─────────────────────────────────────────────────────────────────────────────
def _absorb_account(db: Session, canonical: Account, other: Account) -> None:
    """Move everything of `other` onto `canonical` (rows that would collide on date/isin are dropped) and delete `other`."""
    db.execute(text("UPDATE transactions SET account_id = :new WHERE account_id = :old"),
               {"new": str(canonical.id), "old": str(other.id)})
    db.execute(text("""
        DELETE FROM holdings h WHERE h.account_id = :old
          AND EXISTS (SELECT 1 FROM holdings c
                        WHERE c.account_id = :new
                          AND c.snapshot_date = h.snapshot_date
                          AND COALESCE(c.isin,'') = COALESCE(h.isin,''))
    """), {"new": str(canonical.id), "old": str(other.id)})
    db.execute(text("UPDATE holdings SET account_id = :new WHERE account_id = :old"),
               {"new": str(canonical.id), "old": str(other.id)})
    db.execute(text("""
        DELETE FROM balances b WHERE b.account_id = :old
          AND EXISTS (SELECT 1 FROM balances c
                        WHERE c.account_id = :new
                          AND c.snapshot_date = b.snapshot_date)
    """), {"new": str(canonical.id), "old": str(other.id)})
    db.execute(text("UPDATE balances SET account_id = :new WHERE account_id = :old"),
               {"new": str(canonical.id), "old": str(other.id)})
    db.execute(text("DELETE FROM accounts WHERE id = :old"), {"old": str(other.id)})


def _masks_agree(a: str, b: str) -> bool:
    """Two printed renderings of one account number: same length, equal wherever both show a digit
    (50410XXXXX and XXXXXX3237 are the same 10-digit number seen through different masks)."""
    a, b = (a or "").upper(), (b or "").upper()
    return len(a) == len(b) and "X" in a + b and all(x == y or "X" in (x, y) for x, y in zip(a, b))


def _merge_masked_accounts(db: Session) -> dict[str, int]:
    groups: dict[tuple, list[Account]] = defaultdict(list)
    for a in db.query(Account).all():
        groups[(a.owner_id, a.institution, a.account_type)].append(a)
    merged = 0
    for accs in groups.values():
        accs = [a for a in accs if (a.account_ref or "").lower() != "unknown"]
        weight = lambda a: (db.query(Transaction).filter(Transaction.account_id == a.id).count(),
                            db.execute(text("SELECT count(*) FROM balances WHERE account_id = :a"), {"a": str(a.id)}).scalar())
        gone: set = set()
        for a in sorted(accs, key=weight, reverse=True):         # the account with most data absorbs the others
            if a.id in gone:
                continue
            for b in accs:
                if b is not a and b.id not in gone and _masks_agree(a.account_ref, b.account_ref):
                    _absorb_account(db, a, b)
                    gone.add(b.id)
                    merged += 1
    db.commit()
    return {"masked_accounts_merged": merged}


def _dedup_unknown_accounts(db: Session) -> dict[str, int]:
    groups: dict[tuple, list[Account]] = defaultdict(list)
    for a in db.query(Account).all():
        groups[(a.owner_id, a.institution, a.account_type)].append(a)
    merges = 0
    for accs in groups.values():
        if len(accs) < 2:
            continue
        unknown = [a for a in accs if (a.account_ref or "").lower() == "unknown"]
        real    = [a for a in accs if (a.account_ref or "").lower() != "unknown"]
        if not unknown or not real:
            continue
        canonical = max(
            real,
            key=lambda a: db.query(Transaction).filter(Transaction.account_id == a.id).count()
        )
        for u in unknown:
            _absorb_account(db, canonical, u)
            merges += 1
    db.commit()
    return {"phantoms_merged": merges}


# ─────────────────────────────────────────────────────────────────────────────
# 3. Coin ISIN backfill
# ─────────────────────────────────────────────────────────────────────────────
def _backfill_coin_isins(db: Session) -> dict[str, int]:
    coin_accts = db.query(Account).filter(Account.institution.ilike("%Coin%")).all()
    if not coin_accts:
        return {"updated": 0, "unresolved": 0}
    acc_ids = [a.id for a in coin_accts]
    rows = (
        db.query(Transaction)
        .filter(Transaction.account_id.in_(acc_ids),
                Transaction.isin.is_(None),
                Transaction.counterparty.isnot(None))
        .all()
    )
    updated = unresolved = 0
    for r in rows:
        isin = _amfi_lookup_isin(r.counterparty)
        if isin:
            r.isin = isin
            updated += 1
        else:
            unresolved += 1
    db.commit()
    return {"updated": updated, "unresolved": unresolved}


# ─────────────────────────────────────────────────────────────────────────────
# 4. NAV refresh
# ─────────────────────────────────────────────────────────────────────────────
def _refresh_mf_navs(db: Session) -> dict[str, Any]:
    try:
        _amfi_refresh()
    except Exception as exc:
        logger.warning("AMFI refresh failed (using cached): %s", exc)

    latest = db.query(func.max(Holding.snapshot_date)).scalar()
    if not latest:
        return {"updated": 0, "value_delta": 0}
    rows = (
        db.query(Holding)
        .filter(Holding.snapshot_date == latest,
                Holding.holding_type == "mutual_fund",
                Holding.isin.isnot(None))
        .all()
    )
    updated = 0
    delta = Decimal(0)
    for h in rows:
        nav = _amfi_get_nav(h.isin)
        if not nav or nav <= 0:
            continue
        old_nav = float(h.current_nav or 0)
        if abs(old_nav - nav) < 0.01:
            continue
        units = Decimal(str(h.units or 0))
        new_nav = Decimal(f"{nav:.4f}")
        new_value = (units * new_nav).quantize(Decimal("0.01"))
        old_value = Decimal(h.current_value or 0)
        h.current_nav = new_nav
        h.current_value = new_value
        delta += (new_value - old_value)
        updated += 1
    db.commit()
    return {"updated": updated, "value_delta": float(delta)}


# ─────────────────────────────────────────────────────────────────────────────
# 5. Ghost redemption synthesis
# ─────────────────────────────────────────────────────────────────────────────
def _purge_synthetic_redemptions(db: Session) -> dict[str, int]:
    """
    Earlier versions invented a break-even redemption 90 days after the last buy of any fund that
    was no longer held. A made-up credit is not evidence (it assumed a 0% return), so none is
    created any more: a sale is a trade (Coin mail, tradebook, or derived from the demat line and
    the NAV, app.ledger.derive) or the fund is reported as having no purchase history. This only
    removes rows an older run left behind.
    """
    n = db.query(Transaction).filter(Transaction.reference_number.like("SYNTH:%")).delete(synchronize_session=False)
    db.commit()
    return {"removed": n}



# ─────────────────────────────────────────────────────────────────────────────
# 6. Re-categorize
# ─────────────────────────────────────────────────────────────────────────────
def _recategorize(db: Session) -> dict[str, int]:
    # Only touch rows with a missing category — the normalizer already categorizes
    # on insert, so the only uncategorized rows are those created by other maintenance
    # steps.
    # Full re-categorization after rule updates is handled by POST /api/sync/recategorize.
    from sqlalchemy import or_
    rows = db.query(Transaction).filter(
        or_(Transaction.category.is_(None), Transaction.subcategory.is_(None))
    ).all()
    changed = 0
    for t in rows:
        new = categorise(t.description or "")
        if t.category != new.category or t.subcategory != new.subcategory:
            t.category = new.category
            t.subcategory = new.subcategory
            changed += 1
    db.commit()
    return {"updated": changed}


# ─────────────────────────────────────────────────────────────────────────────
# 7. Archived placeholder owners
# ─────────────────────────────────────────────────────────────────────────────
def _cleanup_phantom_owners(db: Session) -> dict[str, int]:
    """
    Owners whose id starts with '.' are archived placeholders (merged accounts). They must not
    carry assets: one leftover synthesised PPF made Family net worth exceed Self + Spouse by ₹5.3L.
    """
    from app.models.schemas import ManualAsset, NetWorthSnapshot, Owner
    removed = {"manual_assets": 0, "snapshots": 0, "owners": 0}
    for o in db.query(Owner).filter(Owner.id.like(".%")).all():
        removed["manual_assets"] += db.query(ManualAsset).filter(ManualAsset.owner_id == o.id) \
            .delete(synchronize_session=False)
        removed["snapshots"] += db.query(NetWorthSnapshot).filter(NetWorthSnapshot.owner_id == o.id) \
            .delete(synchronize_session=False)
        if not db.query(Account).filter(Account.owner_id == o.id).count():
            db.delete(o)
            removed["owners"] += 1
    db.commit()
    return removed


def _txn_dedupe(db: Session) -> dict:
    from app.txn.dedupe import dedupe_bank_transactions
    return dedupe_bank_transactions(db, apply=True)


def _txn_pair_transfers(db: Session) -> dict:
    from app.txn.transfers import pair_own_transfers
    return pair_own_transfers(db, apply=True)


def _txn_retag(db: Session) -> dict:
    # LLM suggestions are opt-in: a local model proved overconfident on payee names, so they
    # are only ever suggestions in the Review list (TXN_LLM=1 to generate them during sync).
    import os
    from app.txn.pipeline import retag
    return retag(db, use_llm=os.getenv("TXN_LLM", "0") == "1")


# ─────────────────────────────────────────────────────────────────────────────
# Orchestrator
# ─────────────────────────────────────────────────────────────────────────────
def _ppf_from_transfers(db: Session) -> dict:
    from app.engine.ppf_from_transfers import sync_ppf_from_transfers
    return sync_ppf_from_transfers(db)


def _ppf_rates(db: Session) -> dict:
    """PPF pays the Government's notified rate, not a number someone typed: refresh it from the NSI table."""
    from app.engine.small_savings import ppf_current_rate
    from app.models.schemas import ManualAsset
    rate = Decimal(str(ppf_current_rate()))
    n = 0
    for a in db.query(ManualAsset).filter(ManualAsset.asset_type == "ppf").all():
        if a.interest_rate != rate:
            a.interest_rate = rate
            n += 1
    db.commit()
    return {"rate": float(rate), "updated": n}


def _loan_outstanding(db: Session) -> dict:
    from app.engine.loan import apply_loan_anchors
    return apply_loan_anchors(db)


def _dedupe_coin_trades(db: Session) -> dict:
    """Remove a Coin order stored twice (same owner, folio when printed, date, side, units, amount, and the same position among
    identical lines of its mail; filed under two ISINs), keeping the first. Two identical lines of one mail are two orders and
    are both kept."""
    from sqlalchemy import text
    n = db.execute(text("""
        DELETE FROM instrument_trades t USING instrument_trades u
        WHERE t.source = 'coin' AND u.source = 'coin'
          AND t.owner_id = u.owner_id AND t.source_ref IS NOT DISTINCT FROM u.source_ref AND t.trade_date = u.trade_date
          AND t.side = u.side AND t.quantity = u.quantity AND coalesce(t.amount, -1) = coalesce(u.amount, -1)
          AND coalesce(substring(t.natural_key from '[|]#([0-9]+)$'), '0') = coalesce(substring(u.natural_key from '[|]#([0-9]+)$'), '0')
          AND (t.created_at, t.id) > (u.created_at, u.id)""")).rowcount
    db.commit()
    return {"removed": n}


def _value_hand_held_sgb(db: Session) -> dict:
    from app.ledger import sgb_evidence
    done = sgb_evidence.value_hand_held(db)
    db.commit()
    return {"valued": len(done)}


def run_all(db: Session) -> dict[str, Any]:
    """Run every post-sync maintenance step. Each is wrapped in try/except so
    one failure doesn't sink the others. Returns counts for surfacing in the
    sync result payload."""
    results: dict[str, Any] = {}
    steps = [
        ("dedup_transactions",      _dedup_transactions),
        ("dedup_unknown_accounts",  _dedup_unknown_accounts),
        ("merge_masked_accounts",   _merge_masked_accounts),
        ("backfill_coin_isins",     _backfill_coin_isins),
        ("cleanup_phantom_owners",  _cleanup_phantom_owners),
        ("txn_dedupe",              _txn_dedupe),
        ("txn_pair_transfers",      _txn_pair_transfers),
        ("refresh_mf_navs",         _refresh_mf_navs),
        ("dedupe_coin_trades",      _dedupe_coin_trades),
        ("purge_synthetic_redemptions", _purge_synthetic_redemptions),
        ("ppf_from_transfers",      _ppf_from_transfers),
        ("ppf_rates",               _ppf_rates),
        ("loan_outstanding",        _loan_outstanding),
        ("recategorize",            _recategorize),
        ("hand_held_sgb_value",     _value_hand_held_sgb),
        ("txn_retag",               _txn_retag),   # last: one authority decides every category
    ]
    for name, fn in steps:
        try:
            logger.info("Maintenance: %s starting", name)
            results[name] = fn(db)
            logger.info("Maintenance: %s done — %s", name, results[name])
        except Exception as exc:
            logger.error("Maintenance: %s failed — %s", name, exc, exc_info=True)
            results[name] = {"error": str(exc)}
            db.rollback()
    return results
