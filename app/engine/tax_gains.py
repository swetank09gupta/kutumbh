"""
Capital gains from the ledger's own trades: FIFO lots, term by holding period, rates by sale date from the rules registry.
Pure functions (no database): app.engine.tax_view feeds them and adds income, TDS and unrealised tax.

What is computed, and what is reported instead of guessed:
  * equity shares and equity-oriented funds: STCG / LTCG (12 months), rates and the LTCG exemption by sale date;
  * listed gold/silver ETFs: LTCG 12.5% without the exemption, for sales from 23 Jul 2024 (earlier rules are not modelled);
  * debt funds bought from 1 Apr 2023: slab rate (amount only); earlier purchases are reported, not computed;
  * anything else, and sales beyond the units we have a cost for, are listed with what is missing.
Prices are the trade prices (gross): brokerage and stamp duty, which the ITR allows, are not netted; STT, which it does
not, is not included. Corporate actions are not applied.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Iterable, Optional

from app.engine.loan_schedule import add_months
from app.knowledge import rules

D = Decimal
ZERO = D(0)
BUDGET_2024 = date(2024, 7, 23)
DEBT_NEW_RULE = date(2023, 4, 1)


def fy_of(d: date) -> str:
    y = d.year if d.month >= 4 else d.year - 1
    return f"{y}-{str(y + 1)[2:]}"


def fy_end(fy: str) -> date:
    return date(int(fy[:4]) + 1, 3, 31)


def tax_class(isin: str, name: str = "", category: str | None = None) -> str:
    """equity_share | equity_fund | gold_silver_etf | debt_fund | other | unclassified, from AMFI's own category."""
    n = (name or "").upper()
    if isin.startswith("INE"):
        return "equity_share"
    if not isin.startswith("INF"):
        return "other"
    cat = (category or "").lower()
    if re.search(r"GOLD|SILVER", n) and (cat.startswith("exchange traded") or cat.startswith("other scheme") or not cat):
        return "gold_silver_etf"            # the name decides: AMFI files a silver ETF under "Equity ETF"
    if cat.startswith("exchange traded"):
        return "equity_fund" if "equity" in cat else "debt_fund" if "debt" in cat else "unclassified"
    if cat.startswith("equity scheme") or "arbitrage" in cat or "aggressive hybrid" in cat:
        return "equity_fund"
    if cat.startswith("debt scheme"):
        return "debt_fund"
    if cat.startswith("other scheme") or not cat:
        if re.search(r"GOLD|SILVER", n):
            return "gold_silver_etf"
        if cat and re.search(r"NIFTY|SENSEX|INDEX|MIDCAP|SMALLCAP", n) and not re.search(
                r"GILT|G-SEC|SDL|DEBT|LIQUID|BOND|OVERSEAS|NASDAQ|US |INTERNATIONAL|GLOBAL", n):
            return "equity_fund"
    return "unclassified"


@dataclass
class Lot:
    qty: Decimal
    price: Decimal
    day: date


@dataclass
class Sale:
    isin: str
    name: str
    cls: str
    day: date
    qty: Decimal
    proceeds: Decimal
    cost: Decimal
    held_days: int
    term: str                         # short | long
    status: str = "computed"          # computed | not_computed
    reason: str = ""

    @property
    def gain(self) -> Decimal:
        return self.proceeds - self.cost


@dataclass
class FifoResult:
    sales: list[Sale] = field(default_factory=list)
    open_lots: dict[str, list[Lot]] = field(default_factory=dict)
    unknown_cost: list[dict] = field(default_factory=list)     # sold units with no purchase on file


def _price(t) -> Optional[Decimal]:
    if t.price is not None and t.price > 0:
        return D(t.price)
    if t.amount is not None and t.qty:
        return D(t.amount) / D(t.qty)
    return None


def fifo(trades: Iterable, names: dict[str, str], categories: dict[str, str | None]) -> FifoResult:
    """First-in-first-out per ISIN over effective trades (buys before sells on the same day)."""
    by: dict[str, list] = defaultdict(list)
    for t in trades:
        by[t.isin].append(t)
    out = FifoResult()
    for isin, ts in by.items():
        lots: list[Lot] = []
        cls, name = tax_class(isin, names.get(isin, ""), categories.get(isin)), names.get(isin, isin)
        for t in sorted(ts, key=lambda t: (t.trade_date, t.side != "B")):
            price = _price(t)
            if t.side == "B":
                if price is not None:
                    lots.append(Lot(D(t.qty), price, t.trade_date))
                continue
            need, cost, proceeds_unit = D(t.qty), ZERO, price
            taken: list[tuple[Lot, Decimal]] = []
            while need > 0 and lots:
                lot = lots[0]
                use = min(lot.qty, need)
                taken.append((Lot(use, lot.price, lot.day), use))
                lot.qty -= use
                need -= use
                if lot.qty <= 0:
                    lots.pop(0)
            if need > 0:
                out.unknown_cost.append({"isin": isin, "name": name, "date": t.trade_date.isoformat(), "units": float(need)})
            if proceeds_unit is None:
                continue
            for piece, use in taken:
                held = (t.trade_date - piece.day).days
                long_ = t.trade_date > add_months(piece.day, 12)
                s = Sale(isin, name, cls, t.trade_date, use, use * proceeds_unit, use * piece.price, held,
                         "long" if long_ else "short")
                if cls == "gold_silver_etf" and t.trade_date < BUDGET_2024:
                    s.status, s.reason = "not_computed", "gold/silver ETF sold before 23 Jul 2024: the 36-month rule is not modelled"
                elif cls == "debt_fund" and piece.day < DEBT_NEW_RULE:
                    s.status, s.reason = "not_computed", "debt fund bought before 1 Apr 2023: the older rule is not modelled"
                elif cls in ("other", "unclassified"):
                    s.status, s.reason = "not_computed", "fund type unknown" if cls == "unclassified" else "not a taxable listed security here"
                out.sales.append(s)
        out.open_lots[isin] = [l for l in lots if l.qty > 0]
    out.sales.sort(key=lambda s: s.day)
    return out


