"""
Registrar mails that prove how shares arrived without a contract note.

  IPO "Allotment advice cum Unblocking Intimation": shares allotted, issue price, credit date, demat client id
  "Allotment of Bonus Equity Shares":              shares held on the record date, bonus shares credited, demat client id

Neither names an ISIN. app.ingestion.registrar_fetcher matches each to the depository credit of the same quantity in the same demat,
which is what identifies the stock. Pure functions over the cleaned mail text.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Optional


@dataclass
class IpoAllotment:
    applied: int
    allotted: int
    price: Decimal
    credited_on: date
    demat_id: str


@dataclass
class BonusAllotment:
    held: int
    bonus: int
    demat_id: str


def clean(html_or_text: str) -> str:
    t = re.sub(r"<(style|script).*?</\1>", " ", html_or_text, flags=re.S | re.I)
    t = re.sub(r"<[^>]+>", " ", t).replace("&nbsp;", " ").replace("&amp;", "&")
    return re.sub(r"\s+", " ", t).strip()


def parse_ipo_allotment(text: str) -> Optional[IpoAllotment]:
    t = clean(text)
    if not re.search(r"Allotment advice cum Unblocking", t, re.I):
        return None
    price = re.search(r"Issue Price per Security \(Rs\.?\)\s*([\d,]+(?:\.\d+)?)", t, re.I)
    applied = re.search(r"Securities Applied For\s*(\d+)", t, re.I)
    allotted = re.search(r"Securities Allotted\s*(\d+)", t, re.I)
    credited = re.search(r"credited on\s*(\d{2}/\d{2}/\d{4})", t, re.I)
    demat = re.search(r"Client ID\s*(\d{16})", t, re.I) or re.search(r"\b(\d{16})\b", t)
    if not (price and allotted and credited and demat) or int(allotted.group(1)) <= 0:
        return None                                              # an unsuccessful application allots nothing
    return IpoAllotment(int(applied.group(1)) if applied else int(allotted.group(1)), int(allotted.group(1)),
                        Decimal(price.group(1).replace(",", "")), datetime.strptime(credited.group(1), "%d/%m/%Y").date(), demat.group(1))


def parse_bonus(text: str) -> Optional[BonusAllotment]:
    t = clean(text)
    if not re.search(r"Allotment of Bonus", t, re.I) and not re.search(r"bonus equity shares", t, re.I):
        return None
    m = re.search(r"Bonus Shares allotted and credited in demat account\s*(\d{16})\s*(\d+)\s+(\d+)", t, re.I)
    if not m:
        return None
    return BonusAllotment(int(m.group(2)), int(m.group(3)), m.group(1))
