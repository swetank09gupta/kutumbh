"""Narration helpers shared by de-duplication, transfer pairing and tagging."""
from __future__ import annotations

import re
from typing import Iterable, Optional

# SBI prints "UPI/DR/<utr>/<PAYEE truncated to ~8 chars>/<bank>/<vpa>/<note>": the handle (vpa) is
# what tells "Bank Acc" payees or "DRAVIT G" apart, so it is part of the key when it has letters.
_SBI_UPI = re.compile(r"^UPI/(?:DR|CR)/\d+/([^/]+)/[^/]*/([^/]*)/", re.I)

_PAYEE_PATTERNS = [
    re.compile(r"^UPI-([^-]+)-", re.I),                              # HDFC: UPI-<PAYEE>-<vpa>-<ifsc>-<utr>
    re.compile(r"^IMPS[-/](?:P2A|P2P|P2M)?/?-?\d+[-/]([^-/]+)", re.I),
    re.compile(r"^NEFT\s*(?:CR|DR)?-?[A-Z0-9]+-([^-]+)", re.I),
]


def payee_key(description: Optional[str]) -> Optional[str]:
    """
    Stable identity of the counterparty in a UPI/IMPS/NEFT narration, or None when the
    narration has no usable payee (masked accounts, bare references).
    "UPI-MEGHRAJ CHHETRI-Q437..." and "UPI-MEGHRAJCHHETRI-Q437..." give the same key.
    """
    if not description:
        return None
    d = description.strip()
    m = _SBI_UPI.search(d)
    if m:
        name = re.sub(r"[^A-Z]", "", m.group(1).upper())
        handle = re.sub(r"[^A-Z]", "", m.group(2).split("@")[0].upper())[:10]
        if len(name) >= 4 and name.count("X") < len(name) / 2:
            return (name + ("|" + handle if len(handle) >= 4 else ""))[:60]
        return None
    for pat in _PAYEE_PATTERNS:
        m = pat.search(d)
        if m:
            letters = re.sub(r"[^A-Z]", "", m.group(1).upper())
            if len(letters) >= 4 and letters.count("X") < len(letters) / 2:
                return letters[:40]
    return None


_DIGITS = re.compile(r"\d{9,}")


def utr_prefixes(*texts: Optional[str]) -> set[str]:
    """
    First 9 significant digits of every 9+ digit run (UTR / reference) in the texts.
    The same UPI payment is printed as '121775804732', '00001217758047', 'SB0 0000121775804732'
    by different statement layouts; leading zeros and trailing tails differ, the head does not.
    """
    out: set[str] = set()
    for t in texts:
        if not t:
            continue
        for run in _DIGITS.findall(re.sub(r"[ \t]", "", t)):
            sig = run.lstrip("0")
            if len(sig) >= 9:
                out.add(sig[:9])
    return out


def share_utr(a: Iterable[Optional[str]], b: Iterable[Optional[str]]) -> bool:
    return bool(utr_prefixes(*a) & utr_prefixes(*b))


def is_family_payee(description: Optional[str], family_names: Iterable[str]) -> bool:
    """True when the narration names a family member whose money moves through the household accounts (parents: cheque deposits,
    RTGS/NEFT). Matched on the whole name with everything but letters removed, so "CHQ DEP … PRADEEP KUMAR GUPTA DAYA GUPTA" and
    "NEFT-PRADEEPKUMARGUPTA" both match and a stranger sharing a first name or the surname does not."""
    text = re.sub(r"[^A-Z]", "", (description or "").upper())
    for name in family_names:
        key = re.sub(r"[^A-Z]", "", (name or "").upper())
        if len(key) >= 8 and key in text:
            return True
    return False


def is_own_payee(description: Optional[str], owner_names: Iterable[str]) -> bool:
    """
    True when the counterparty is one of the household's own names ("UPI-SWETANK GUPTA-…",
    "UPI/CR/…/SWETANK /CITI/swetank.en/…"): money moved between own accounts, including accounts
    whose statements we do not have. Needs the first name as the payee and either the surname
    or the first name in the UPI handle, so a stranger sharing a first name is not caught.
    """
    key = payee_key(description)
    if not key:
        return False
    payee, _, handle = key.partition("|")
    for full in owner_names:
        parts = [re.sub(r"[^A-Z]", "", p.upper()) for p in full.split() if p.strip()]
        if len(parts) < 2 or len(parts[0]) < 4:
            continue
        first, last = parts[0], parts[-1]
        if payee.startswith(first) and (last in payee or first in handle or handle.startswith(first[:6])):
            return True
    return False
