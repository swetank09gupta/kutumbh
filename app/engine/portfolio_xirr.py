"""
Returns (XIRR / CAGR / gain) for every cut of the portfolio — total, asset class, goal, instrument.

ONE definition: build_positions() turns the portfolio into positions (each current holding, each
closed holding, each manual investment) with their own dated cash flows. Every cut is a sum over
the same positions, so the class rows add up to the total and a goal can never disagree with the
instruments inside it.

Cost coverage: a position whose purchase history is incomplete (units that arrived with no trade
on file) is *left out of the return and said so* — never guessed. The numbers describe the covered
share of the value; `coverage_pct` and the note say how much that is.

Sign convention: buy / contribution -> outflow (negative); sell / redeem / dividend -> inflow;
the current value of a held position is a terminal inflow today.
"""
from __future__ import annotations

import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Optional

from sqlalchemy.orm import Session

from app.engine.holdings_view import latest_holdings as _latest_holdings
from app.engine.xirr_engine import xirr, cagr
from app.models.schemas import (
    Account, InstrumentTrade, ManualAsset, ManualAssetContribution, Transaction,
)

logger = logging.getLogger(__name__)

ZERO = Decimal(0)

# Manual asset types that are real investments (the rest, e.g. liabilities, are skipped).
_INVESTING_ASSET_TYPES = {
    "fd", "rd", "ppf", "nps",
    "gold", "silver", "sgb",
    "real_estate", "insurance", "other",
}


def _manual_asset_cashflows(
    db: Session, asset: ManualAsset, today: date,
) -> Optional[tuple[list[tuple[date, float]], Decimal, Decimal]]:
    """
    Returns (cashflows, invested, current_value) for a manual asset, or None if
    the asset has no usable cashflow data (excluded from XIRR).

    Cashflow construction:
        1. If contribution rows exist → each row is an outflow on its date.
        2. Else if start_date + invested_amount → a single outflow on start_date.
        3. Else → return None (asset is excluded with a warning).

        current_value is appended as a terminal inflow on `today`.
    """
    contribs = (
        db.query(ManualAssetContribution)
        .filter(ManualAssetContribution.asset_id == asset.id)
        .order_by(ManualAssetContribution.date)
        .all()
    )

    cfs: list[tuple[date, float]] = []
    invested = ZERO

    if contribs:
        for c in contribs:
            amt = float(c.amount or 0)
            if amt <= 0:
                continue
            cfs.append((c.date, -amt))
            invested += Decimal(str(amt))
    elif asset.start_date and asset.invested_amount and asset.invested_amount > 0:
        amt = float(asset.invested_amount)
        cfs.append((asset.start_date, -amt))
        invested = Decimal(str(amt))
    else:
        logger.info(
            "Skipping manual asset '%s' (%s): no start_date or contributions",
            asset.name, asset.asset_type,
        )
        return None

    current = asset.current_value or ZERO

    # ── FD / RD interest accrual ──────────────────────────────────────────────
    # The SBI parser stores current_value = principal (no interest growth) for
    # term deposits. Compound the principal at the FD's stated rate from
    # start_date to min(today, maturity_date) so XIRR sees real growth.
    # PPF is typically already credited correctly via the year-end snapshot.
    if asset.asset_type in ("fd", "rd") \
            and asset.start_date and asset.interest_rate \
            and asset.invested_amount and asset.invested_amount > 0:
        principal = float(asset.invested_amount)
        rate      = float(asset.interest_rate) / 100.0   # decimal, e.g. 0.065
        end_date  = asset.maturity_date if (asset.maturity_date and asset.maturity_date < today) else today
        years_elapsed = max(0.0, (end_date - asset.start_date).days / 365.25)
        # Quarterly compounding — matches SBI's standard FD interest convention.
        n = 4
        synthetic = principal * (1.0 + rate / n) ** (n * years_elapsed)
        synthetic_dec = Decimal(f"{synthetic:.2f}")
        # Use the synthesized value if it's higher than the parser-provided one
        # (which is usually just the principal). If a fresh statement has reported
        # a higher current_value already, keep that.
        current = max(current, synthetic_dec)

    if current > 0:
        cfs.append((today, float(current)))

    return cfs, invested, current


