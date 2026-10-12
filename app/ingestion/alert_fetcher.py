"""
Pull bank alert e-mails (HDFC / SBI) from every mailbox and store them idempotently in bank_alerts.

Alerts are small body-only mails, so they are parsed straight from IMAP — nothing is saved to
data/raw. Each alert gets a natural key, so re-fetching an overlapping window inserts nothing new.
"""
from __future__ import annotations

import email
import logging
import uuid
from datetime import date, datetime, timedelta
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.config import settings
from app.ingestion.email_fetcher import _imap_connect, _parse_imap_fetch
from app.ingestion.parsers.bank_alert import BankAlert, clean_text, parse_alert, parse_tds
from app.models.schemas import BankAlertRow, TdsDeduction

logger = logging.getLogger(__name__)

_SENDERS = ("hdfcbank.bank.in", "hdfcbank.net", "alerts.sbi.bank.in")
_SUBJECT_HINTS = ("upi txn", "account update", "deposit alert", "cbssbi", "neft transaction",
                  "transaction success", "debited", "credited")


def alert_key(owner_id: str, a: BankAlert) -> str:
    """Identity of one event. Ref-less alerts of the same kind collapse (HDFC sends ACH twice)."""
    return (f"{owner_id}|{a.bank}|{a.account_last4 or ''}|{a.txn_date}|{a.direction}|{a.amount}|"
            f"{a.kind if not a.ref else ''}|{a.ref or ''}")


def _subject(msg) -> str:
    try:
        return str(make_header(decode_header(msg.get("Subject") or "")))
    except Exception:
        return msg.get("Subject") or ""


def _body(msg) -> str:
    for ctype in ("text/plain", "text/html"):
        for part in msg.walk():
            if part.get_content_type() == ctype:
                raw = part.get_payload(decode=True)
                if raw:
                    return clean_text(raw.decode(part.get_content_charset() or "utf-8", "replace"))
    return ""


def _mailbox(host: str) -> str:
    return '"[Gmail]/All Mail"' if "gmail" in host else "INBOX"


def fetch_alerts(since_days: int, tds: list | None = None) -> list[tuple[str, BankAlert, datetime | None]]:
    """(owner_id, alert, sent_at) for every parseable alert in every mailbox. TDS notices (not transactions) are
    appended to `tds` as (owner_id, last4, amount, date) when a list is given."""
    since = (date.today() - timedelta(days=since_days)).strftime("%d-%b-%Y")
    out: list[tuple[str, BankAlert, datetime | None]] = []
    for acc in settings.get_email_accounts():
        try:
            mail = _imap_connect(acc)
        except Exception as exc:
            logger.error("alerts: cannot open %s: %s", acc["address"], exc)
            continue
        try:
            mail.select(_mailbox(acc["imap_host"]), readonly=True)
            seen: set[bytes] = set()
            for sender in _SENDERS:
                _, data = mail.search(None, "SINCE", since, "FROM", f'"{sender}"')
                seen.update(data[0].split() if data and data[0] else [])
            n_before = len(out)
            for uid in sorted(seen, key=int):
                try:
                    raw = _parse_imap_fetch(mail.fetch(uid, "(RFC822)")[1])
                    if not raw:
                        continue
                    msg = email.message_from_bytes(raw)
                    subj = _subject(msg).lower()
                    if not any(h in subj for h in _SUBJECT_HINTS):
                        continue
                    body = _body(msg)
                    alert = parse_alert(msg.get("From", ""), body)
                    try:
                        sent = parsedate_to_datetime(msg.get("Date"))
                    except Exception:
                        sent = None
                    if alert:
                        out.append((acc["owner_id"], alert, sent))
                    elif tds is not None and (t := parse_tds(msg.get("From", ""), body)) and sent:
                        tds.append((acc["owner_id"], t[0], t[1], sent.date()))
                except Exception as exc:                   # one bad mail must not stop the rest
                    logger.warning("alerts: skipped a message in %s: %s", acc["address"], exc)
            logger.info("alerts: %s → %d alerts from %d candidate mails", acc["address"],
                        len(out) - n_before, len(seen))
        finally:
            try:
                mail.logout()
            except Exception:
                pass
    return out


def ingest_alerts(db: Session, since_days: int = 30) -> dict[str, int]:
    tds: list = []
    found = fetch_alerts(since_days, tds)
    tds_new = 0
    for owner_id, last4, amount, when in tds:
        key = f"{owner_id}|{last4}|{when}|{amount}"
        tds_new += db.execute(pg_insert(TdsDeduction.__table__).values(
            id=uuid.uuid4(), owner_id=owner_id, account_last4=last4, deducted_on=when, amount=amount, natural_key=key
        ).on_conflict_do_nothing(index_elements=["natural_key"])).rowcount
    rows, keys = [], set()
    for owner_id, a, sent in found:
        k = alert_key(owner_id, a)
        if k in keys:
            continue
        keys.add(k)
        rows.append({"id": uuid.uuid4(), "owner_id": owner_id, "bank": a.bank, "account_last4": a.account_last4,
                     "direction": a.direction, "amount": a.amount, "txn_date": a.txn_date, "kind": a.kind,
                     "payee": a.payee, "vpa": a.vpa, "ref": a.ref, "balance_after": a.balance_after,
                     "narration": a.narration, "sent_at": sent, "status": "new", "natural_key": k})
    inserted = 0
    for i in range(0, len(rows), 500):
        stmt = pg_insert(BankAlertRow.__table__).values(rows[i:i + 500])
        inserted += db.execute(stmt.on_conflict_do_nothing(index_elements=["natural_key"])).rowcount
    db.commit()
    return {"parsed": len(found), "new": inserted, "tds_new": tds_new}
