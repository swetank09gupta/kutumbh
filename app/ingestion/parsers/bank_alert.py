"""
Bank alert e-mails (HDFC Insta Alerts, SBI CBSSBI / NEFT / YONO) → one small record each.

Alerts arrive the same minute as the transaction, weeks before the statement, so they are the
only early evidence of what happened. They are evidence, never cashflow: app.txn.alert_recon
matches them against statement rows and reports what is still pending or missing.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

SENDERS = ("hdfcbank.bank.in", "hdfcbank.net", "alerts.sbi.bank.in", "sbi.co.in")


@dataclass
class BankAlert:
    bank: str                     # HDFC | SBI
    account_last4: str | None
    direction: str                # debit | credit
    amount: Decimal
    txn_date: date
    kind: str                     # upi | ach | neft | transfer | deposit | nach | other
    payee: str | None = None
    vpa: str | None = None
    ref: str | None = None        # UTR / UPI reference: the strongest match key
    balance_after: Decimal | None = None
    narration: str | None = None


def _amt(s: str) -> Decimal:
    return Decimal(s.replace(",", ""))


def _date(s: str) -> date:
    s = s.strip().replace(".", "/")
    for fmt in ("%d-%m-%y", "%d-%b-%Y", "%d-%B-%Y", "%d/%m/%y", "%d/%m/%Y"):
        try:
            return datetime.strptime(s.title() if "-" in s and s[3:6].isalpha() else s, fmt).date()
        except ValueError:
            continue
    raise ValueError(s)


def clean_text(html_or_text: str) -> str:
    t = re.sub(r"<(style|script).*?</\1>", " ", html_or_text, flags=re.S | re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    t = t.replace("&nbsp;", " ").replace("&#160;", " ").replace("&amp;", "&")
    return re.sub(r"\s+", " ", t).strip()


_N = r"([\d,]+(?:\.\d+)?)"
_D = r"(\d\d-\d\d-\d\d|\d\d-[A-Za-z]{3}-\d{4}|\d\d/\d\d/\d\d(?:\d\d)?|\d\d\.\d\d\.\d\d)"

_HDFC = [
    ("upi", "debit", re.compile(
        rf"Rs\.?\s*{_N} is debited from your account ending (\d{{4}}) towards VPA (\S+) \((.*?)\) on {_D}\. "
        rf"UPI transaction reference no\.: (\d+)", re.I),
     lambda m: dict(amount=m[1], acct=m[2], vpa=m[3], payee=m[4], date=m[5], ref=m[6])),
    ("upi", "credit", re.compile(
        rf"Rs\.?\s*{_N} has been successfully credited to your HDFC Bank account ending in (\d{{4}})\..*?"
        rf"Date: {_D}.*?Sender: (.*?) \(VPA: (\S+?)\).*?UPI Reference No\.: (\d+)", re.I),
     lambda m: dict(amount=m[1], acct=m[2], date=m[3], payee=m[4], vpa=m[5], ref=m[6])),
    ("ach", "debit", re.compile(
        rf"Rs\.? INR {_N} is deducted from your account ending X*(\d{{4}}) and added to (.*?) account on {_D}\."
        rf"(?: The available balance in your account is Rs\. INR {_N})?", re.I),
     lambda m: dict(amount=m[1], acct=m[2], payee=m[3], date=m[4], balance=m[5])),
    ("ach", "debit", re.compile(
        rf"Rs\.?\s*{_N} has been debited from HDFC Bank Account Number X*(\d{{4}}) towards (.*?) "
        rf"with UMRN (\S+) on {_D}", re.I),
     lambda m: dict(amount=m[1], acct=m[2], payee=m[3], date=m[5])),
    ("deposit", "credit", re.compile(
        rf"Amount received: INR {_N} Account: X*(\d{{4}}) Date: {_D} Reference Details: (.*?) "
        rf"Available Balance: INR {_N}", re.I),
     lambda m: dict(amount=m[1], acct=m[2], date=m[3], payee=m[4], ref=m[4], balance=m[5])),
]

_PREFIX = re.compile(r"^(?:Transferred to|Deposit by transfer from)\s+", re.I)

_SBI = [
    ("transfer", None, re.compile(
        rf"Your A/C X+(\d+) (Debited|Credited) INR {_N} on {_D} -(.*?)\. Avl Bal(?:ance)? INR {_N}", re.I),
     lambda m: dict(acct=m[1], direction="debit" if m[2].lower().startswith("deb") else "credit", amount=m[3],
                    date=m[4], payee=_PREFIX.sub("", m[5]), balance=m[6])),
    ("nach", "debit", re.compile(
        rf"A/C X+(\d+) has a debit by NACH of Rs {_N} on {_D}\. Avl Bal Rs {_N}", re.I),
     lambda m: dict(acct=m[1], amount=m[2], date=m[3], balance=m[4])),
    ("upi", None, re.compile(
        rf"A/C X+(\d+) has (credit|debit) for (.*?) of Rs {_N} on {_D}\. Avl Bal Rs {_N}", re.I),
     lambda m: dict(acct=m[1], direction=m[2].lower(), payee=m[3], amount=m[4], date=m[5], balance=m[6])),
    ("neft", "debit", re.compile(
        rf"A/c Debited: X+(\d+) Date: {_D} UTR No\.: (\S+) Beneficiary Name: (.*?) Beneficiary A/c No\.: X*(\d+)"
        rf".*?Amount Remitted: INR {_N}", re.I),
     lambda m: dict(acct=m[1], date=m[2], ref=m[3], payee=m[4], amount=m[6])),
    ("neft", "credit", re.compile(
        rf"Credited to Your A/c: X+(\d+) Amount: INR {_N} UTR No\.: (\S+) Date: {_D}", re.I),
     lambda m: dict(acct=m[1], amount=m[2], ref=m[3], date=m[4])),
    ("transfer", "debit", re.compile(
        rf"Ac X+(\d+) debited for SI Fund Transfer of Rs\.?{_N} to (.*?) Ac X*\d+ on {_D} Ref (\S+?)\.", re.I),
     lambda m: dict(acct=m[1], amount=m[2], payee=m[3], date=m[4], ref=m[5])),
]


def parse_alert(sender: str, text: str) -> BankAlert | None:
    """Parse one alert body (already tag-stripped). Returns None for anything that is not a transaction."""
    s = (sender or "").lower()
    if "hdfc" in s:
        bank, table = "HDFC", _HDFC
    elif "sbi" in s:
        bank, table = "SBI", _SBI
    else:
        return None
    for kind, direction, rx, pick in table:
        m = rx.search(text)
        if not m:
            continue
        f = pick(m)
        try:
            when = _date(f["date"])
        except ValueError:
            continue
        acct = f.get("acct")
        return BankAlert(
            bank=bank, account_last4=acct[-4:] if acct else None,
            direction=f.get("direction") or direction, amount=_amt(f["amount"]), txn_date=when, kind=kind,
            payee=(f.get("payee") or "").strip()[:200] or None, vpa=f.get("vpa"),
            ref=(f.get("ref") or "").strip()[:80] or None,
            balance_after=_amt(f["balance"]) if f.get("balance") else None,
            narration=text[m.start():m.end()][:300],
        )
    return None