@dataclass
class Position:
    owner: str
    isin: Optional[str]
    name: str
    cls: str                          # equity | mutual_fund | bonds | fd | ppf | ...
    current: float                    # value today (0 for a closed position)
    cfs: list = field(default_factory=list)   # (date, amount) trades/contributions; no terminal value
    goal_tag: Optional[str] = None
    covered: bool = True              # purchase history complete -> counts in returns
    units: float = 0.0
    invested_remaining: Optional[float] = None   # cost of the units still held
    holding: object = None            # the holdings-view row, for CAGR inputs
    closed: bool = False
    deposit: bool = False             # FD / RD: earns its stated rate, so no return is derived from it
    rate: Optional[float] = None      # stated interest rate, manual investments
    maturity: Optional[date] = None
    contract: bool = False            # rate implied by the statement's maturity value (not typed, not printed)
    why: Optional[str] = None         # why no return could be measured
    terminal: Optional[tuple] = None  # (date, value) the return is measured to, when that is not today (PPF: last 31 March)


def _asset_class(isin: str, holding_type: Optional[str] = None) -> str:
    if holding_type:
        return holding_type
    return "mutual_fund" if isin.startswith("INF") else "bonds" if isin.startswith("IN0") else "equity"


_FD_INT = re.compile(r"INT\s*TRF\s*FRM\s*(\d{6,})\s*TO", re.I)


def _fd_tds(db: Session, owner_ids: list[str]) -> dict[tuple[str, str], list[tuple[date, float]]]:
    """Tax deducted at source from each deposit's interest: {(owner, last4): [(date, amount)]}."""
    from app.models.schemas import TdsDeduction
    out: dict[tuple[str, str], list[tuple[date, float]]] = defaultdict(list)
    for r in db.query(TdsDeduction).filter(TdsDeduction.owner_id.in_(owner_ids)).all():
        out[(r.owner_id, r.account_last4)].append((r.deducted_on, float(r.amount)))
    return out


def _fd_interest_credits(db: Session, owner_ids: list[str]) -> dict[tuple[str, str], list[tuple[date, float]]]:
    """Interest each SBI deposit actually paid into savings, found by the deposit's account number in the
    narration ("INT TRF FRM 43960134737 TO 30778223316"): {(owner, last4 of the deposit): [(date, amount)]}."""
    out: dict[tuple[str, str], list[tuple[date, float]]] = defaultdict(list)
    q = (db.query(Transaction.date, Transaction.amount, Transaction.raw_description, Account.owner_id)
         .join(Account, Account.id == Transaction.account_id)
         .filter(Account.owner_id.in_(owner_ids), Account.account_type == "bank",
                 Transaction.transaction_type == "credit", Transaction.raw_description.ilike("%INT TRF FRM%")))
    for d, amt, desc, owner in q.all():
        m = _FD_INT.search(desc or "")
        if m and amt and amt > 0:
            out[(owner, m.group(1)[-4:])].append((d, float(amt)))
    return out


_FD_BREAK = re.compile(r"SWEEP\s*TRF\s*CREDT|SWEEP\s*FROM|SWEEP\s*DEPOSIT|\bTDR\b|PREMAT|MATUR", re.I)


def _fd_breaks(db: Session, owner_ids: list[str]) -> dict[str, list[tuple[date, Decimal]]]:
    """Money that came back into savings from a deposit (sweep / TDR credits), to date a part-withdrawal."""
    out: dict[str, list[tuple[date, Decimal]]] = defaultdict(list)
    q = (db.query(Transaction.date, Transaction.amount, Transaction.raw_description, Account.owner_id)
         .join(Account, Account.id == Transaction.account_id)
         .filter(Account.owner_id.in_(owner_ids), Account.account_type == "bank", Transaction.transaction_type == "credit"))
    for d, amt, desc, owner in q.all():
        if amt and amt > 0 and _FD_BREAK.search(desc or ""):
            out[owner].append((d, Decimal(amt)))
    return out


