"""
Product invariants: things that must be true across every page, whatever data arrives.

Each function returns a list of violations (empty = holds). They are used twice:
  * consistency.py runs them against the live database after every sync, and
  * tests/test_invariants_db.py runs them against a scratch database with a deliberately awkward household,
so a change that makes a page drop an asset, disagree with another page, or silently reinterpret a number
fails before it ships. Every invariant is listed, with its origin, in docs/INVARIANTS.md.
"""
from __future__ import annotations

import math
from datetime import date
from typing import Callable

from sqlalchemy.orm import Session

TOL = 1.0     # ₹1: below this it is rounding


def _owners(db: Session) -> list[str]:
    from app.engine.networth import real_owners
    return [o.id for o in real_owners(db)]


def _returns(db: Session, owner: str) -> dict:
    from app.engine.portfolio_xirr import compute_owner_returns
    return compute_owner_returns(db, owner)


# INV-01  Nothing vanishes: every manual investment that is held is a row on the Investments page.
def inv01_every_manual_investment_is_listed(db: Session) -> list[str]:
    from app.engine.portfolio_xirr import _INVESTING_ASSET_TYPES
    from app.models.schemas import ManualAsset
    out = []
    today = date.today()
    for o in _owners(db):
        listed = {m["name"] for m in _returns(db, o)["manual_assets"]}
        for a in db.query(ManualAsset).filter(ManualAsset.owner_id == o).all():
            if a.asset_type not in _INVESTING_ASSET_TYPES or a.asset_type == "real_estate":
                continue
            if (a.current_value or 0) <= 0 or (a.asset_type in ("fd", "rd") and a.maturity_date and a.maturity_date < today):
                continue
            if (a.name or a.asset_type) not in listed:
                out.append(f"{o}: {a.name} (₹{float(a.current_value):,.0f}) is held but not on the Investments page")
    return out


# INV-02  One total: the classes add up to the total value, and the allocation uses the same numbers.
def inv02_classes_add_up_to_total_value(db: Session) -> list[str]:
    out = []
    for o in _owners(db) + ["family"]:
        r = _returns(db, o)
        by_class = sum(float(c["current_all"]) for c in r["by_class"])
        if abs(by_class - float(r["total"]["value_all"])) > TOL:
            out.append(f"{o}: classes sum to ₹{by_class:,.0f} but the total value is ₹{r['total']['value_all']:,.0f}")
    return out


# INV-03  Investments agree with net worth for every class both pages show.
def inv03_investments_match_net_worth(db: Session) -> list[str]:
    from app.consistency import _latest_nw
    nw = _latest_nw(db)
    out = []
    for o in _owners(db):
        if o not in nw:
            continue
        by = {c["class"]: float(c["current_all"]) for c in _returns(db, o)["by_class"]}
        pairs = {"fd": by.get("fd", 0) + by.get("rd", 0), "ppf": by.get("ppf", 0),
                 "mf": by.get("mutual_fund", 0), "equity+bonds": by.get("equity", 0) + by.get("bonds", 0)}
        theirs = {"fd": float(nw[o].fd_value or 0), "ppf": float(nw[o].ppf_value or 0),
                  "mf": float(nw[o].mf_value or 0), "equity+bonds": float(nw[o].equity_value or 0)}
        for k in pairs:
            if abs(pairs[k] - theirs[k]) > TOL:
                out.append(f"{o}: {k} is ₹{pairs[k]:,.0f} on Investments but ₹{theirs[k]:,.0f} in net worth")
    return out


# INV-04  Family is the sum of its members, for values and for what was invested.
def inv04_family_is_the_sum_of_members(db: Session) -> list[str]:
    owners = _owners(db)
    fam = _returns(db, "family")["total"]
    members = [_returns(db, o)["total"] for o in owners]
    out = []
    for k in ("value_all", "invested", "proceeds", "current_all"):
        s = sum(float(m[k]) for m in members)
        if abs(s - float(fam[k])) > TOL:
            out.append(f"family {k} is ₹{float(fam[k]):,.0f} but its members add up to ₹{s:,.0f}")
    return out


# INV-05  The total is the sum of the class returns (deposit-only classes carry no return and are outside it).
def inv05_total_return_is_the_sum_of_classes(db: Session) -> list[str]:
    out = []
    for o in _owners(db) + ["family"]:
        r = _returns(db, o)
        counted = [c for c in r["by_class"] if c.get("invested") is not None]
        for k in ("invested", "proceeds"):
            s = sum(float(c[k] or 0) for c in counted)
            if abs(s - float(r["total"][k])) > TOL:
                out.append(f"{o}: classes' {k} add up to ₹{s:,.0f} but the total says ₹{float(r['total'][k]):,.0f}")
    return out