def _w(items: list[tuple[Decimal, Decimal]]) -> Decimal:
    """Gain-weighted average rate (%) of the positive gains in [(gain, rate)]."""
    pos = [(g, r) for g, r in items if g > 0]
    tot = sum((g for g, _ in pos), ZERO)
    return sum((g * r for g, r in pos), ZERO) / tot if tot else ZERO


def year_summary(sales: list[Sale], fy: str) -> dict:
    """Gains, set-off, exemption and estimated tax for one financial year of one person's sales."""
    mine = [s for s in sales if fy_of(s.day) == fy]
    comp = [s for s in mine if s.status == "computed"]
    eq = [s for s in comp if s.cls in ("equity_share", "equity_fund")]
    other = [s for s in comp if s.cls == "gold_silver_etf"]
    slab = [s for s in comp if s.cls == "debt_fund"]

    def rate(rule, d):
        return rules.value(rule, d)

    stcg = [(s.gain, rate("cg.equity.stcg_rate", s.day)) for s in eq if s.term == "short"]
    ltcg = [(s.gain, rate("cg.equity.ltcg_rate", s.day)) for s in eq if s.term == "long"]
    st_net, lt_net = sum((g for g, _ in stcg), ZERO), sum((g for g, _ in ltcg), ZERO)
    carried = ZERO
    if st_net < 0:                                  # a short-term loss sets off against long-term gains as well
        lt_net += st_net
        st_net = ZERO
    if lt_net < 0:
        carried, lt_net = -lt_net, ZERO             # a long-term loss is carried forward, never against short-term gains
    exemption = rate("cg.equity.ltcg_exemption", fy_end(fy))
    lt_taxable = max(lt_net - exemption, ZERO)
    tax_st = st_net * _w(stcg) / 100
    tax_lt = lt_taxable * _w(ltcg) / 100

    o_long = sum((s.gain for s in other if s.term == "long"), ZERO)
    o_short = sum((s.gain for s in other if s.term == "short"), ZERO)
    tax_o = max(o_long, ZERO) * rate("cg.listed_other.ltcg_rate", fy_end(fy)) / 100 if other else ZERO
    return {
        "fy": fy, "sales": len(mine),
        "equity": {"stcg": float(st_net), "ltcg": float(lt_net), "ltcg_exemption": float(exemption),
                   "ltcg_exemption_used": float(min(lt_net, exemption)), "ltcg_taxable": float(lt_taxable),
                   "loss_carried_forward": float(carried), "tax_stcg": float(tax_st), "tax_ltcg": float(tax_lt)},
        "gold_silver_etf": {"ltcg": float(o_long), "stcg_at_slab": float(o_short), "tax_ltcg": float(tax_o)},
        "debt_funds_at_slab": float(sum((s.gain for s in slab), ZERO)),
        "estimated_tax": float(tax_st + tax_lt + tax_o),
        "not_computed": [{"name": s.name, "date": s.day.isoformat(), "units": float(s.qty), "gain": float(s.gain), "reason": s.reason}
                         for s in mine if s.status != "computed"],
    }


def unrealised(open_lots: dict[str, list[Lot]], unit_price: dict[str, Decimal], names: dict[str, str],
               categories: dict[str, str | None], today: date, exemption_left: Decimal) -> dict:
    """If everything held were sold today: gain by class and term, and the tax at today's rates. Only lots with a purchase on file."""
    eq_st = eq_lt = oth_lt = oth_st = slab = ZERO
    rows = []
    for isin, lots in open_lots.items():
        px = unit_price.get(isin)
        cls = tax_class(isin, names.get(isin, ""), categories.get(isin))
        if px is None or not lots or cls in ("other", "unclassified"):
            continue
        g_st = g_lt = ZERO
        for l in lots:
            g = l.qty * (px - l.price)
            if today > add_months(l.day, 12):
                g_lt += g
            else:
                g_st += g
        if cls in ("equity_share", "equity_fund"):
            eq_st += g_st; eq_lt += g_lt
        elif cls == "gold_silver_etf":
            oth_st += g_st; oth_lt += g_lt
        else:
            slab += g_st + g_lt
        rows.append({"isin": isin, "name": names.get(isin, isin), "class": cls, "short_term": float(g_st), "long_term": float(g_lt)})
    st_taxable = max(eq_st, ZERO)
    lt_net = eq_lt + (eq_st if eq_st < 0 else ZERO)
    lt_taxable = max(lt_net - max(exemption_left, ZERO), ZERO)
    tax = (st_taxable * rules.value("cg.equity.stcg_rate", today) / 100 + lt_taxable * rules.value("cg.equity.ltcg_rate", today) / 100
           + max(oth_lt, ZERO) * rules.value("cg.listed_other.ltcg_rate", today) / 100)
    rows.sort(key=lambda r: -(r["short_term"] + r["long_term"]))
    return {"equity_short_term": float(eq_st), "equity_long_term": float(eq_lt), "exemption_left": float(max(exemption_left, ZERO)),
            "gold_silver_etf_long_term": float(oth_lt), "gold_silver_etf_short_term": float(oth_st), "debt_at_slab": float(slab),
            "estimated_tax_if_sold_today": float(tax), "holdings": rows[:12]}