def build_positions(db: Session, owner_ids: list[str], today: date) -> tuple[list[Position], list[dict]]:
    """Every position of these owners with its cash flows, plus what was skipped and why."""
    skipped: list[dict] = []
    holdings = _latest_holdings(db, owner_ids=owner_ids)

    # Cash flows per (owner, isin): every transaction tagged with an ISIN, whichever account it sits in —
    # a SIP mandate debit arrives on the BANK statement (decision 23, ADR-0013). Only DEMAT unit-count
    # rows are excluded.
    flows: dict[tuple[str, str], list[tuple[date, float]]] = defaultdict(list)
    rows = (db.query(Transaction, Account.owner_id)
            .join(Account, Account.id == Transaction.account_id)
            .filter(Account.owner_id.in_(owner_ids), Transaction.isin.isnot(None))
            .order_by(Transaction.date).all())
    for t, owner in rows:
        amt = float(t.amount or 0)
        if amt <= 0 or (t.description or "").upper().startswith("DEMAT "):
            continue                                   # DEMAT rows hold a unit count, not rupees
        if t.transaction_type == "debit":
            flows[(owner, t.isin)].append((t.date, -amt))
        elif t.transaction_type == "credit":
            flows[(owner, t.isin)].append((t.date, amt))

    positions: list[Position] = []
    held: set[tuple[str, str]] = set()
    for h in holdings:
        if not h.isin or float(h.current_value or 0) <= 0:
            continue
        key = (h.owner_id, h.isin)
        held.add(key)
        cfs = list(flows.get(key, []))
        quality = getattr(h, "cost_basis_quality", None)
        invested_rem = float(getattr(h, "invested_value", 0) or 0)
        if invested_rem <= 0 and h.avg_cost_nav and float(h.units or 0) > 0:
            invested_rem = float(h.avg_cost_nav) * float(h.units)
        has_buy = any(a < 0 for _, a in cfs)
        if quality == "exact" and not has_buy and invested_rem > 0:
            # cost known without a cash trade (an SGB at its RBI issue price): one dated outflow
            first = getattr(h, "first_buy_date", None) or today
            cfs = [(first, -invested_rem)] + cfs
            has_buy = True
        covered = quality == "exact" or (quality is None and has_buy)
        positions.append(Position(
            owner=h.owner_id, isin=h.isin, name=h.instrument_name or h.symbol or h.isin,
            cls=_asset_class(h.isin, h.holding_type), current=float(h.current_value), cfs=cfs,
            goal_tag=h.goal_tag, covered=covered, units=float(h.units or 0),
            invested_remaining=invested_rem if (covered and invested_rem > 0) else None, holding=h))

    # Positions that were bought and fully sold: realised history. Covered only when units bought equal
    # units sold: more bought than sold means units left with no sale on file (proceeds unknown), more
    # sold than bought means part of the cost lies before our history. Neither is guessed.
    bought: dict[tuple[str, str], Decimal] = defaultdict(lambda: ZERO)
    sold: dict[tuple[str, str], Decimal] = defaultdict(lambda: ZERO)
    names: dict[tuple[str, str], str] = {}
    types: dict[tuple[str, str], str] = {}
    for t in db.query(InstrumentTrade).filter(InstrumentTrade.owner_id.in_(owner_ids),
                                              InstrumentTrade.isin.isnot(None)).all():
        k = (t.owner_id, t.isin)
        (bought if t.side == "B" else sold)[k] += t.quantity or ZERO
        names[k] = t.instrument_name or t.symbol or t.isin
        types[k] = t.holding_type or ""
    for key, cfs in flows.items():
        if key in held:
            continue
        owner, isin = key
        tol = Decimal("0.05") if isin.startswith("INF") else Decimal("0.001")
        ok = abs(bought[key] - sold[key]) <= tol
        if not ok:
            skipped.append({"kind": "closed_position", "name": names.get(key, isin),
                            "reason": f"bought {bought[key]} units but sold {sold[key]} on file — "
                                      f"proceeds or cost missing, left out of returns"})
        positions.append(Position(owner=owner, isin=isin, name=names.get(key, isin),
                                  cls=_asset_class(isin, types.get(key) or None), current=0.0, cfs=list(cfs),
                                  covered=ok, closed=True))

    # Manual investments: FD, PPF, RD, NPS, gold ...
    from app.engine.deposits import deposit_history, is_live
    hist, newest = deposit_history(db)
    fd_interest = _fd_interest_credits(db, owner_ids)
    fd_breaks = _fd_breaks(db, owner_ids)
    fd_tds = _fd_tds(db, owner_ids)
    for asset in db.query(ManualAsset).filter(ManualAsset.owner_id.in_(owner_ids)).all():
        if asset.asset_type not in _INVESTING_ASSET_TYPES:
            continue
        if asset.asset_type in ("fd", "rd") and asset.maturity_date and asset.maturity_date < today:
            skipped.append({"kind": "manual_asset", "name": asset.name or asset.asset_type,
                            "reason": "matured before today"})
            continue
        if asset.asset_type in ("fd", "rd"):
            from app.engine.deposit_flows import fd_flows
            from app.engine.deposits import accrued_value, contract_rate_pct
            if not is_live(asset, hist, newest):
                skipped.append({"kind": "manual_asset", "name": asset.name,
                                "reason": "closed: the newest statement no longer lists it"})
                continue
            contract = contract_rate_pct(asset)               # from the statement's own maturity value, if cumulative
            rate = contract if contract is not None else (float(asset.interest_rate) if asset.interest_rate else None)
            current = float(accrued_value(asset, today))       # the same value net worth uses
            snaps = hist.get((asset.owner_id, asset.name), [])
            flows = [(d, float(a)) for d, a in fd_flows(snaps, asset.start_date, fd_breaks.get(asset.owner_id, []))] if snaps else []
            tail = re.search(r"(\d{4})\s*$", asset.name or "")
            credits = fd_interest.get((asset.owner_id, tail.group(1)), []) if tail else []
            first = min((d for d, _ in flows), default=None)
            months = ((today - first).days / 30.4) if first else 0
            # Credits to savings are NET of tax deducted at source; the return is quoted before tax (as for the PPF
            # and the funds), so the deducted tax is added back.
            paid = [(d, a) for d, a in credits if first and d > first] \
                + [(d, a) for d, a in (fd_tds.get((asset.owner_id, tail.group(1)), []) if tail else []) if first and d > first]
            n_credits = sum(1 for d, a in credits if first and d > first)
            if flows and contract is not None:
                # Cumulative deposit: its own statements give the opening, every part-withdrawal and top-up; the
                # interest stays inside it and is part of what it is worth today.
                positions.append(Position(owner=asset.owner_id, isin=None, name=asset.name or asset.asset_type,
                                          cls=asset.asset_type, current=current, cfs=flows, rate=rate,
                                          maturity=asset.maturity_date, contract=True))
            elif flows and months >= 6 and n_credits >= 0.8 * (months - 1):
                # Interest is paid out monthly: the same flows plus every interest credit it paid into savings.
                positions.append(Position(owner=asset.owner_id, isin=None, name=asset.name or asset.asset_type,
                                          cls=asset.asset_type, current=current, cfs=flows + paid, rate=rate,
                                          maturity=asset.maturity_date))
            else:
                why = ("no statement history for it" if not flows else
                       "history under six months" if months < 6 else
                       f"its interest is paid out monthly but only {n_credits} of about {max(0, int(months - 1))} credits are on file")
                positions.append(Position(owner=asset.owner_id, isin=None, name=asset.name or asset.asset_type,
                                          cls=asset.asset_type, current=current, covered=False, deposit=True,
                                          rate=rate, maturity=asset.maturity_date, contract=contract is not None,
                                          why=why))
            continue
        if asset.asset_type == "ppf" and not db.query(ManualAssetContribution).filter_by(asset_id=asset.id).first():
            from app.engine.deposit_flows import ppf_measure
            from app.engine.ppf_from_transfers import _ppf_contribution_txns_for, ppf_candidate_transfers
            snaps = [(d, p) for d, p, _a in hist.get((asset.owner_id, asset.name), [])]
            bank = [(d, a) for d, a, _desc, _inst in _ppf_contribution_txns_for(db, asset.owner_id)]
            m = ppf_measure(snaps, bank, maybe=ppf_candidate_transfers(db, asset.owner_id))
            if m:
                # Measured over whole financial years between 31-March balances (interest is credited only then);
                # what it is worth today is the statement balance.
                positions.append(Position(owner=asset.owner_id, isin=None, name=asset.name or asset.asset_type,
                                          cls="ppf", current=float(asset.current_value or 0),
                                          cfs=[(d, -float(a)) for d, a in m["flows"]], goal_tag=asset.goal_tag,
                                          terminal=(m["terminal"][0], float(m["terminal"][1])),
                                          rate=float(asset.interest_rate) if asset.interest_rate else None,
                                          maturity=asset.maturity_date))
                continue
        out = _manual_asset_cashflows(db, asset, today)
        if out is None:
            if asset.asset_type in ("ppf", "nps", "gold", "silver", "sgb"):
                # Held and valued, but no dated contributions: it stays in the class value instead of
                # vanishing from the page. PPF pays an administered rate, so like a deposit it is a balance;
                # the market-linked ones count as "no purchase history" in the coverage.
                positions.append(Position(owner=asset.owner_id, isin=None, name=asset.name or asset.asset_type,
                                          cls=asset.asset_type, current=float(asset.current_value or 0),
                                          covered=False, goal_tag=asset.goal_tag,
                                          deposit=asset.asset_type == "ppf",
                                          rate=float(asset.interest_rate) if asset.interest_rate else None,
                                          maturity=asset.maturity_date))
            else:
                skipped.append({"kind": "manual_asset", "name": asset.name or asset.asset_type,
                                "reason": "no start_date or contributions"})
            continue
        cfs, _inv, current = out
        if current > 0 and cfs:
            cfs = cfs[:-1]                              # drop the terminal value; summarise() adds it
        terminal = None
        if asset.asset_type == "ppf" and cfs:
            # PPF interest is credited on 31 March only: measure to the last 31 March (the credited balance then),
            # with the deposits made up to it; later deposits are in today's value but not yet in a return.
            from app.engine.small_savings import ppf_balance
            last = date(today.year, 3, 31) if today >= date(today.year, 3, 31) else date(today.year - 1, 3, 31)
            deposits = [(d, Decimal(str(-a))) for d, a in cfs if d <= last and a < 0]
            if deposits:
                bal, _ = ppf_balance(deposits, last + timedelta(days=1))
                cfs, terminal = [(d, a) for d, a in cfs if d <= last], (last, float(bal))
        positions.append(Position(owner=asset.owner_id, isin=None, name=asset.name or asset.asset_type,
                                  cls=asset.asset_type, current=float(current), cfs=cfs,
                                  goal_tag=asset.goal_tag if asset.asset_type != "real_estate" else None,
                                  rate=float(asset.interest_rate) if asset.interest_rate else None,
                                  maturity=asset.maturity_date, terminal=terminal))
    return positions, skipped


