"""
Bank alert e-mails (HDFC Insta Alerts, SBI CBSSBI / NEFT / YONO / debit-card ATM, Axis UPI / NEFT) → one small record each.

Alerts arrive the same minute as the transaction, weeks before the statement, so they are the
only early evidence of what happened. They are evidence, never cashflow: app.txn.alert_recon
matches them against statement rows and reports what is still pending or missing.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

SENDERS = ("hdfcbank.bank.in", "hdfcbank.net", "alerts.sbi.bank.in", "alerts.sbi.co.in", "sbi.co.in", "alerts@axisbank.com", "alerts@axis.bank.in", "mailer.zerodha.com")


@dataclass
class BankAlert:
    bank: str                     # HDFC | SBI | AXIS
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
    for fmt in ("%d-%m-%y", "%d-%m-%Y", "%d-%b-%Y", "%d-%B-%Y", "%d/%m/%y", "%d/%m/%Y", "%b %d, %Y"):
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
    # Debit-card alert for an ATM cash withdrawal. A purchase prints the card's last four, not the account's: skipped.
    ("atm", "debit", re.compile(
        rf"Date & Time ([A-Za-z]{{3}} \d\d, \d{{4}}), [\d:]+ Transaction Number (\d+) Amount \(INR\) {_N} "
        rf"Last 4 Digit of Account X*(\d{{4}}) Transaction Type CASH WITHDRAWAL Channel ATM Available Balance \(INR\) {_N}", re.I),
     lambda m: dict(date=m[1], ref=m[2], amount=m[3], acct=m[4], balance=m[5])),
    # YONO / mobile-banking NEFT: the mail carries no date (the day it was sent is the day) and its TID is not the UTR, so no
    # ref: a "NEFT Transaction" mail for the same transfer, which has the UTR, then counts as the original and this as its copy.
    ("neft", "debit", re.compile(
        rf"NEFT transfer to (.*?) for Rs\.?{_N} from A/C no\. X+(\d+) has been completed successfully", re.I),
     lambda m: dict(payee=m[1], amount=m[2], acct=m[3], date=None)),
]

# Axis: the new "summary" alert and the older "A/c no. … has been debited with …" wording; both carry UPI/NEFT info, no balance.
_AD = r"(\d\d-\d\d-\d{2,4})"
_AXIS = [
    ("upi", None, re.compile(
        rf"Amount (Debited|Credited): INR {_N} Account Number: X*(\d+) Date & Time: {_AD}, [\d:]+ IST "
        rf"Transaction Info: (.*?)(?: If this transaction| Feel free| Call us| $)", re.I),
     lambda m: dict(direction="debit" if m[1].lower().startswith("deb") else "credit", amount=m[2], acct=m[3], date=m[4], info=m[5])),
    ("upi", None, re.compile(
        rf"A/c no\. X*(\d+) has been (debited|credited) with INR {_N} on {_AD} [\d:]+ IST by (\S+)", re.I),
     lambda m: dict(direction="debit" if m[2].lower().startswith("deb") else "credit", amount=m[3], acct=m[1], date=m[4], info=m[5])),
]


def _axis_info(info: str) -> tuple[str, str | None, str | None]:
    """'UPI/P2M/548863353225/FLIPKART PAYMENTS' → (kind, ref, payee); 'NEFT/MB/AXOMB12253386885/A' likewise."""
    parts = [p.strip() for p in (info or "").split("/") if p.strip()]
    kind = "upi" if parts[:1] == ["UPI"] else "neft" if parts[:1] == ["NEFT"] else "other"
    ref = next((p for p in parts[1:] if re.fullmatch(r"[A-Za-z0-9]{10,}", p)), None)
    payee = next((p for p in reversed(parts[2:]) if p != ref and not re.fullmatch(r"[A-Za-z0-9]{10,}", p)), None)
    return kind, ref, payee


_TDS = re.compile(rf"TDS of INR {_N} deducted for interest paid on A/C No X*(\d+)", re.I)


def parse_tds(sender: str, text: str) -> tuple[str, Decimal] | None:
    """SBI's "TDS of INR 73.00 deducted for interest paid on A/C No XXXXX826925": (last 4 digits of the deposit, amount).
    Not a transaction: tax deducted at source from the interest, which is why a deposit's credits are net."""
    if "sbi" not in (sender or "").lower():
        return None
    m = _TDS.search(text)
    return (m.group(2)[-4:], _amt(m.group(1))) if m else None


UNKNOWN_BANK = "?"          # a Zerodha payment mail names the account's last four digits but not its bank: the fetcher resolves it from the alerts already stored
_ZERODHA = re.compile(rf"₹\s*{_N} has been deposited to Zerodha (?:equity|commodity) from account X*(\d+) through UPI on (\d{{1,2}} [A-Za-z]{{3}} \d{{4}})\. "
                      rf"Your transaction Reference number is (\d+)", re.I)


def parse_zerodha_payment(text: str) -> BankAlert | None:
    """Zerodha's "Payment success" mail: money put into the broker by UPI from the account ending ... (SBI sends no mail for a UPI payment, so this is
    the only same-day trace of it). A debit to the broker: the UPI reference is the key the statement row carries ("UPI/DR/<ref>/ZERODHA ...")."""
    m = _ZERODHA.search(text)
    if not m:
        return None
    try:
        when = datetime.strptime(m.group(3), "%d %b %Y").date()
    except ValueError:
        return None
    return BankAlert(bank=UNKNOWN_BANK, account_last4=m.group(2)[-4:], direction="debit", amount=_amt(m.group(1)), txn_date=when, kind="upi",
                     payee="ZERODHA", ref=m.group(4), narration=text[m.start():m.end()][:300])


def parse_alert(sender: str, text: str, sent: date | None = None) -> BankAlert | None:
    """Parse one alert body (already tag-stripped). Returns None for anything that is not a transaction.
    `sent` is the day the mail was sent, used by the one template (YONO NEFT) whose body carries no date."""
    s = (sender or "").lower()
    if "zerodha" in s:
        return parse_zerodha_payment(text)
    if "hdfc" in s:
        bank, table = "HDFC", _HDFC
    elif "sbi" in s:
        bank, table = "SBI", _SBI
    elif "alerts@axis" in s:                      # not Straight2axis@ payment advices
        bank, table = "AXIS", _AXIS
    else:
        return None
    for kind, direction, rx, pick in table:
        m = rx.search(text)
        if not m:
            continue
        f = pick(m)
        try:
            when = _date(f["date"]) if f.get("date") else sent
        except ValueError:
            continue
        if when is None:
            continue
        if f.get("info") is not None:
            kind, f["ref"], f["payee"] = _axis_info(f["info"])
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
