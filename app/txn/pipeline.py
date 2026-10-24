"""
One place that decides what every transaction is, in a fixed order of authority:

  1. trade     rows that are trade cashflows (contract note / Coin / tradebook) or demat unit counts
  2. user      payees you labelled (and rows you edited) — never overwritten
  3. paired    own-account transfers found by matching debit ↔ credit (app.txn.transfers)
  4. rule      the regex rules in app.etl.categorizer
  5. memory    a remembered payee label (user, or an LLM suggestion above the confidence bar)
  6. llm       Ollama's suggestion for a payee nobody has labelled, stored in memory
  anything still unknown is flagged needs_review for the Review list.

Idempotent: safe after every sync; only rows whose source is trade/rule/memory/llm are re-decided.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from decimal import Decimal
from typing import Optional

from sqlalchemy.orm import Session

from app.etl.categorizer import categorise
from app.llm import categorise as llm_cat
from app.models.schemas import Account, InstrumentTrade, PayeeMemory, Transaction
from app.txn.normalize import is_family_payee, is_own_payee, payee_key

logger = logging.getLogger(__name__)

REDECIDABLE = (None, "", "rule", "memory", "llm", "trade")


def _trade_fingerprints(db: Session) -> set[str]:
    return {r[0] for r in db.query(InstrumentTrade.txn_fingerprint)
            .filter(InstrumentTrade.txn_fingerprint.isnot(None)).all()}


def tag_trades(db: Session) -> int:
    """Trade cashflows are investments, demat unit counts are not cash — whatever their narration says."""
    linked = _trade_fingerprints(db)
    changed = 0
    q = (db.query(Transaction, Account.institution)
         .join(Account, Transaction.account_id == Account.id)
         .filter(Account.institution.in_(["Zerodha", "Zerodha Coin", "CDSL"])))
    for t, inst in q:
        if t.category_source == "user":
            continue
        desc = (t.description or "").upper()
        if desc.startswith("DEMAT "):
            want = ("transfer", "demat_movement")
        elif t.fingerprint in linked or (t.isin and inst in ("Zerodha", "Zerodha Coin")):
            want = ("investment", "mutual_fund" if (t.isin or "").startswith("INF") else "stocks")
        else:
            continue
        if (t.category, t.subcategory, t.category_source) != (*want, "trade"):
            t.category, t.subcategory, t.category_source, t.needs_review = want[0], want[1], "trade", False
            changed += 1
    return changed


def is_authoritative(mem: Optional[PayeeMemory]) -> bool:
    """Only a label you gave is applied automatically; an LLM suggestion waits in the Review list."""
    return bool(mem and mem.source == "user" and mem.category != "other")


def _owner_names() -> list[str]:
    try:
        from app.config import settings
        return [o.get("name") for o in settings.get_accounts_config().get("owners", []) if o.get("name")]
    except Exception:
        return []


def _family_names() -> list[str]:
    """Private list in config/accounts.yml (`family_names`): parents etc. Their money in or out is a transfer, not income or spending."""
    try:
        from app.config import settings
        return [n for n in (settings.get_accounts_config().get("family_names") or []) if n]
    except Exception:
        return []


def _apply_rules_and_memory(db: Session, memory: dict[str, PayeeMemory]) -> dict[str, int]:
    stats = defaultdict(int)
    names = _owner_names()
    family = _family_names()
    q = (db.query(Transaction).join(Account, Transaction.account_id == Account.id)
         .filter(Account.account_type == "bank"))
    for t in q:
        if t.category_source not in REDECIDABLE or t.category_source == "trade":
            continue
        desc = t.description or t.raw_description or ""
        cat = categorise(desc)
        source, conf, review = "rule", None, False
        category, sub = cat.category, cat.subcategory
        if category not in ("investment", "income", "emi", "insurance") and is_own_payee(t.raw_description or desc, names):
            category, sub = "transfer", "own_account"
        elif category not in ("investment", "income", "emi", "insurance") and is_family_payee(t.raw_description or desc, family):
            category, sub = "transfer", "family"
        elif category == "other":
            key = payee_key(t.raw_description or desc)
            mem = memory.get(key) if key else None
            if is_authoritative(mem):
                category, sub = mem.category, mem.subcategory
                source, conf = "memory", 1.0
            elif key:
                review = True
        if (t.category, t.subcategory, t.category_source, bool(t.needs_review)) != (category, sub, source, review):
            t.category, t.subcategory, t.category_source, t.needs_review = category, sub, source, review
            t.category_conf = Decimal(str(conf)) if conf is not None else None
            stats["retagged"] += 1
        stats["review"] += int(review)
    return stats


def _ask_llm_for_unknown_payees(db: Session, memory: dict[str, PayeeMemory], limit: int) -> int:
    agg: dict[str, dict] = {}
    q = (db.query(Transaction).filter(Transaction.needs_review.is_(True)))
    for t in q:
        key = payee_key(t.raw_description or t.description)
        if not key or key in memory:
            continue
        a = agg.setdefault(key, {"key": key, "payee": key, "sample": (t.raw_description or t.description or "")[:120],
                                 "direction": t.transaction_type, "amount": 0.0, "n": 0})
        a["amount"] += float(t.amount)
        a["n"] += 1
    items = sorted(agg.values(), key=lambda a: -a["amount"])[:limit]
    if not items:
        return 0
    for a in items:
        a["amount"] = a["amount"] / a["n"]
    suggestions = llm_cat.classify(items)
    for key, (cat, sub, conf) in suggestions.items():
        row = PayeeMemory(payee_key=key, category=cat, subcategory=sub, source="llm",
                          confidence=Decimal(str(conf)), sample=agg[key]["sample"][:200])
        db.merge(row)
        memory[key] = row
    db.flush()
    return len(suggestions)


def suggest_labels(db: Session, limit: int = 24) -> dict:
    """On demand: ask the local model about the biggest unlabelled payees. Suggestions only."""
    memory = {m.payee_key: m for m in db.query(PayeeMemory).all()}
    n = _ask_llm_for_unknown_payees(db, memory, limit)
    db.commit()
    return {"suggested": n, "limit": limit}


def retag(db: Session, use_llm: bool = True, llm_limit: int = 60) -> dict:
    out: dict = {"trades_tagged": tag_trades(db)}
    db.flush()
    from app.ledger import sgb_evidence
    out["sgb_tagged"] = sgb_evidence.tag(db)          # SGB coupons and subscriptions proven by arithmetic on the statements
    db.flush()
    from app.ledger import dividends
    out["dividends_tagged"] = dividends.tag(db)       # dividends proven by shares held x per-share amount (cached history)
    db.flush()
    memory = {m.payee_key: m for m in db.query(PayeeMemory).all()}
    out.update(_apply_rules_and_memory(db, memory))
    if use_llm:
        out["llm_suggestions"] = _ask_llm_for_unknown_payees(db, memory, llm_limit)
        if out["llm_suggestions"]:
            out.update({f"after_llm_{k}": v for k, v in _apply_rules_and_memory(db, memory).items()})
    db.commit()
    return out


# ── Review list ──────────────────────────────────────────────────────────────
def account_label(a: Account) -> str:
    """"HDFC …1632 (user)": what a person recognises, not the row id."""
    ref = (a.account_ref or "")
    ref = ref[-4:] if ref.isdigit() or ref.replace("X", "").isdigit() else ""            # "unknown" or a client code is not a number to show
    return f"{a.institution or a.account_type}{' …' + ref if ref else ''} ({a.owner_id})"


def review_list(db: Session, limit: int = 50) -> list[dict]:
    """Payees nobody has labelled, biggest first. Each says when it last happened (date; the time too when a bank alert confirmed that payment),
    for how much, and from which account, so it can be recognised without opening the transaction list."""
    from app.models.schemas import BankAlertRow
    mem = {m.payee_key: m for m in db.query(PayeeMemory).all()}
    groups: dict[str, dict] = {}
    for t, acc in db.query(Transaction, Account).join(Account, Transaction.account_id == Account.id).filter(Transaction.needs_review.is_(True)):
        key = payee_key(t.raw_description or t.description)
        if not key:
            continue
        g = groups.setdefault(key, {"payee_key": key, "sample": (t.raw_description or t.description or "")[:100],
                                    "count": 0, "debit_total": 0.0, "credit_total": 0.0, "last_date": None, "accounts": {}, "_last": None})
        g["count"] += 1
        g["debit_total" if t.transaction_type == "debit" else "credit_total"] += float(t.amount)
        g["accounts"][account_label(acc)] = g["accounts"].get(account_label(acc), 0) + 1
        g["last_date"] = max(filter(None, [g["last_date"], t.date]))
        if g["_last"] is None or (t.date, str(t.created_at)) > (g["_last"].date, str(g["_last"].created_at)):
            g["_last"] = t
    out = []
    for key, g in groups.items():
        last = g.pop("_last")
        alert = db.query(BankAlertRow).filter(BankAlertRow.matched_txn_id == last.id).order_by(BankAlertRow.sent_at.desc()).first()
        g["last"] = {"date": str(last.date), "time": alert.sent_at.astimezone().strftime("%H:%M") if alert is not None and alert.sent_at else None,
                     "amount": float(last.amount), "direction": last.transaction_type, "account": account_label(db.get(Account, last.account_id))}
        g["accounts"] = sorted(g["accounts"], key=lambda a: -g["accounts"][a])
        m = mem.get(key)
        g["suggestion"] = ({"category": m.category, "subcategory": m.subcategory,
                            "confidence": float(m.confidence or 0)} if m else None)
        g["last_date"] = str(g["last_date"])
        out.append(g)
    out.sort(key=lambda g: -(g["debit_total"] + g["credit_total"]))
    return out[:limit]


def label_payee(db: Session, key: str, category: str, subcategory: Optional[str] = None) -> dict:
    """Remember the label and apply it to every past and future transaction with that payee."""
    db.merge(PayeeMemory(payee_key=key, category=category, subcategory=subcategory or None,
                         source="user", confidence=Decimal(1)))
    n = 0
    for t in db.query(Transaction).join(Account, Transaction.account_id == Account.id) \
            .filter(Account.account_type == "bank"):
        if t.category_source in ("trade",) or payee_key(t.raw_description or t.description) != key:
            continue
        t.category, t.subcategory, t.category_source, t.needs_review = category, subcategory or None, "user", False
        n += 1
    db.commit()
    return {"payee_key": key, "category": category, "subcategory": subcategory, "transactions_updated": n}