def _years(cfs: list[tuple[date, float]], today: date) -> float:
    return max(0.0, (today - min(d for d, _ in cfs)).days / 365.25) if cfs else 0.0


def _avg_holding_days(cfs: list[tuple[date, float]], today: date) -> float:
    """Capital-weighted holding period: sells consume the oldest buys first (by amount), the rest is held to today."""
    buys = [[d, -a] for d, a in sorted(cfs) if a < 0]
    total = sum(b[1] for b in buys)
    if total <= 0:
        return 0.0
    weighted = 0.0
    for d, a in sorted(cfs):
        if a <= 0:
            continue
        left = a
        while left > 1e-9 and buys:
            take = min(buys[0][1], left)
            weighted += take * max(0, (d - buys[0][0]).days)
            buys[0][1] -= take
            left -= take
            if buys[0][1] <= 1e-9:
                buys.pop(0)
    weighted += sum(b[1] * max(0, (today - b[0]).days) for b in buys)
    return weighted / total


# Returns for money held under a year are reported as plain gain, not annualised (the SEBI/AMFI
# convention): a 20% gain in 3 months is not "+110% a year", and 8 days is not "+4,000%".
_MIN_ANNUALISE_DAYS = 365


def summarise(positions: list[Position], today: date) -> dict:
    """Money-weighted return of the covered positions, and how much of the value that covers."""
    balances = sum(p.current for p in positions if p.deposit)          # deposits / PPF: valued, no return
    positions = [p for p in positions if not p.deposit]
    cov = [p for p in positions if p.covered]
    all_current = sum(p.current for p in positions)
    cov_current = sum(p.current for p in cov)
    cfs: list[tuple[date, float]] = []
    ret_current = 0.0                                  # value the return is measured to (today, or a PPF's last 31 March)
    for p in cov:
        cfs.extend(p.cfs)
        end, val = p.terminal if p.terminal else (today, p.current)
        ret_current += val
        if val > 0:
            cfs.append((end, val))
    invested = sum(-a for p in cov for _, a in p.cfs if a < 0)
    proceeds = sum(a for p in cov for _, a in p.cfs if a > 0)
    years = _years(cfs, today)
    profit = ret_current + proceeds - invested
    coverage = (cov_current / all_current) if all_current > 0 else None
    r = xirr(cfs) if invested > 0 else None
    per = [(sum(-a for _, a in p.cfs if a < 0), _avg_holding_days(p.cfs, p.terminal[0] if p.terminal else today))
           for p in cov if p.cfs]
    avg_days = (sum(w * d for w, d in per) / sum(w for w, _ in per)) if per and sum(w for w, _ in per) > 0 else 0.0
    churn = invested > 0 and avg_days < _MIN_ANNUALISE_DAYS
    if churn:
        r = None
    parts = []
    if all_current > 0 and cov_current < all_current and (all_current - cov_current) > all_current * 0.005:
        gap = all_current - cov_current
        parts.append(f"return covers {coverage * 100:.0f}% of ₹{all_current:,.0f} — ₹{gap:,.0f} has no purchase history yet"
                     if cov else f"₹{all_current:,.0f} held, no purchase history yet")
    if balances > 0 and positions:
        parts.append(f"plus ₹{balances:,.0f} held as a balance, return not measurable (total ₹{all_current + balances:,.0f})")
    if churn:
        parts.append("money was held under a year on average — shown as plain gain, not annualised")
    elif r is None and invested > 0:
        parts.append("no annual rate fits these flows; see gain")
    note = "; ".join(parts) or None
    return {
        "invested": round(invested, 2), "current": round(cov_current, 2), "current_all": round(all_current + balances, 2),
        "proceeds": round(proceeds, 2),
        "gain": round(profit, 2) if invested > 0 else None,
        "gain_pct": round(profit / invested * 100, 2) if invested > 0 else None,
        "years": round(years, 2), "xirr_pct": r,
        "cagr_pct": cagr(invested, cov_current, years) if (invested > 0 and cov_current > 0 and not proceeds) else None,
        "coverage_pct": round(coverage * 100, 1) if coverage is not None else None,
        "uncovered_current": round(all_current - cov_current, 2),
        "note": note,
    }


