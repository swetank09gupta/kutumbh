"""
Ledger engine — loads trades, demat movements and statement anchors from the DB,
runs the pure rules in app.ledger.rules, and records what it could not prove.

Entry points:
  compute_owner(db, owner_id)  -> OwnerLedger   (read-only)
  reconcile(db)                -> summary dict  (links matches, upserts recon_issues)
  dedupe_cashflows(db, apply)  -> dry-run/apply removal of cross-source duplicates
"""
from __future__ import annotations

import logging
import re
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import func, text
from sqlalchemy.orm import Session

from app.ledger import rules
from app.ledger.rules import ZERO, Movement, Trade
from app.models.schemas import (
    Account, DematMovement, Holding, InstrumentTrade, IsinAlias, ReconIssue, StatementBalance, Transaction,
)

logger = logging.getLogger(__name__)

LEDGER_SOURCE_PREFIX = "ledger:"
_TRADE_SETTLEMENT = re.compile(r"payout|pay-?in|delivery|obligation|settlement", re.I)
_TRANSFER = re.compile(r"inter\s*depository|off.?market|transfer|\bidt\b", re.I)
_CORP_ACTION = re.compile(r"corporate|\bCA\b|bonus|split|rights|demerger|merger|dividend|ipo|allot|sgb", re.I)


# ── data classes ─────────────────────────────────────────────────────────────
@dataclass
class OwnerLedger:
    owner_id: str
    anchor_date: Optional[date]
    states: dict[str, rules.IsinState]
    meta: dict[str, dict]                         # isin -> name / symbol / holding_type
    trades: list[Trade]                           # effective trades
    superseded: list[Trade]
    conflicts: list[rules.Conflict]
    unresolved: list[InstrumentTrade]             # trades with no ISIN
    movements: list[Movement]
    txn_links: dict[str, str] = field(default_factory=dict)   # trade id -> txn fingerprint
    statement_dates: list[date] = field(default_factory=list)


# ── loading ──────────────────────────────────────────────────────────────────
def statement_balances(db: Session, owner_id: str):
    """
    Month-end demat balances for the owner: (dates, {date: {isin: qty}}, {isin: meta}).

    The timeline is Zerodha's own monthly statements (one family, one scope, absent = 0).
    CDSL's CAS arrives sooner, so its last date extends the timeline — but only for ISINs
    that belong to the Zerodha ledger (in the last Zerodha statement, or traded, or seen
    in its movement lines); the CAS also lists holdings outside this demat. With no
    Zerodha statements at all, CDSL is the timeline. Both families are cross-checked on
    shared dates by _depository_findings.
    """
    rows = db.query(StatementBalance).filter(StatementBalance.owner_id == owner_id).all()
    fam: dict[str, dict[date, dict[str, Decimal]]] = {"zerodha": defaultdict(dict), "cdsl": defaultdict(dict)}
    meta: dict[str, dict] = {}
    for r in sorted(rows, key=lambda r: r.as_on):
        fam[r.family][r.as_on]                     # register the date even if it held nothing
        if r.isin:
            fam[r.family][r.as_on][r.isin] = r.qty or ZERO
            meta[r.isin] = {"name": r.name, "symbol": r.name, "holding_type": None,
                            "goal_tag": None, "stmt_rate": r.rate}
    z, c = fam["zerodha"], fam["cdsl"]
    if z:
        qty_by_date = {d: dict(m) for d, m in z.items()}
        last_z = max(z)
        later = [d for d in c if d > last_z]
        if later:
            d_c = max(later)
            scope = set(z[last_z]) | {r[0] for r in db.query(InstrumentTrade.isin).filter(
                InstrumentTrade.owner_id == owner_id, InstrumentTrade.isin.isnot(None)).distinct().all()} \
                | {r[0] for r in db.query(DematMovement.isin).filter(DematMovement.owner_id == owner_id).distinct().all()}
            qty_by_date[d_c] = {i: q for i, q in c[d_c].items() if i in scope}
    else:
        qty_by_date = {d: dict(m) for d, m in c.items()}
    for isin, htype, tag in (
        db.query(Holding.isin, Holding.holding_type, Holding.goal_tag)
        .join(Account, Holding.account_id == Account.id)
        .filter(Account.owner_id == owner_id, Holding.holding_type.isnot(None),
                ~Holding.source_file.like(f"{LEDGER_SOURCE_PREFIX}%")).distinct().all()
    ):
        if isin in meta:
            meta[isin]["holding_type"] = meta[isin].get("holding_type") or htype
            meta[isin]["goal_tag"] = meta[isin].get("goal_tag") or tag
    return sorted(qty_by_date), qty_by_date, meta