# INV-06  Every current holding is a row on Investments, at the same value as the holdings view.
def inv06_every_holding_is_listed(db: Session) -> list[str]:
    from app.engine.holdings_view import latest_holdings
    out = []
    for o in _owners(db):
        shown = {i["isin"]: float(i["current"]) for i in _returns(db, o)["instruments"]}
        for h in latest_holdings(db, owner_ids=[o]):
            v = float(h.current_value or 0)
            if v <= 0 or not h.isin:
                continue
            if h.isin not in shown:
                out.append(f"{o}: {h.instrument_name or h.isin} (₹{v:,.0f}) is held but missing from Investments")
            elif abs(shown[h.isin] - v) > TOL:
                out.append(f"{o}: {h.isin} is ₹{v:,.0f} in holdings but ₹{shown[h.isin]:,.0f} on Investments")
    return out


# INV-07  The loan in net worth is what the lender last said, rolled forward by EMIs — unless you typed a
#         newer figure yourself (a newer manual value always wins; see app.engine.loan).
def inv07_loan_follows_the_lender(db: Session) -> list[str]:
    import re
    from app.engine.loan import outstanding
    from app.models.schemas import LoanAnchor, ManualAsset
    out = []
    for (o,) in db.query(LoanAnchor.owner_id).distinct().all():
        res = outstanding(db, o)
        a = db.query(ManualAsset).filter(ManualAsset.owner_id == o, ManualAsset.asset_type == "liability",
                                         ManualAsset.institution == "HDFC").first()
        if not (res and a):
            continue
        m = re.search(r"as of (\d{4}-\d{2}-\d{2})", a.notes or "")
        known = a.source_statement_date or (date.fromisoformat(m.group(1)) if m else None)
        if (known is None or res["as_of"] > known) and abs(abs(float(a.current_value or 0)) - float(res["principal"])) > TOL:
            out.append(f"{o}: loan row (as of {known}) says ₹{abs(float(a.current_value)):,.0f}, "
                       f"the lender's letters (as of {res['as_of']}) say ₹{float(res['principal']):,.0f}")
    return out


# INV-08  No payload carries NaN / infinity (the UI would print them or break).
def inv08_payloads_are_finite(db: Session) -> list[str]:
    def walk(x, path):
        if isinstance(x, float) and not math.isfinite(x):
            yield path
        elif isinstance(x, dict):
            for k, v in x.items():
                yield from walk(v, f"{path}.{k}")
        elif isinstance(x, (list, tuple)):
            for i, v in enumerate(x):
                yield from walk(v, f"{path}[{i}]")
    out = []
    for o in _owners(db) + ["family"]:
        out += [f"returns[{o}]{p}" for p in walk(_returns(db, o), "")]
    return out


# INV-09  The Manual Assets page shows the same value as net worth for deposits and PPF.
def inv09_manual_assets_match_net_worth(db: Session) -> list[str]:
    from app.api.routes.manual import _value_today
    from app.consistency import _latest_nw
    from app.models.schemas import ManualAsset
    nw = _latest_nw(db)
    out = []
    today = date.today()
    for o in _owners(db):
        if o not in nw:
            continue
        rows = db.query(ManualAsset).filter(ManualAsset.owner_id == o).all()
        fd = sum(_value_today(a) for a in rows if a.asset_type in ("fd", "rd") and not (a.maturity_date and a.maturity_date < today))
        ppf = sum(_value_today(a) for a in rows if a.asset_type == "ppf")
        for label, mine, theirs in (("deposits", fd, float(nw[o].fd_value or 0)), ("PPF", ppf, float(nw[o].ppf_value or 0))):
            if abs(mine - theirs) > TOL:
                out.append(f"{o}: {label} are ₹{mine:,.0f} on Manual Assets but ₹{theirs:,.0f} in net worth")
    return out


INVARIANTS: list[tuple[str, Callable[[Session], list[str]]]] = [
    ("INV-01 every manual investment is on the Investments page", inv01_every_manual_investment_is_listed),
    ("INV-02 classes add up to the total value", inv02_classes_add_up_to_total_value),
    ("INV-03 Investments match net worth", inv03_investments_match_net_worth),
    ("INV-04 family is the sum of its members", inv04_family_is_the_sum_of_members),
    ("INV-05 the total return is the sum of the classes", inv05_total_return_is_the_sum_of_classes),
    ("INV-06 every holding is listed at its value", inv06_every_holding_is_listed),
    ("INV-07 the loan follows the lender", inv07_loan_follows_the_lender),
    ("INV-08 payloads are finite", inv08_payloads_are_finite),
    ("INV-09 Manual Assets match net worth", inv09_manual_assets_match_net_worth),
]