def compute_owner_returns(db: Session, owner_id: str, today: Optional[date] = None) -> dict:
    """Pass owner_id="family" to aggregate every real owner."""
    today = today or date.today()
    if owner_id == "family":
        from app.engine.networth import real_owners
        owner_ids = [o.id for o in real_owners(db)]
    else:
        owner_ids = [owner_id]
    positions, skipped = build_positions(db, owner_ids, today)

    by_cls: dict[str, list[Position]] = defaultdict(list)
    for p in positions:
        by_cls[p.cls].append(p)
    by_class = []
    for cls in sorted(by_cls):
        ps = by_cls[cls]
        if all(p.deposit for p in ps):
            value = round(sum(p.current for p in ps), 2)
            by_class.append({
                "class": cls, "count": len(ps), "invested": None, "current": value, "current_all": value,
                "proceeds": 0.0, "gain": None, "gain_pct": None, "years": None, "xirr_pct": None, "cagr_pct": None,
                "coverage_pct": None, "uncovered_current": 0.0, "cost_coverage_pct": None, "excluded_from_total": True,
                "note": ("earns the Government-notified PPF rate; contributions are not dated, so no return is measured"
                         if cls == "ppf" else
                         "balance at the deposit's own rate; no interest credits found to measure a return"),
                "exclusion_reason": None})
            continue
        row = summarise(ps, today)
        by_class.append({
            "class": cls, "count": sum(1 for p in ps if not p.closed), **row,
            # kept for the UI: the reason a figure is missing or partial
            "cost_coverage_pct": row["coverage_pct"], "exclusion_reason": row["note"],
            "excluded_from_total": False,
        })
    total = summarise([p for p in positions if not p.deposit], today)
    total["value_all"] = round(sum(p.current for p in positions), 2)          # everything held, balances included
    _describe_total(total, by_class)
    total["xirr_incl_balances_pct"] = _xirr_with_balances(positions, today)
    return {
        "owner_id": owner_id, "as_of": today.isoformat(), "total": total, "by_class": by_class,
        "instruments": _instruments(positions, today), "by_goal": _goals(positions, today),
        "manual_assets": _manual_rows(positions, today),
        "skipped": skipped,
    }