def _to_trade(r: InstrumentTrade) -> Trade:
    return Trade(str(r.id), r.isin, r.trade_date, r.side, r.quantity, r.price, r.amount,
                 r.source, r.symbol, r.settle_date)


def _to_move(r: DematMovement) -> Movement:
    return Movement(str(r.id), r.isin, r.move_date, r.qty_in or ZERO, r.qty_out or ZERO,
                    r.running_qty, r.description or "", r.seq,
                    str(r.statement_date) if r.statement_date else r.source_file or "",
                    alert=(r.description or "").startswith("CDSL alert"))


def compute_owner(db: Session, owner_id: str) -> OwnerLedger:
    dates, qty_by_date, meta = statement_balances(db, owner_id)
    anchor_date = dates[-1] if dates else None

    trade_rows = db.query(InstrumentTrade).filter(InstrumentTrade.owner_id == owner_id).all()
    resolved = [r for r in trade_rows if r.isin]
    unresolved = [r for r in trade_rows if not r.isin]
    for r in resolved:
        meta.setdefault(r.isin, {"name": r.instrument_name, "symbol": r.symbol,
                                 "holding_type": r.holding_type, "goal_tag": None, "stmt_rate": None})
    links = {str(r.id): r.txn_fingerprint for r in trade_rows}

    effective, superseded, conflicts = rules.effective_trades([_to_trade(r) for r in resolved])

    move_rows = db.query(DematMovement).filter(DematMovement.owner_id == owner_id).all()
    all_moves = [_to_move(r) for r in move_rows]
    movements = [m for m in all_moves if not m.alert]
    # Alerts name the demat they belong to; keep only this owner's Zerodha demat
    # (statement lines carry its 16-digit client id).
    suffixes = {mm.group(1)[-8:] for m in movements for mm in [re.search(r"\b(1208\d{12})\b", m.description)] if mm}
    alerts = [m for m in all_moves if m.alert
              and (not suffixes or re.search(r"\*(\d{8})", m.description) is None
                   or re.search(r"\*(\d{8})", m.description).group(1) in suffixes)]
    for r in move_rows:
        meta.setdefault(r.isin, {"name": r.symbol, "symbol": r.symbol,
                                 "holding_type": None, "goal_tag": None, "stmt_rate": None})

    by_isin_t: dict[str, list[Trade]] = defaultdict(list)
    by_isin_m: dict[str, list[Movement]] = defaultdict(list)
    by_isin_a: dict[str, list[Movement]] = defaultdict(list)
    for t in effective:
        by_isin_t[t.isin].append(t)
    for m in movements:
        by_isin_m[m.isin].append(m)
    for a in alerts:
        by_isin_a[a.isin].append(a)

    held_ever = {i for m in qty_by_date.values() for i in m}
    states: dict[str, rules.IsinState] = {}
    for isin in set(by_isin_t) | set(by_isin_m) | set(by_isin_a) | held_ever:
        balances = [(d, qty_by_date[d].get(isin, ZERO)) for d in dates]
        states[isin] = rules.compute_isin_state(
            isin, by_isin_t.get(isin, []), by_isin_m.get(isin, []), balances, by_isin_a.get(isin, []))
    return OwnerLedger(owner_id, anchor_date, states, meta, effective, superseded,
                       conflicts, unresolved, movements, links, dates)


