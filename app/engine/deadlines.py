"""
Deadlines and dates that matter, derived from what is held and from the public rules (app/knowledge/rules.yml).

Every item says why it is there: the registry rules it applies and/or the evidence it was read from (a statement,
a lender letter). Nothing is guessed: an account whose opening date we do not know is listed under `gaps`, not given a date.
Early-exit dates for SGBs are deliberately absent (decided 12 Oct 2026): only the maturity is shown.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.knowledge import rules
from app.ledger.sgb import tranche

HORIZON_DAYS = 365
LOOKBACK_DAYS = 30          # a deposit that matured this recently and is still listed is worth a look


# ── pure date rules ──────────────────────────────────────────────────────────
def fy_bounds(today: date) -> tuple[date, date]:
    start = date(today.year if today.month >= 4 else today.year - 1, 4, 1)
    return start, date(start.year + 1, 3, 31)


def fy_label(start: date) -> str:
    return f"FY{start.year}-{str(start.year + 1)[2:]}"


def next_cutoff(today: date, day: int) -> date:
    """The next date (today counts) on which a PPF deposit made that day still earns the month's interest."""
    if today.day <= day:
        return today.replace(day=day)
    y, m = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
    return date(y, m, day)


def ppf_maturity(open_date: date, years: int) -> date:
    """Matures `years` complete financial years after the end of the FY of opening (31 March)."""
    fy_end = date(open_date.year + (1 if open_date.month >= 4 else 0), 3, 31)
    return date(fy_end.year + years, 3, 31)


def add_years(d: date, years: int) -> date:
    try:
        return d.replace(year=d.year + years)
    except ValueError:                                   # 29 Feb
        return d.replace(year=d.year + years, day=28)


def elss_unlocks(trades: list[tuple[date, str, Decimal, Decimal]], years: int) -> list[tuple[date, Decimal, Decimal]]:
    """[(unlock date, units, cost)] for the instalments still held. `trades` = (date, 'B'|'S', units, amount).
    Sales consume the oldest units first (FIFO); a unit inside its lock-in cannot have been sold."""
    lots = [[d, q, a] for d, side, q, a in sorted(trades, key=lambda t: t[0]) if side == "B"]
    sold = sum((q for _d, side, q, _a in trades if side == "S"), Decimal(0))
    for lot in lots:
        take = min(sold, lot[1])
        if take:
            lot[2] = lot[2] * (lot[1] - take) / lot[1]
            lot[1] -= take
            sold -= take
    return [(add_years(d, years), q, a) for d, q, a in lots if q > 0]


# ── items ────────────────────────────────────────────────────────────────────
def _item(when: date, today: date, kind: str, owner: str, title: str, detail: str, *, rule_ids=(), evidence="",
          status: str | None = None) -> dict:
    days = (when - today).days
    return {"date": when.isoformat(), "days": days, "kind": kind, "owner": owner, "title": title, "detail": detail,
            "rules": list(rule_ids), "evidence": evidence,
            "status": status or ("today" if days == 0 else "past" if days < 0 else "soon" if days <= 30 else "later")}


def _pct(x) -> str:
    return format(Decimal(str(x)).normalize(), "f")


def _money(x) -> str:
    return f"₹{float(x):,.0f}"