def _xirr_with_balances(positions: list[Position], today: date) -> Optional[float]:
    """
    The return of EVERYTHING held, counting the PPF / deposit balances whose own history is not on file at the
    rate they contractually earn (PPF: the Government-notified rate; deposit: the rate on its statement, or the one
    its maturity value implies). Each such balance enters as a year of growth at that rate. It is shown beside the
    measured return, never instead of it: it assumes a contract, it does not measure.
    """
    flows: list[tuple[date, float]] = []
    for p in positions:
        if p.deposit:
            if p.current > 0 and p.rate:
                flows += [(today - timedelta(days=365), -p.current / (1 + p.rate / 100)), (today, p.current)]
        elif p.covered:
            end, val = p.terminal if p.terminal else (today, p.current)
            flows += list(p.cfs) + ([(end, val)] if val > 0 else [])
    invested = sum(-a for _, a in flows if a < 0)
    return xirr(flows) if invested > 0 else None


def _describe_total(total: dict, by_class: list[dict]) -> None:
    """Say plainly what the headline return covers: the share of EVERYTHING held, and what is left out and why."""
    held, covered = total["value_all"], total["current"]
    total["coverage_pct"] = round(covered / held * 100, 1) if held > 0 else None
    total["uncovered_current"] = round(held - covered, 2)
    left = []
    for c in by_class:
        gap = (c.get("current_all") or 0) - (c.get("current") or 0)
        if gap > max(1.0, 0.005 * (c.get("current_all") or 0)):
            why = ("balance, no return measurable" if c["class"] in ("fd", "rd", "ppf")
                   else "no purchase history yet")
            left.append(f"₹{gap:,.0f} {_CLASS_NAMES.get(c['class'], c['class'])} ({why})")
    total["note"] = (f"Return covers ₹{covered:,.0f} of the ₹{held:,.0f} held ({total['coverage_pct']:.0f}%)"
                     + (". Left out: " + "; ".join(left) if left else "")) if held > 0 else None