# ── ISIN aliases ─────────────────────────────────────────────────────────────
def depository_isins(db: Session) -> dict[str, set[str]]:
    """{owner: ISINs that owner's depository has shown}: real for that owner, never aliases of theirs."""
    out: dict[str, set[str]] = defaultdict(set)
    for o, i in db.query(StatementBalance.owner_id, StatementBalance.isin).filter(StatementBalance.isin != "").distinct():
        out[o].add(i)
    for o, i in db.query(DematMovement.owner_id, DematMovement.isin).distinct():
        out[o].add(i)
    return out


def canonicalize_isins(db: Session) -> dict:
    """
    Learn ISIN aliases from demat evidence and re-point trades (and their cashflow
    rows) to the ISIN the depository uses.

    A trade votes X→Y when a demat movement of the same owner under another ISIN Y has the
    same direction and the same *fractional* unit quantity inside the usual settlement
    window, while no movement under the trade's own ISIN X matches it. Exact 3-decimal unit
    equality makes a coincidence between two funds negligible; whole-share quantities are
    never evidence (a 5-share ONGC trade "matched" a 5-share JSW movement), so aliases are
    learned for fund ISINs (INF…) only, and only when exactly one Y collects the votes.
    """
    from datetime import timedelta

    moves = defaultdict(list)       # owner -> [(isin, date, in, out)]
    for m in db.query(DematMovement).all():
        moves[m.owner_id].append((m.isin, m.move_date, m.qty_in or ZERO, m.qty_out or ZERO))
    votes: dict[tuple[str, str], dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for t in db.query(InstrumentTrade).filter(InstrumentTrade.isin.like("INF%")).all():
        if t.quantity == t.quantity.to_integral_value():
            continue                                # whole units carry no evidence
        lo = t.trade_date - timedelta(days=rules.MATCH_DAYS_AFTER)
        hi = t.trade_date + timedelta(days=rules.MATCH_DAYS_BEFORE)
        own, other = False, set()
        for isin, d, q_in, q_out in moves.get(t.owner_id, []):
            q = q_in if t.side == "B" else q_out
            if q <= 0 or not (lo <= d <= hi) or abs(q - t.quantity) > rules.MF_TOL:
                continue
            if isin == t.isin:
                own = True
            elif isin.startswith("INF"):
                other.add(isin)
        if not own and len(other) == 1:
            votes[(t.owner_id, t.isin)][next(iter(other))] += 1

    depo = depository_isins(db)
    learned = {}
    for (o, x), ys in votes.items():
        if len(ys) == 1 and x not in depo[o]:       # an ISIN the owner's depository shows is real, not an alias
            learned[(o, x)] = next(iter(ys))
    # Drop aliases that turned out to be this owner's depository ISINs (a scheme that changed
    # ISIN over time, or a mistaken vote) — including circular pairs.
    for a in db.query(IsinAlias).all():
        if a.alias_isin in depo[a.owner_id]:
            db.delete(a)
    db.flush()
    existing = {(a.owner_id, a.alias_isin): a for a in db.query(IsinAlias).all()}
    for (o, x), y in learned.items():
        if (o, x) not in existing:
            db.add(IsinAlias(owner_id=o, alias_isin=x, isin=y, votes=votes[(o, x)][y]))
        else:
            existing[(o, x)].isin, existing[(o, x)].votes = y, votes[(o, x)][y]
    db.flush()

    aliases = {(a.owner_id, a.alias_isin): a.isin for a in db.query(IsinAlias).all()}
    # Desired state: every trade carries canonical(original ISIN). The original is the one
    # in its natural key, so this also undoes any earlier wrong re-pointing.
    repointed = cashflows = 0
    for r in db.query(InstrumentTrade).filter(InstrumentTrade.isin.isnot(None)).all():
        parts = (r.natural_key or "").split("|")
        orig = parts[2] if len(parts) > 2 and len(parts[2]) == 12 and parts[2][:2] == "IN" else r.isin
        want = r.isin_override or aliases.get((r.owner_id, orig), orig)
        if r.isin != want:
            r.isin = want
            repointed += 1
            if r.txn_fingerprint:
                cashflows += db.query(Transaction).filter(Transaction.fingerprint == r.txn_fingerprint) \
                    .update({"isin": want}, synchronize_session=False)
    if repointed:
        db.flush()
    # Every trade's cashflow row carries the trade's ISIN (a Coin mail names the fund but not
    # the ISIN; the ledger resolved it, the cashflow row must say the same or per-fund returns
    # silently lose that money).
    synced = db.execute(text(
        "UPDATE transactions t SET isin = i.isin FROM instrument_trades i "
        "WHERE i.txn_fingerprint = t.fingerprint AND i.isin IS NOT NULL AND t.isin IS DISTINCT FROM i.isin")).rowcount
    return {"aliases": len(aliases), "trades_repointed": repointed, "cashflows_repointed": cashflows,
            "cashflow_isins_synced": synced}


def learn_twin_aliases(db: Session, led: OwnerLedger) -> int:
    """
    Two fund ISINs of one scheme show up as mirror-image residuals: the depository ISIN's
    balance moved by +q with no trade, while the e-mail's ISIN has trades of q and no balance
    move (-q), in the same interval. Exact fractional equality is the evidence.
    """
    pos_res: dict[tuple, str] = {}
    neg_res: dict[tuple, str] = {}
    for isin, st in led.states.items():
        if not isin.startswith("INF"):
            continue
        for iv in st.intervals:
            if iv.reconciled or iv.residual == iv.residual.to_integral_value():
                continue
            (pos_res if iv.residual > 0 else neg_res)[(iv.end, abs(iv.residual).quantize(Decimal("0.001")))] = isin
    learned = 0
    oid = led.owner_id
    existing = {a.alias_isin for a in db.query(IsinAlias).filter(IsinAlias.owner_id == oid).all()}
    depo = depository_isins(db)[oid]
    candidates: dict[str, set[str]] = defaultdict(set)         # trade ISIN -> possible depository ISINs
    for key, trade_isin in neg_res.items():
        keeper = pos_res.get(key)
        if keeper and keeper != trade_isin:
            candidates[trade_isin].add(keeper)
    for trade_isin, keepers in candidates.items():
        if len(keepers) == 1 and trade_isin not in existing and trade_isin not in depo:
            db.add(IsinAlias(owner_id=oid, alias_isin=trade_isin, isin=next(iter(keepers)), votes=1))
            learned += 1

    # Mirror residuals are evidence for ONE trade, not for a whole ISIN: the same fund can sit
    # under two ISINs (Coin and the depository), and a fund renamed in a Coin mail can resolve to
    # the wrong sibling. Re-point exactly the trade that explains the mirrored residual.
    for mine, theirs in ((neg_res, pos_res), (pos_res, neg_res)):
        for key, trade_isin in mine.items():
            keeper = theirs.get(key)
            if not keeper or keeper == trade_isin or keeper not in depo:
                continue
            st = led.states.get(trade_isin)
            if st is None:
                continue
            end, q = key
            for iv in st.intervals:
                if iv.end != end or iv.reconciled or abs(abs(iv.residual) - q) > Decimal("0.001"):
                    continue
                hit = [t for t in iv.trades if abs(t.signed + iv.residual) <= Decimal("0.001")]
                if len(hit) != 1:
                    continue
                row = db.get(InstrumentTrade, uuid.UUID(hit[0].id))
                if row is not None and row.isin_override != keeper:
                    row.isin_override = keeper
                    learned += 1
    if learned:
        db.flush()
    return learned


# ── unresolved ISINs ─────────────────────────────────────────────────────────
def resolve_isins(db: Session) -> int:
    """
    Give ISIN-less trades (Coin mails whose fund name AMFI can't match) an ISIN when
    it can be proven, in order of strength:
      1. a demat movement of the same owner with the same unit quantity, same direction,
         settling within the usual window — and exactly one candidate ISIN
      2. the same owner already traded that folio under exactly one ISIN
      3. the AMFI scheme-name lookup
    Returns the number of trades resolved.
    """
    from datetime import timedelta
    from app.engine.amfi_isin import lookup_isin

    unresolved = db.query(InstrumentTrade).filter(InstrumentTrade.isin.is_(None)).all()
    if not unresolved:
        return 0
    folio_map: dict[tuple[str, str], set[str]] = defaultdict(set)
    for owner, folio, isin in (
        db.query(InstrumentTrade.owner_id, InstrumentTrade.source_ref, InstrumentTrade.isin)
        .filter(InstrumentTrade.isin.isnot(None), InstrumentTrade.source_ref.isnot(None)).all()
    ):
        folio_map[(owner, folio)].add(isin)

    fixed = 0
    for r in unresolved:
        isin = None
        want_in = r.side == "B"
        lo, hi = r.trade_date - timedelta(days=rules.MATCH_DAYS_AFTER), r.trade_date + timedelta(days=rules.MATCH_DAYS_BEFORE)
        cands = {
            m.isin for m in db.query(DematMovement).filter(
                DematMovement.owner_id == r.owner_id, DematMovement.move_date >= lo, DematMovement.move_date <= hi).all()
            if abs((m.qty_in if want_in else m.qty_out) - r.quantity) <= rules.MF_TOL and (m.qty_in if want_in else m.qty_out) > 0
        }
        if len(cands) == 1:
            isin = next(iter(cands))
        if not isin:
            fm = folio_map.get((r.owner_id, r.source_ref or ""), set())
            if len(fm) == 1:
                isin = next(iter(fm))
        if not isin and r.instrument_name and not r.instrument_name.startswith(("Folio ", "Unknown")):
            try:
                isin = lookup_isin(r.instrument_name)
            except Exception:                       # AMFI cache unavailable
                isin = None
        if isin:
            r.isin = isin
            r.holding_type = r.holding_type or ("mutual_fund" if isin.startswith("INF") else None)
            fixed += 1
    if fixed:
        db.flush()
    return fixed


# ── reconcile ────────────────────────────────────────────────────────────────
@dataclass
class Finding:
    kind: str
    severity: str
    owner_id: Optional[str]
    isin: Optional[str]
    name: Optional[str]
    as_of: Optional[date]
    detail: str
    ledger_qty: Optional[Decimal] = None
    statement_qty: Optional[Decimal] = None
    extra: str = ""

    @property
    def key(self) -> str:
        return "|".join([self.kind, self.owner_id or "", self.isin or "", str(self.as_of or ""), self.extra])[:300]


def _classify_movement(m: Movement) -> tuple[str, str, str]:
    d = m.description or ""
    if _TRANSFER.search(d):
        return "off_market_transfer", "warn", "Depository transfer with no trade — cost basis unknown"
    if _TRADE_SETTLEMENT.search(d):
        return "missing_trade", "error", "Settled in the demat but no contract note/Coin/tradebook trade matches it"
    if _CORP_ACTION.search(d):
        return "corporate_action", "info", "Corporate action / allotment — cost basis unknown"
    return "unclassified_movement", "warn", "Demat movement with no matching trade"


def _depository_findings(db: Session, owner_id: str) -> list[Finding]:
    """
    Zerodha's statement and CDSL's CAS must agree on every shared date for the ISINs the
    Zerodha demat holds (the CAS also lists other demats, so extra CDSL-only ISINs are expected).
    """
    rows = db.query(StatementBalance).filter(StatementBalance.owner_id == owner_id).all()
    by: dict[date, dict[str, dict[str, Decimal]]] = defaultdict(lambda: {"zerodha": {}, "cdsl": {}})
    names: dict[str, str] = {}
    for r in rows:
        by[r.as_on][r.family]
        if r.isin:
            by[r.as_on][r.family][r.isin] = r.qty or ZERO
            names[r.isin] = r.name
    out: list[Finding] = []
    for d, fams in by.items():
        z, c = fams["zerodha"], fams["cdsl"]
        if not z or not c:
            continue
        for isin in z:
            if abs(z[isin] - c.get(isin, ZERO)) > rules.tol_for(isin):
                out.append(Finding(
                    "depository_mismatch", "warn", owner_id, isin, names.get(isin), d,
                    f"Zerodha statement shows {z[isin]} but CDSL CAS shows {c.get(isin, ZERO)} on {d}",
                    z[isin], c.get(isin, ZERO)))
    return out


def _interval_findings(led: OwnerLedger) -> list[Finding]:
    """Every change between two month-end balances must be explained by trades."""
    out: list[Finding] = []
    oid = led.owner_id
    for isin, st in led.states.items():
        nm = (led.meta.get(isin) or {}).get("name")
        ivs = st.intervals
        # A trade straddling a month-end (settles on the 1st) shows as +q in one interval and
        # -q in the next. The pair cancels: report it as information, not as two errors.
        boundary = rules.boundary_indices(ivs)
        for i in range(len(ivs) - 1):
            a, b = ivs[i], ivs[i + 1]
            if i in boundary and i + 1 in boundary and abs(a.residual + b.residual) <= a.tol and not a.reconciled:
                out.append(Finding(
                    "settlement_boundary", "info", oid, isin, nm, b.start,
                    f"{a.residual:+} in {a.start}→{a.end} and {b.residual:+} in {b.start}→{b.end} cancel: "
                    f"a trade settled across the month-end", a.residual, b.residual, extra=f"{a.start}>{b.end}"))
        for i, iv in enumerate(ivs):
            if iv.reconciled or i in boundary:
                continue
            ext = f"{iv.start}>{iv.end}"
            if iv.residual > 0:
                kinds = [_classify_movement(m) for m in iv.unmatched_lines]
                if not kinds:
                    kind, sev, why = ("untraced_change", "warn",
                                      "Balance rose with no trade and no movement line")
                else:
                    kind, sev, why = sorted(kinds, key=lambda k: {"error": 0, "warn": 1, "info": 2}[k[1]])[0]
                lines = "; ".join(f"+{m.qty} {m.move_date} {m.description[:40]}" for m in iv.unmatched_lines[:3])
                out.append(Finding(
                    kind, sev, oid, isin, nm, iv.end,
                    f"{iv.start} → {iv.end}: balance changed by {iv.actual:+} but trades explain only "
                    f"{iv.traded:+}; {iv.residual} unexplained. {why}." + (f" Lines: {lines}" if lines else ""),
                    iv.traded, iv.actual, extra=ext))
            else:
                src = ", ".join(sorted({f'{t.source} {t.side} {t.qty} {t.trade_date}' for t in iv.trades}))[:200]
                out.append(Finding(
                    "trades_exceed_statement", "error", oid, isin, nm, iv.end,
                    f"{iv.start} → {iv.end}: balance changed by {iv.actual:+} but trades add up to "
                    f"{iv.traded:+} (excess {-iv.residual}) — duplicate, wrong date or quantity? {src}",
                    iv.traded, iv.actual, extra=ext))
    return out


def _missing_statement_findings(led: OwnerLedger) -> list[Finding]:
    out: list[Finding] = []
    for prev, cur in zip(led.statement_dates, led.statement_dates[1:]):
        if (cur - prev).days > 35:
            out.append(Finding("missing_statement", "warn", led.owner_id, None, None, cur,
                               f"No demat statement between {prev} and {cur} "
                               f"({(cur - prev).days} days) — changes in that window cannot be reconciled",
                               extra=f"{prev}>{cur}"))
    return out


def findings_for_owner(db: Session, led: OwnerLedger) -> list[Finding]:
    f: list[Finding] = []
    oid = led.owner_id
    name = lambda i: (led.meta.get(i) or {}).get("name")

    f.extend(_interval_findings(led))
    f.extend(_missing_statement_findings(led))
    f.extend(_depository_findings(db, oid))

    for c in led.conflicts:
        f.append(Finding("source_conflict", "warn", oid, c.isin, name(c.isin), c.trade_date,
                         f"{c.kept_source} says {c.kept_qty} {c.side} but {c.other_source} says {c.other_qty}; "
                         f"{c.kept_source} is used", c.kept_qty, c.other_qty, extra=f"{c.side}|{c.other_source}"))

    for t in led.unresolved:
        f.append(Finding("unresolved_instrument", "warn", oid, None, t.instrument_name, t.trade_date,
                         f"{t.side} {t.quantity} on {t.trade_date} has no ISIN, so it is not in any position",
                         extra=str(t.id)))

    for isin, st in led.states.items():
        pos = st.position
        for t in st.pending:
            f.append(Finding("pending_trade", "info", oid, isin, name(isin), t.trade_date,
                             f"{t.side} {t.qty} @ {t.price} settles after the last statement "
                             f"({led.anchor_date}); counted in the position until a statement confirms it",
                             t.qty, None, extra=t.id))
        for a in st.alert_only:
            f.append(Finding("alert_without_trade", "warn", oid, isin, name(isin), a.move_date,
                             f"CDSL alert: {'+' if a.side == 'B' else '-'}{a.qty} on {a.move_date} after the last statement "
                             f"({led.anchor_date}) with no trade on file; counted in the position, cost unknown",
                             a.signed, None, extra=a.id))
        if "pre_history_exceeds" in pos.flags:
            f.append(Finding("pre_history_mismatch", "warn", oid, isin, name(isin), led.statement_dates[0] if led.statement_dates else None,
                             "Trades before the first statement add up to more than it shows: units were sold "
                             "before our statement history and no sell is on file (needs a tradebook for that period)"))
        if "ledger_exceeds_statement" in pos.flags:
            f.append(Finding("ledger_exceeds_statement", "error", oid, isin, name(isin), led.anchor_date,
                             "Trades and untraced inflows add up to more than the latest statement shows — "
                             "statement used", None, st.anchor_qty))
        if "negative_position" in pos.flags:
            f.append(Finding("negative_position", "error", oid, isin, name(isin), led.anchor_date,
                             "A sell exceeds the units held at that point in the replay — earlier buy missing"))

    # cashflow link: every ledger trade needs exactly its cashflow row in transactions
    fps = {fp for fp in led.txn_links.values() if fp}
    existing = {r[0] for r in db.query(Transaction.fingerprint).filter(Transaction.fingerprint.in_(fps)).all()} if fps else set()
    for tid, fp in led.txn_links.items():
        if fp and fp not in existing:
            f.append(Finding("trade_without_cashflow", "warn", oid, None, None, None,
                             "Ledger trade has no matching row in transactions (XIRR would miss it)", extra=tid))
    return f


def _cashflow_findings(db: Session) -> list[Finding]:
    """Instrument cashflows in `transactions` that no ledger trade accounts for."""
    out: list[Finding] = []
    linked = {r[0] for r in db.query(InstrumentTrade.txn_fingerprint)
              .filter(InstrumentTrade.txn_fingerprint.isnot(None)).all()}
    q = (db.query(Transaction, Account)
         .join(Account, Transaction.account_id == Account.id)
         .filter(Account.institution.in_(["Zerodha", "Zerodha Coin"]),
                 Transaction.isin.isnot(None),
                 ~Transaction.description.like("DEMAT %")))
    for t, a in q:
        if t.fingerprint not in linked:
            out.append(Finding("cashflow_without_trade", "warn", a.owner_id, t.isin, t.counterparty, t.date,
                               f"₹{t.amount} {t.transaction_type} ({(t.description or '')[:50]}) has no ledger trade — "
                               f"imported before the ledger existed or from an unsupported source",
                               extra=t.fingerprint[:16]))
    return out


def reconcile(db: Session, owner_ids: Optional[list[str]] = None) -> dict:
    """Run every check, link matched trades to their movements, refresh recon_issues."""
    aliasing = canonicalize_isins(db)
    resolved = resolve_isins(db)
    deduped = dedupe_cashflows(db, apply=True)      # same-total duplicates only; never touches conflicts
    owners = owner_ids or [o for (o,) in db.query(InstrumentTrade.owner_id).distinct().all()] \
        + [o for (o,) in db.query(DematMovement.owner_id).distinct().all()]
    owners = sorted(set(owners))

    findings: list[Finding] = []
    linked = 0
    for oid in owners:
        led = compute_owner(db, oid)
        if learn_twin_aliases(db, led):             # re-point trades to the depository ISIN, recompute
            canonicalize_isins(db)
            led = compute_owner(db, oid)
        findings.extend(findings_for_owner(db, led))
        # persist trade -> movement links (audit trail of what matched what)
        match = {}
        for st in led.states.values():
            match.update(st.matched)
        for r in db.query(InstrumentTrade).filter(InstrumentTrade.owner_id == oid).all():
            mid = match.get(str(r.id))
            new = None if mid is None else uuid.UUID(mid)
            if r.settled_movement_id != new:
                r.settled_movement_id = new
                linked += 1
    findings.extend(_cashflow_findings(db))

    # upsert: seen -> open; not seen any more -> resolved
    now = datetime.now(timezone.utc)
    existing = {r.issue_key: r for r in db.query(ReconIssue).all()}
    seen: set[str] = set()
    for fd in findings:
        seen.add(fd.key)
        row = existing.get(fd.key)
        if row is None:
            row = ReconIssue(issue_key=fd.key)
            db.add(row)
        row.owner_id, row.isin, row.instrument_name = fd.owner_id, fd.isin, (fd.name or "")[:200] or None
        row.kind, row.severity, row.as_of_date, row.detail = fd.kind, fd.severity, fd.as_of, fd.detail
        row.ledger_qty, row.statement_qty = fd.ledger_qty, fd.statement_qty
        row.status, row.last_seen, row.resolved_at = "open", now, None
    resolved_n = 0
    for key, row in existing.items():
        if key not in seen and row.status == "open":
            row.status, row.resolved_at = "resolved", now
            resolved_n += 1
    db.commit()

    by_kind: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for fd in findings:
        by_kind[fd.kind][fd.severity] += 1
    return {
        "owners": owners,
        "isin_aliases": aliasing,
        "isins_resolved": resolved,
        "duplicates_removed": {k: deduped[k] for k in ("duplicates", "trades_removed", "cashflows_removed")},
        "trade_links_updated": linked,
        "open_issues": len(findings),
        "newly_resolved": resolved_n,
        "errors": sum(1 for x in findings if x.severity == "error"),
        "warnings": sum(1 for x in findings if x.severity == "warn"),
        "by_kind": {k: dict(v) for k, v in sorted(by_kind.items())},
    }


# ── cross-source duplicate removal ───────────────────────────────────────────
def dedupe_cashflows(db: Session, apply: bool = False) -> dict:
    """
    Where two sources report the same (owner, isin, date, side) with the same
    total quantity, only the best-ranked source may feed XIRR. Lower-ranked
    trades are listed (dry run) or removed together with their cashflow rows.
    Disagreeing totals are never touched — they are reported as source_conflict.
    """
    removed_trades = removed_txns = 0
    candidates: list[dict] = []
    owners = [o for (o,) in db.query(InstrumentTrade.owner_id).distinct().all()]
    for oid in owners:
        rows = db.query(InstrumentTrade).filter(InstrumentTrade.owner_id == oid,
                                                InstrumentTrade.isin.isnot(None)).all()
        by_id = {str(r.id): r for r in rows}
        _, superseded, _conflicts = rules.effective_trades([_to_trade(r) for r in rows])
        conflict_groups = {(c.isin, c.trade_date, c.side) for c in _conflicts}
        for t in superseded:
            if (t.isin, t.trade_date, t.side) in conflict_groups:
                continue
            r = by_id[t.id]
            candidates.append({"owner": oid, "isin": t.isin, "date": str(t.trade_date), "side": t.side,
                               "qty": str(t.qty), "source": t.source, "source_file": r.source_file})
            if apply:
                if r.txn_fingerprint:
                    removed_txns += db.query(Transaction).filter(
                        Transaction.fingerprint == r.txn_fingerprint).delete(synchronize_session=False)
                db.delete(r)
                removed_trades += 1
    if apply:
        db.commit()
    return {"applied": apply, "duplicates": len(candidates), "trades_removed": removed_trades,
            "cashflows_removed": removed_txns, "sample": candidates[:20]}