def build(db: Session, today: date | None = None, horizon_days: int = HORIZON_DAYS) -> dict:
    from app.engine.deposits import deposit_history, is_live, maturity_value, table_date
    from app.engine.networth import real_owners
    from app.models.schemas import DepositSnapshot, InstrumentTrade, LoanAnchor, ManualAsset, ManualAssetContribution

    today = today or date.today()
    limit_day = today + timedelta(days=horizon_days)
    items: list[dict] = []
    gaps: list[dict] = []
    owners = [o.id for o in real_owners(db)]
    series, newest = deposit_history(db)
    fy_start, fy_end = fy_bounds(today)

    # PPF: next useful deposit date, FY headroom, maturity
    cutoff_day = int(rules.value("ppf.interest_cutoff_day", today))
    ppf_limit = rules.value("ppf.annual_limit", today)
    for a in db.query(ManualAsset).filter(ManualAsset.asset_type == "ppf").order_by(ManualAsset.owner_id).all():
        if a.owner_id not in owners:
            continue
        paid = sum((c.amount for c in db.query(ManualAssetContribution).filter(
            ManualAssetContribution.asset_id == a.id, ManualAssetContribution.date >= fy_start,
            ManualAssetContribution.date <= fy_end)), Decimal(0))
        room = max(ppf_limit - paid, Decimal(0))
        label = a.name.replace(" (synthesised from transfers)", "")
        if room > 0:
            items.append(_item(
                next_cutoff(today, cutoff_day), today, "ppf_deposit", a.owner_id, f"{label}: deposit by the {cutoff_day}th",
                f"{_money(paid)} of {_money(ppf_limit)} recorded for {fy_label(fy_start)}, {_money(room)} still open "
                f"(deposits made from other accounts are not seen). A deposit after the {cutoff_day}th earns from next month.",
                rule_ids=("ppf.interest_cutoff_day", "ppf.annual_limit"),
                evidence=f"{label} deposits recorded this FY"))
            if fy_end <= limit_day:
                items.append(_item(
                    fy_end, today, "ppf_fy_end", a.owner_id, f"{label}: last day of {fy_label(fy_start)}",
                    f"{_money(room)} of the {_money(ppf_limit)} limit is unused so far; it cannot be carried over.",
                    rule_ids=("ppf.annual_limit",), evidence=f"{label} deposits recorded this FY"))
        opened = db.query(func.min(DepositSnapshot.open_date)).filter(
            DepositSnapshot.owner_id == a.owner_id, DepositSnapshot.name == a.name).scalar()
        if opened:
            mat = ppf_maturity(opened, int(rules.value("ppf.maturity_years", today)))
            if mat <= limit_day:
                items.append(_item(mat, today, "ppf_maturity", a.owner_id, f"{label} matures",
                                   "15 financial years after the year it was opened; extendable in 5-year blocks, with or without deposits.",
                                   rule_ids=("ppf.maturity_years",), evidence=f"opened {opened.isoformat()} per the bank statement"))
        else:
            gaps.append({"owner": a.owner_id, "what": f"{label}: opening date unknown, so the maturity date cannot be given"})

    # Fixed deposits still listed by the newest statement
    for a in db.query(ManualAsset).filter(ManualAsset.asset_type == "fd").order_by(ManualAsset.maturity_date).all():
        if a.owner_id not in owners or not a.maturity_date or not is_live(a, series, newest):
            continue
        if not (today - timedelta(days=LOOKBACK_DAYS) <= a.maturity_date <= limit_day):
            continue
        mv = maturity_value(a)
        pays = f"pays {_money(mv)} at maturity" if mv else f"principal {_money(a.invested_amount or a.current_value)}"
        late = a.maturity_date < today
        items.append(_item(
            a.maturity_date, today, "fd_maturity", a.owner_id, f"{a.name} " + ("matured" if late else "matures"),
            (f"The newest statement still lists it: confirm whether it renewed or was paid out. " if late else "") +
            f"It {pays}.", evidence=f"{a.institution or 'bank'} statement of {table_date(newest, a) or 'unknown date'}",
            status="past" if late else None))

    # ELSS instalments unlocking
    lock = int(rules.value("elss.lock_in_years", today))
    from app.engine.recommendations import _ELSS_NAME_PATTERNS
    by_fund: dict[tuple, list] = defaultdict(list)
    for t in db.query(InstrumentTrade).filter(InstrumentTrade.holding_type == "mutual_fund").all():
        name = (t.instrument_name or "").lower()
        if t.owner_id in owners and any(p in name for p in _ELSS_NAME_PATTERNS):
            by_fund[(t.owner_id, t.isin or " ".join(name.split()))].append((t.trade_date, t.side, t.quantity, t.amount or Decimal(0)))
    for (owner, _key), trades in sorted(by_fund.items(), key=lambda kv: kv[0][0]):
        names = {t.instrument_name for t in db.query(InstrumentTrade).filter(
            InstrumentTrade.owner_id == owner, InstrumentTrade.trade_date == trades[0][0]) if t.instrument_name}
        fund = sorted(names)[0].title() if names else "ELSS fund"
        monthly: dict[tuple, list] = defaultdict(lambda: [Decimal(0), Decimal(0), date.max])
        for when, units, cost in elss_unlocks(trades, lock):
            if today <= when <= limit_day:
                m = monthly[(when.year, when.month)]
                m[0] += units; m[1] += cost; m[2] = min(m[2], when)
        for (_y, _m), (units, cost, first) in sorted(monthly.items()):
            items.append(_item(first, today, "elss_unlock", owner, f"{fund}: instalments unlock",
                               f"{float(units):,.3f} units (cost {_money(cost)}) from instalments made three years earlier "
                               f"are free to redeem from {first.strftime('%d %b %Y')} (instalments unlock one by one through the month).",
                               rule_ids=("elss.lock_in_years",), evidence="allotment dates from Coin / contract notes"))

    # SGB: next coupon and maturity of every tranche held, in the demat or by hand (dates from the tranche registry)
    from app.ledger import sgb_evidence
    tenor_rule = rules.rule("sgb.tenor_years", today)
    for (owner, isin), units in sorted(sgb_evidence.held_units(db, owners).items()):
        t = tranche(isin)
        nxt = next((d for d in sgb_evidence.coupon_dates(t, t.maturity_date) if d > today), None)
        if nxt and nxt <= limit_day:
            items.append(_item(nxt, today, "sgb_coupon", owner, f"{t.series}: coupon due",
                               f"{float(units):g} g x ₹{float(t.nominal):,.0f} x 1.25% = {_money(t.coupon(units))}, credited to the bank "
                               "account linked to the demat within a few days.",
                               rule_ids=("sgb.coupon_rate",), evidence=f"{t.series} issued {t.issue_date.isoformat()}"))
        if today <= t.maturity_date <= limit_day:
            items.append(_item(t.maturity_date, today, "sgb_maturity", owner, f"{t.series} matures",
                               f"{float(units):g} g are redeemed at the IBJA average price of the three business days before; "
                               "interest is paid half-yearly until then.", rule_ids=(tenor_rule.id,),
                               evidence=f"issued {t.issue_date.isoformat()} ({t.series})"))
    items.sort(key=lambda i: (i["date"], i["owner"], i["kind"]))
    return {"as_of": today.isoformat(), "horizon_days": horizon_days, "items": items, "gaps": gaps}


def uncited(payload: dict, today: date | None = None) -> list[dict]:
    """Items with neither a resolvable registry rule nor evidence. Must be empty: nothing is shown without a basis."""
    bad = []
    for i in payload["items"]:
        ok_rules = all(_resolves(r, today) for r in i["rules"])
        if not ok_rules or not (i["rules"] or i["evidence"]):
            bad.append(i)
    return bad


def _resolves(rule_id: str, today: date | None) -> bool:
    try:
        rules.rule(rule_id, today)
        return True
    except rules.RuleNotInForce:
        return False