_CLASS_NAMES = {"fd": "fixed deposits", "rd": "recurring deposits", "ppf": "PPF", "mutual_fund": "mutual funds",
                "equity": "equity", "bonds": "bonds", "nps": "NPS", "gold": "gold", "silver": "silver", "sgb": "SGB"}


def _instruments(positions: list[Position], today: date) -> list[dict]:
    out = []
    for p in positions:
        h = p.holding
        if p.closed or h is None:
            continue
        r = xirr(p.cfs + [(today, p.current)]) if (p.covered and p.cfs) else None
        young = p.covered and p.cfs and _avg_holding_days(p.cfs, today) < _MIN_ANNUALISE_DAYS
        if young:
            r = None
        first = min((d for d, _ in p.cfs), default=None)
        years = max(0.0, (today - first).days / 365.25) if first else 0.0
        h_cagr = None
        nav_cost, nav_cur = float(h.avg_cost_nav or 0), float(h.current_nav or 0)
        fb = getattr(h, "first_buy_date", None) or first
        if p.covered and not young and nav_cost > 0 and nav_cur > 0 and fb and fb < today:
            y = (today - fb).days / 365.25
            h_cagr = cagr(nav_cost, nav_cur, y) if y > 0.08 else None
        inv = p.invested_remaining
        out.append({
            "isin": p.isin, "name": p.name, "type": p.cls, "owner": p.owner, "units": p.units,
            "current": p.current, "invested": inv, "gain": (p.current - inv) if inv else None,
            "gain_pct": round((p.current / inv - 1) * 100, 2) if inv else None,
            "years": round(years, 2), "xirr_pct": r, "cagr_pct": h_cagr, "goal_tag": p.goal_tag,
            "cost_quality": getattr(h, "cost_basis_quality", None),
        })
    out.sort(key=lambda x: x["current"], reverse=True)
    return out


