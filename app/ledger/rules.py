"""
Pure ledger logic — no DB, no I/O. See docs/adr/0023-instrument-ledger.md.

Three facts are kept apart on purpose:

  trades     what we paid / received, exact date + price   (contract notes, Coin, tradebook)
  movements  what the depository says moved, + running bal  (monthly demat statement)
  anchor     the statement's closing quantity               (monthly "Holdings as on")

Quantity truth comes from movements/anchor; date, price and cost come from trades.
A trade is *matched* to the movement that settled it, so the same event is never
counted twice. Movements with no trade stay in the position with unknown cost.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Iterable, Optional

ZERO = Decimal(0)
QTY_TOL = Decimal("0.0005")          # statements print units to 3 dp
MF_TOL = Decimal("0.05")             # fund units accumulate 3-dp rounding between e-mails and statements


def tol_for(isin: str) -> Decimal:
    """Equity quantities are whole shares; fund/ETF units are fractional."""
    return MF_TOL if isin.startswith("INF") else QTY_TOL
MATCH_DAYS_BEFORE = 10               # a movement settles up to 10 days after the trade
MATCH_DAYS_AFTER = 3                 # ...and rarely a day or two before the trade date we parsed

# Lower rank wins when two sources describe the same (isin, date, side).
SOURCE_RANK = {"contract_note": 1, "coin": 1, "tradebook": 2, "derived": 3}


# ── value objects ────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Trade:
    id: str
    isin: str
    trade_date: date
    side: str                         # "B" | "S"
    qty: Decimal
    price: Optional[Decimal] = None
    amount: Optional[Decimal] = None
    source: str = "contract_note"
    symbol: Optional[str] = None
    settle: Optional[date] = None     # exact settlement date when the source prints it

    @property
    def signed(self) -> Decimal:
        return self.qty if self.side == "B" else -self.qty


@dataclass(frozen=True)
class Movement:
    id: str
    isin: str
    move_date: date
    qty_in: Decimal
    qty_out: Decimal
    running_qty: Optional[Decimal] = None
    description: str = ""
    seq: Optional[int] = None
    stmt: str = ""                    # statement key (as-on date); movements chain within one statement
    alert: bool = False               # CDSL transaction alert (daily, quantity only), not a statement line

    @property
    def side(self) -> str:
        return "B" if self.qty_in > 0 else "S"

    @property
    def qty(self) -> Decimal:
        return self.qty_in if self.qty_in > 0 else self.qty_out

    @property
    def signed(self) -> Decimal:
        return self.qty_in - self.qty_out


@dataclass
class Conflict:
    isin: str
    trade_date: date
    side: str
    kept_source: str
    kept_qty: Decimal
    other_source: str
    other_qty: Decimal


def settle_date(t: Trade) -> date:
    """
    When the units reach (or leave) the demat. Exchange trades settle T+1 business
    day (weekends only; exchange holidays are not modelled — reconciliation shows
    any boundary effect). Coin allotments/redemptions are credited on the allotment
    date the e-mail reports.
    """
    if t.settle:
        return t.settle
    if t.source == "coin":
        return t.trade_date
    d = t.trade_date + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


# ── 1. cross-source de-duplication ───────────────────────────────────────────
XSOURCE_DAYS = 4        # an order (tradebook) and its allotment (Coin) can be a few days apart


def effective_trades(trades: Iterable[Trade]) -> tuple[list[Trade], list[Trade], list[Conflict]]:
    """
    Collapse trades that more than one source reports for the same event down to the
    best-ranked source.

    Same event means either
      * same (isin, trade_date, side) — order-level tradebook rows vs one consolidated
        contract-note row; compared by *total* quantity, or
      * same isin and side, the same *fractional* unit quantity (to 3 dp) and dates within
        XSOURCE_DAYS — a fund order (tradebook, dated the order day) vs its Coin allotment
        mail (dated the allotment day). Whole-share quantities are never matched this way.

    Returns (effective, superseded, conflicts):
      effective   trades that count
      superseded  lower-ranked duplicates (same totals) — safe to ignore / delete
      conflicts   same-day groups where the sources disagree on total quantity; the
                  best-ranked source is kept but the disagreement is reported
    """
    groups: dict[tuple, list[Trade]] = defaultdict(list)
    for t in trades:
        groups[(t.isin, t.trade_date, t.side)].append(t)

    effective: list[Trade] = []
    superseded: list[Trade] = []
    conflicts: list[Conflict] = []
    for (isin, d, side), rows in groups.items():
        by_source: dict[str, list[Trade]] = defaultdict(list)
        for r in rows:
            by_source[r.source].append(r)
        best = min(by_source, key=lambda s: (SOURCE_RANK.get(s, 9), s))
        effective.extend(by_source[best])
        best_qty = sum((r.qty for r in by_source[best]), ZERO)
        for src, rs in by_source.items():
            if src == best:
                continue
            superseded.extend(rs)
            other_qty = sum((r.qty for r in rs), ZERO)
            if abs(other_qty - best_qty) > QTY_TOL:
                conflicts.append(Conflict(isin, d, side, best, best_qty, src, other_qty))

    # Cross-day pass for fund units: a lower-ranked trade that equals a better-ranked one.
    keepers = list(effective)
    used: set[str] = set()
    survivors: list[Trade] = []
    for t in sorted(keepers, key=lambda t: (SOURCE_RANK.get(t.source, 9), t.trade_date)):
        if t.qty == t.qty.to_integral_value():
            survivors.append(t)
            continue
        dup = next((h for h in survivors
                    if h.isin == t.isin and h.side == t.side and h.id not in used
                    and SOURCE_RANK.get(h.source, 9) < SOURCE_RANK.get(t.source, 9)
                    and abs(h.qty - t.qty) <= MF_TOL
                    and abs((h.trade_date - t.trade_date).days) <= XSOURCE_DAYS), None)
        if dup is not None:
            used.add(dup.id)
            superseded.append(t)
        else:
            survivors.append(t)
    effective = survivors
    effective.sort(key=lambda t: (t.trade_date, t.side, t.id))
    return effective, superseded, conflicts


# ── 2. trade ↔ movement matching ─────────────────────────────────────────────
@dataclass
class MatchResult:
    matches: dict[str, str] = field(default_factory=dict)     # trade id -> movement id
    unmatched_trades: list[Trade] = field(default_factory=list)
    unmatched_movements: list[Movement] = field(default_factory=list)


def match_movements(trades: Iterable[Trade], movements: Iterable[Movement]) -> MatchResult:
    """
    Pair every demat movement with the trade(s) that settled it.

    A movement matches one trade, or the trades of one-to-three consecutive trade
    days, whose quantities add up to the movement quantity (order-level tradebook
    rows and netted settlements both occur). Matching is per (isin, side) and
    greedy in date order; each trade is used at most once.
    """
    res = MatchResult()
    by_key_t: dict[tuple, list[Trade]] = defaultdict(list)
    by_key_m: dict[tuple, list[Movement]] = defaultdict(list)
    for t in trades:
        by_key_t[(t.isin, t.side)].append(t)
    for m in movements:
        if m.qty > 0:
            by_key_m[(m.isin, m.side)].append(m)

    for key in set(by_key_t) | set(by_key_m):
        ts = sorted(by_key_t.get(key, []), key=lambda t: (t.trade_date, t.id))
        ms = sorted(by_key_m.get(key, []), key=lambda m: (m.move_date, m.seq if m.seq is not None else 0, m.id))
        day_groups: list[list[Trade]] = []
        for t in ts:
            if day_groups and day_groups[-1][0].trade_date == t.trade_date:
                day_groups[-1].append(t)
            else:
                day_groups.append([t])
        used = [False] * len(day_groups)

        for m in ms:
            lo = m.move_date - timedelta(days=MATCH_DAYS_BEFORE)
            hi = m.move_date + timedelta(days=MATCH_DAYS_AFTER)
            cand = [i for i, g in enumerate(day_groups)
                    if not used[i] and lo <= g[0].trade_date <= hi]
            best: Optional[tuple] = None
            for start in range(len(cand)):
                total = ZERO
                for end in range(start, min(start + 3, len(cand))):
                    total += sum((t.qty for t in day_groups[cand[end]]), ZERO)
                    if abs(total - m.qty) <= QTY_TOL:
                        idxs = cand[start:end + 1]
                        spread = max(abs((day_groups[i][0].trade_date - m.move_date).days) for i in idxs)
                        score = (len(idxs), spread, idxs[0])
                        if best is None or score < best[0]:
                            best = (score, idxs)
                        break
                    if total > m.qty + QTY_TOL:
                        break
            if best is None:
                res.unmatched_movements.append(m)
                continue
            for i in best[1]:
                used[i] = True
                for t in day_groups[i]:
                    res.matches[t.id] = m.id

        for i, g in enumerate(day_groups):
            if not used[i]:
                res.unmatched_trades.extend(g)

    res.unmatched_trades.sort(key=lambda t: (t.trade_date, t.id))
    res.unmatched_movements.sort(key=lambda m: (m.move_date, m.id))
    return res


# ── 3. balance intervals: the heart of reconciliation ────────────────────────
Balance = tuple  # (as_on: date, qty: Decimal) — a month-end statement balance


@dataclass
class Interval:
    """Change between two consecutive statement balances and the trades that should explain it."""
    isin: str
    start: date
    end: date
    actual: Decimal                       # balance(end) - balance(start)
    traded: Decimal                       # signed qty of effective trades settling in (start, end]
    trades: list[Trade]
    lines: list[Movement]                 # demat movement lines dated in (start, end]
    unmatched_lines: list[Movement]       # ...that no trade accounts for
    tol: Decimal = QTY_TOL

    @property
    def residual(self) -> Decimal:
        return self.actual - self.traded

    @property
    def reconciled(self) -> bool:
        return abs(self.residual) <= self.tol


def boundary_indices(ivs: list["Interval"]) -> set[int]:
    """
    Adjacent intervals whose residuals cancel (+q then -q, or -q then +q): one trade settled
    across the month-end statement date. Nothing is missing, so neither side is an error and
    neither creates an untraced lot.
    """
    out: set[int] = set()
    for i in range(len(ivs) - 1):
        a, b = ivs[i], ivs[i + 1]
        if (not a.reconciled and not b.reconciled and abs(a.residual + b.residual) <= a.tol
                and abs(a.residual) > a.tol):
            out.update((i, i + 1))
    return out


@dataclass
class Position:
    isin: str
    qty: Decimal = ZERO                   # truth: last statement balance + trades settling after it
    known_qty: Decimal = ZERO             # units whose purchase price we know
    unknown_qty: Decimal = ZERO           # units with no traced price (pre-history, transfers, SGB …)
    known_cost: Decimal = ZERO            # gross cost of the known units
    first_buy_date: Optional[date] = None
    last_price: Optional[Decimal] = None
    last_price_date: Optional[date] = None
    flags: list[str] = field(default_factory=list)

    @property
    def quality(self) -> str:
        if self.qty <= QTY_TOL:
            return "exact"
        clean = "ledger_exceeds_statement" not in self.flags and "pre_history_exceeds" not in self.flags
        return "exact" if self.unknown_qty <= QTY_TOL and clean else "partial"

    @property
    def avg_cost(self) -> Optional[Decimal]:
        return (self.known_cost / self.known_qty) if self.known_qty > QTY_TOL else None

    @property
    def invested(self) -> Optional[Decimal]:
        """Cost of the whole position; only meaningful when quality == 'exact'."""
        return self.known_cost if self.quality == "exact" else None


@dataclass
class IsinState:
    isin: str
    anchor_date: Optional[date]
    anchor_qty: Decimal
    matched: dict[str, str]                # trade id -> movement line that settled it (audit trail)
    intervals: list[Interval]
    pending: list[Trade]                   # settle after the anchor → added to the position
    unmatched_movements: list[Movement]
    alert_only: list[Movement]             # CDSL alerts after the anchor that no trade explains
    position: Position


def compute_isin_state(
    isin: str,
    trades: list[Trade],
    movements: list[Movement],
    balances: list[tuple[date, Decimal]],
    alerts: Optional[list[Movement]] = None,
) -> IsinState:
    """
    trades     effective (de-duplicated) trades for this ISIN
    movements  demat movement lines for this ISIN (evidence only)
    alerts     CDSL "transactions in your demat account" alerts: daily, quantity only.
               Those after the last statement that no pending trade accounts for are
               added to the position (cost unknown) — the statement is the only other
               source of units that arrive without a contract note/Coin mail.
    balances   month-end statement balances (as-on date, qty), any order. The
               caller supplies a balance for *every* statement date, 0 where the
               ISIN was absent, so a fully-sold position closes properly.

    Quantity truth is the newest balance. Between two balances the trades that
    settle in the interval must add up to the change; a non-zero residual is
    reported (missing trade, duplicate, transfer …) — never silently absorbed.
    """
    balances = sorted(balances, key=lambda b: b[0])
    anchor_date, anchor_qty = (balances[-1] if balances else (None, ZERO))
    tol = tol_for(isin)
    mr = match_movements(trades, movements)

    # A trade matched to a movement line settled on that line's date — observed, not assumed
    # (Coin units credit T+1 on BSE StAR in 2021 but same-day on NSE in 2026).
    line_date = {m.id: m.move_date for m in movements}
    seen_settle = {tid: line_date[mid] for tid, mid in mr.matches.items()}

    def when(t: Trade) -> date:
        return seen_settle.get(t.id) or settle_date(t)

    # Intervals between consecutive statements
    intervals: list[Interval] = []
    unmatched_set = {m.id for m in mr.unmatched_movements}
    for (d0, q0), (d1, q1) in zip(balances, balances[1:]):
        in_iv = [t for t in trades if d0 < when(t) <= d1]
        lines = [m for m in movements if d0 < m.move_date <= d1]
        intervals.append(Interval(
            isin, d0, d1, q1 - q0, sum((t.signed for t in in_iv), ZERO), in_iv, lines,
            [m for m in lines if m.id in unmatched_set], tol,
        ))

    pending = [t for t in trades if anchor_date is None or when(t) > anchor_date]
    alert_only: list[Movement] = []
    if alerts and anchor_date is not None:
        late = [a for a in alerts if a.move_date > anchor_date]
        # a pending trade and its alert are the same units: match them, count the rest
        alert_only = match_movements(pending, late).unmatched_movements
    truth = anchor_qty + sum((t.signed for t in pending), ZERO) + sum((a.signed for a in alert_only), ZERO)

    # ── cost replay (FIFO — what Zerodha Console and tax law use) ────────────
    # events: every priced trade (settled or pending) + untraced inflows the
    # intervals could not attribute to a trade. Lots are consumed oldest-first.
    events: list[tuple] = []
    for t in trades:
        events.append((t.trade_date, 0 if t.side == "B" else 1, t.side, t.qty, t.price))
    boundary = boundary_indices(intervals)
    for i, iv in enumerate(intervals):
        if iv.residual > iv.tol and i not in boundary:
            events.append((iv.start, 0, "B", iv.residual, None))        # untraced inflow, unknown cost
    for a in alert_only:
        events.append((a.move_date, 0 if a.side == "B" else 1, a.side, a.qty, None))
    events.sort(key=lambda e: (e[0], e[1]))

    pos = Position(isin=isin)
    lots: list[list] = []                    # [qty, price | None, date | None], oldest first
    if balances:
        first_date, first_qty = balances[0]
        net_before = sum((t.signed for t in trades if when(t) <= first_date), ZERO)
        opening = first_qty - net_before
        if opening > tol:
            lots.append([opening, None, None])          # held before our history: oldest, cost unknown
        elif opening < -tol:
            # trades before the first statement exceed what it shows: units were sold in
            # that pre-history without a trade on file
            pos.flags.append("pre_history_exceeds")

    def consume(qty: Decimal) -> None:
        left = qty
        while left > QTY_TOL and lots:
            take = min(lots[0][0], left)
            lots[0][0] -= take
            left -= take
            if lots[0][0] <= QTY_TOL:
                lots.pop(0)
        if left > tol and "negative_position" not in pos.flags:
            pos.flags.append("negative_position")

    for d, _o, side, qty, price in events:
        if side == "B":
            has_price = price is not None and price > 0
            lots.append([qty, price if has_price else None, d])
            if has_price:
                pos.last_price, pos.last_price_date = price, d
        else:
            consume(qty)
            if price is not None and price > 0:
                pos.last_price, pos.last_price_date = price, d

    # Whatever the replay and the statement still disagree on: statement wins.
    held = sum((l[0] for l in lots), ZERO)
    residual = truth - held
    if residual > tol:
        lots.insert(0, [residual, None, None])
    elif residual < -tol:
        if "pre_history_exceeds" not in pos.flags and "ledger_exceeds_statement" not in pos.flags:
            pos.flags.append("ledger_exceeds_statement")
        consume(-residual)          # drop the oldest excess units; statement quantity wins

    # Units with no trade whose tranche terms are published (SGBs) cost the issue price from the issue date.
    from app.ledger.sgb import issue_terms
    terms = issue_terms(isin)
    if terms:
        for l in lots:
            if l[1] is None:
                l[1], l[2] = terms[1], terms[0]
                if "issue_price_cost" not in pos.flags:
                    pos.flags.append("issue_price_cost")

    pos.known_qty = sum((l[0] for l in lots if l[1] is not None), ZERO)
    pos.unknown_qty = sum((l[0] for l in lots if l[1] is None), ZERO)
    pos.known_cost = sum((l[0] * l[1] for l in lots if l[1] is not None), ZERO)
    dated = [l[2] for l in lots if l[1] is not None and l[2] is not None]
    pos.first_buy_date = min(dated) if dated else None
    pos.qty = truth if truth > QTY_TOL else ZERO
    return IsinState(isin, anchor_date, anchor_qty, mr.matches, intervals, pending,
                     mr.unmatched_movements, alert_only, pos)
