"""
IPO allotments and bonus issues from registrar mails, matched to the depository credit of the same quantity.

A contract note exists for a market trade; an IPO allotment and a bonus issue have none, so the shares sat in the ledger with an
unknown cost (and a sale of them in the tax view as "cost unknown"). The registrar's mail states the price and the quantity; the
demat statement's credit of that quantity in the same demat names the stock and the day. Together they are a proven trade:
  IPO allotment -> a buy at the issue price on the credit date (source `ipo_allotment`)
  bonus issue   -> a buy at nil cost on the credit date (source `bonus`; cost of acquisition of a bonus share is nil)
"""
from __future__ import annotations

import email
import logging
import re
import uuid
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from app.config import settings
from app.ingestion.email_fetcher import _imap_connect, _parse_imap_fetch
from app.ingestion.parsers.registrar_notice import BonusAllotment, IpoAllotment, parse_bonus, parse_ipo_allotment
from app.models.schemas import Account, DematMovement, InstrumentTrade

logger = logging.getLogger(__name__)
_PHRASES = ("Allotment advice cum Unblocking", "Allotment of Bonus")
DAYS_TOL = 3
_TEMPORARY_ISIN = ("IN8", "IN9")     # the depository's stand-in codes for shares not yet listed under the real ISIN


def _body(msg) -> str:
    out = ""
    for part in msg.walk():
        if part.get_content_type() in ("text/plain", "text/html"):
            raw = part.get_payload(decode=True)
            if raw:
                out += " " + raw.decode(part.get_content_charset() or "utf-8", "replace")
    return out


def fetch_notices(since_days: int) -> list[tuple[str, object, date, str]]:
    """(owner, notice, mail date, message id) for every parseable notice in every mailbox."""
    since = (date.today() - timedelta(days=since_days)).strftime("%d-%b-%Y")
    out = []
    for acc in settings.get_email_accounts():
        gmail = "gmail" in acc["imap_host"]
        try:
            mail = _imap_connect(acc)
        except Exception as exc:
            logger.error("registrar notices: cannot open %s: %s", acc["address"], exc)
            continue
        try:
            mail.select('"[Gmail]/All Mail"' if gmail else "INBOX", readonly=True)
            ids: set[bytes] = set()
            for phrase in _PHRASES:
                if gmail:
                    _, data = mail.search(None, "X-GM-RAW", f'"\\"{phrase}\\" after:{(date.today() - timedelta(days=since_days)).strftime("%Y/%m/%d")}"')
                else:
                    _, data = mail.search(None, "SINCE", since, "TEXT", f'"{phrase}"')
                ids.update(data[0].split() if data and data[0] else [])
            for uid in sorted(ids, key=int):
                try:
                    raw = _parse_imap_fetch(mail.fetch(uid, "(RFC822)")[1])
                    if not raw:
                        continue
                    msg = email.message_from_bytes(raw)
                    body = _body(msg)
                    notice = parse_ipo_allotment(body) or parse_bonus(body)
                    if notice:
                        sent = email.utils.parsedate_to_datetime(msg.get("Date")).date()
                        out.append((acc["owner_id"], notice, sent, (msg.get("Message-ID") or uid.decode())[:80]))
                except Exception as exc:                       # one bad mail must not stop the rest
                    logger.warning("registrar notices: skipped a message in %s: %s", acc["address"], exc)
        finally:
            try:
                mail.logout()
            except Exception:
                pass
    return out


def _credit(db: Session, owner_id: str, qty: int, near: date, demat_id: str, before: int = DAYS_TOL, after: int = DAYS_TOL):
    """The depository credit of exactly this quantity around that day in this demat: the line that names the stock."""
    q = (db.query(DematMovement).filter(DematMovement.owner_id == owner_id, DematMovement.qty_in == Decimal(qty),
                                        DematMovement.move_date >= near - timedelta(days=before),
                                        DematMovement.move_date <= near + timedelta(days=after))
         .order_by(DematMovement.move_date))
    rows = [r for r in q if not re.search(r"\b\d{16}\b", r.description or "") or demat_id in (r.description or "")]
    rows = [r for r in rows if not (r.isin or "").startswith(_TEMPORARY_ISIN)] or rows   # a bonus first lands under a temporary ISIN, then converts
    isins = {r.isin for r in rows}
    return rows[0] if len(isins) == 1 else None                  # two stocks credited the same quantity that week: do not guess


def store_notices(db: Session, found: list) -> dict[str, int]:
    made = {"ipo_allotment": 0, "bonus": 0, "unmatched": 0}
    for owner_id, notice, sent, ref in found:
        acct = (db.query(Account).filter(Account.owner_id == owner_id, Account.institution == "Zerodha", Account.account_type == "stocks").first())
        if acct is None:
            continue
        if isinstance(notice, IpoAllotment):
            mv, kind, qty, price = _credit(db, owner_id, notice.allotted, notice.credited_on, notice.demat_id), "ipo_allotment", notice.allotted, notice.price
        elif isinstance(notice, BonusAllotment):
            mv, kind, qty, price = _credit(db, owner_id, notice.bonus, sent, notice.demat_id, before=14, after=3), "bonus", notice.bonus, Decimal(0)
        else:
            continue
        if mv is None:
            made["unmatched"] += 1
            continue
        key = f"{kind}|{owner_id}|{mv.isin}|{mv.move_date}|{qty}"
        if db.query(InstrumentTrade.id).filter(InstrumentTrade.natural_key == key).first():
            continue
        db.add(InstrumentTrade(id=uuid.uuid4(), owner_id=owner_id, account_id=acct.id, isin=mv.isin, symbol=(mv.symbol or "")[:200],
                               instrument_name=(mv.symbol or "")[:200], holding_type="equity", trade_date=mv.move_date, settle_date=mv.move_date,
                               side="B", quantity=Decimal(qty), price=price, amount=price * qty, source=kind, source_ref=ref,
                               source_file="registrar mail", natural_key=key))
        made[kind] += 1
    db.commit()
    return made


def ingest_registrar_notices(db: Session, since_days: int = 400) -> dict[str, int]:
    found = fetch_notices(since_days)
    return {"notices": len(found), **store_notices(db, found)}