def _manual_rows(positions: list[Position], today: date) -> list[dict]:
    """PPF, FDs and other manual investments as rows for the holdings table (the market holdings are the other rows)."""
    out = []
    for p in positions:
        if p.holding is not None or p.closed or p.isin is not None or p.current <= 0:
            continue
        end, val = p.terminal if p.terminal else (today, p.current)
        r = None if (p.deposit or not p.covered or not p.cfs) else xirr(p.cfs + [(end, val)])
        if r is not None and _avg_holding_days(p.cfs, end) < _MIN_ANNUALISE_DAYS:
            r = None                                    # held under a year on average: plain gain, as everywhere
        invested = sum(-a for _, a in p.cfs if a < 0) or None
        out.append({"name": p.name, "owner": p.owner, "type": p.cls, "current": round(p.current, 2),
                    "measured_to": end.isoformat() if p.terminal else None,
                    "rate": p.rate, "maturity": p.maturity.isoformat() if p.maturity else None,
                    "invested": invested, "gain": round(val - invested + sum(a for _, a in p.cfs if a > 0), 2) if (invested and p.covered and not p.deposit) else None,
                    "xirr_pct": r, "goal_tag": p.goal_tag, "contract": p.contract, "why": p.why})
    out.sort(key=lambda x: x["current"], reverse=True)
    return out


def _goals(positions: list[Position], today: date) -> list[dict]:
    by_tag: dict[str, list[Position]] = defaultdict(list)
    for p in positions:
        if not p.closed and p.holding is not None:         # holdings only, as the goal tiles always did
            by_tag[p.goal_tag or "untagged"].append(p)
    out = []
    for tag, ps in by_tag.items():
        row = summarise(ps, today)
        out.append({
            "goal_tag": tag, "count": len(ps), **row,
            "current": round(sum(p.current for p in ps), 2),       # the goal's value, covered or not
            "returns_withheld": row["note"] if row["xirr_pct"] is None and row["invested"] <= 0 else None,
        })
    out.sort(key=lambda x: x["current"], reverse=True)
    return out


# goal_returns.py blends per-instrument returns into a goal's expected return
def _per_instrument_returns(db: Session, owner_ids: list[str], today: date) -> list[dict]:
    positions, _ = build_positions(db, owner_ids, today)
    return _instruments(positions, today)
