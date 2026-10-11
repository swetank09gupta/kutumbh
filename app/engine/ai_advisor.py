"""
Local AI financial advisor using Ollama (Llama 3.2 or any local model).
All data stays on your machine — nothing is sent to external servers.

Requires Ollama running on the host:
  brew install ollama          (Mac)
  ollama pull llama3.2
  ollama serve
"""
from __future__ import annotations

import json
import logging
import os
import urllib.request
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

OLLAMA_HOST  = os.getenv("OLLAMA_HOST", "http://ollama:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.2")
ZERO         = Decimal(0)


def _age(birth_date) -> int | None:
    if not birth_date:
        return None
    today = date.today()
    return today.year - birth_date.year - (
        (today.month, today.day) < (birth_date.month, birth_date.day)
    )


def _risk_label(age: int | None, risk_appetite: str | None) -> str:
    if risk_appetite and risk_appetite != "moderate":
        return risk_appetite
    if age is None:
        return "moderate"
    if age < 35:
        return "aggressive"
    if age < 45:
        return "moderate-aggressive"
    if age < 55:
        return "moderate"
    return "conservative"


def build_context(db: Session) -> dict:
    from sqlalchemy import func
    from app.models.schemas import (
        Owner, Account, Transaction, NetWorthSnapshot, ManualAsset,
    )
    from app.engine.goals import project_all_goals
    from app.engine.inflation import get_india_cpi

    # Owner profiles
    owners   = db.query(Owner).filter(Owner.id != "family").all()
    profiles = []
    for o in owners:
        bd   = getattr(o, "birth_date", None)
        risk = getattr(o, "risk_appetite", None)
        ret_age = getattr(o, "retirement_age", 60)
        age  = _age(bd)
        profiles.append({
            "name":           o.name,
            "age":            age,
            "risk_appetite":  _risk_label(age, risk),
            "retirement_age": ret_age or 60,
        })

    # Latest family net worth
    nw    = (
        db.query(NetWorthSnapshot).filter_by(owner_id="family")
        .order_by(NetWorthSnapshot.snapshot_date.desc()).first()
    )
    # Allocation is judged on financial assets: real estate cannot be rebalanced with SIPs.
    total = float((nw.total_assets or 0) - (nw.real_estate_value or 0)) if nw else 0
    networth = {
        "total":       total,
        "bank":        float(nw.bank_balance  or 0) if nw else 0,
        "equity":      float(nw.equity_value  or 0) if nw else 0,
        "mf":          float(nw.mf_value      or 0) if nw else 0,
        "ppf":         float(nw.ppf_value     or 0) if nw else 0,
        "fd":          float(nw.fd_value      or 0) if nw else 0,
        "other":       float(nw.other_assets  or 0) if nw else 0,
        "equity_pct":  round((float((nw.equity_value or 0) + (nw.mf_value or 0))) / total * 100, 1)
                       if total else 0,
        "debt_pct":    round((float((nw.ppf_value or 0) + (nw.fd_value or 0))) / total * 100, 1)
                       if total else 0,
    }

    # Manual assets (gold, silver, SGB, etc.)
    manual_assets = [
        {"type": m.asset_type, "name": m.name,
         "value": float(m.current_value or 0), "owner": m.owner_id,
         "maturity_date": str(m.maturity_date) if m.maturity_date else None,
         "rate": float(m.interest_rate or 0)}
        for m in db.query(ManualAsset).all()
    ]

    # Cash flow — last 3 full months, from the shared definitions (app.semantic), so the advisor
    # quotes the same income and spending as every page.
    from sqlalchemy import text
    start = date.today().replace(day=1)
    for _ in range(3):
        start = (start - timedelta(days=1)).replace(day=1)
    window = "date >= :s AND date < date_trunc('month', CURRENT_DATE)"
    income = db.execute(text(f"SELECT COALESCE(SUM(amount),0) FROM v_cashflow WHERE flow='income' AND {window}"),
                        {"s": start}).scalar() or ZERO
    expenses = db.execute(text(f"SELECT COALESCE(SUM(amount),0) FROM v_cashflow WHERE flow='expense' AND {window}"),
                          {"s": start}).scalar() or ZERO
    top_cats = db.execute(text(
        f"SELECT category, SUM(amount) AS t FROM v_cashflow WHERE flow='expense' AND category IS NOT NULL AND {window} "
        "GROUP BY category ORDER BY 2 DESC LIMIT 6"), {"s": start}).all()

    monthly_income   = round(float(income)   / 3, 0)
    monthly_expenses = round(float(expenses) / 3, 0)
    savings_rate     = round((monthly_income - monthly_expenses) / monthly_income * 100, 1) \
                       if monthly_income > 0 else 0

    # Goals
    goals_proj = project_all_goals(db)
    goals_data = [
        {
            "name":        p.name,
            "type":        p.goal_type,
            "target":      p.inflation_adj_target,
            "target_date": str(p.target_date),
            "corpus":      p.current_corpus,
            "on_track":    p.on_track,
            "sip_needed":  p.monthly_sip_needed,
            "gap":         p.gap,
            "months_left": p.months_to_goal,
        }
        for p in goals_proj
    ]

    return {
        "profiles":          profiles,
        "networth":          networth,
        "manual_assets":     [a for a in manual_assets if a["value"] > 0],
        "monthly_income":    monthly_income,
        "monthly_expenses":  monthly_expenses,
        "savings_rate":      savings_rate,
        "top_categories":    [{"cat": c, "monthly": round(float(t) / 3, 0)} for c, t in top_cats],
        "goals":             goals_data,
        "india_cpi":         get_india_cpi(),
    }


def _build_prompt(ctx: dict) -> str:
    nw  = ctx["networth"]
    profiles_txt = "\n".join(
        f"  • {p['name']}: age {p['age'] or 'unknown'}, risk={p['risk_appetite']}, "
        f"target retirement age {p['retirement_age']}"
        for p in ctx["profiles"]
    )
    goals_txt = "\n".join(
        f"  • {g['name']} ({g['type']}): target ₹{g['target']:,.0f} by {g['target_date']}, "
        f"corpus ₹{g['corpus']:,.0f}, {'✓ ON TRACK' if g['on_track'] else '✗ BEHIND'}, "
        f"gap ₹{abs(g['gap']):,.0f}, {g['months_left']} months left"
        for g in ctx["goals"]
    ) or "  No goals defined yet."
    manual_txt = "\n".join(
        f"  • {a['name']} ({a['type']}): ₹{a['value']:,.0f}"
        + (f" @ {a['rate']:.1f}% till {a['maturity_date']}" if a['rate'] > 0 else "")
        for a in ctx["manual_assets"]
    ) or "  None entered yet."
    cats_txt = "\n".join(
        f"  • {c['cat']}: ₹{c['monthly']:,.0f}/mo" for c in ctx["top_categories"]
    )
    equity_total = nw["equity"] + nw["mf"]

    return f"""You are a personal finance advisor for an Indian family. Be specific, practical, and use Indian financial instruments (PPF, ELSS, NPS, SGB, SIP, etc.).

FAMILY PROFILE:
{profiles_txt}

NET WORTH — Family (₹):
  Total:          ₹{nw['total']:>12,.0f}
  Bank/Liquid:    ₹{nw['bank']:>12,.0f}
  Equity+MF:      ₹{equity_total:>12,.0f}  ({nw['equity_pct']:.0f}% of portfolio)
    Direct equity:  ₹{nw['equity']:>12,.0f}
    Mutual funds:   ₹{nw['mf']:>12,.0f}
  PPF:            ₹{nw['ppf']:>12,.0f}
  FD/RD:          ₹{nw['fd']:>12,.0f}
  Other assets:   ₹{nw['other']:>12,.0f}

MANUAL ASSETS (Gold / SGB / Silver / etc.):
{manual_txt}

CASH FLOW (monthly avg, last 3 months):
  Income:   ₹{ctx['monthly_income']:,.0f}
  Expenses: ₹{ctx['monthly_expenses']:,.0f}
  Savings rate: {ctx['savings_rate']:.1f}%

TOP EXPENSE CATEGORIES:
{cats_txt}

FINANCIAL GOALS:
{goals_txt}

INDIA CPI INFLATION: {ctx['india_cpi']:.1f}% (live data)

---
Provide your analysis in exactly this format:

HEALTH SCORE: [0-100]/100 — [one sentence why]

PORTFOLIO ASSESSMENT:
[2-3 sentences on asset allocation vs their age/risk profile. Is equity % appropriate? Is gold/SGB exposure good? Any concentration risk?]

TOP 3 RECOMMENDATIONS:
1. [Specific action with ₹ amount and timeline]
2. [Specific action with ₹ amount and timeline]
3. [Specific action with ₹ amount and timeline]

GOALS REVIEW:
[One line per goal: status + most important action]

NEXT 12 MONTHS FOCUS:
[2-3 sentences tailored to their ages and life stage — what should they prioritize this year?]

Be concise. Think step-by-step before answering."""


def get_ai_insights(db: Session) -> dict:
    """Call Ollama locally for AI-powered financial insights. Fully private."""
    try:
        ctx    = build_context(db)
        prompt = _build_prompt(ctx)
    except Exception as exc:
        logger.error("Context build failed: %s", exc, exc_info=True)
        return {"status": "error", "error": f"Context build failed: {exc}"}

    # Resolve against what is installed: asking for a model that is not pulled returns a 404,
    # which the card used to report as "Ollama not reachable".
    from app.llm import ollama as _ollama
    model = _ollama.pick_model(OLLAMA_MODEL, "qwen2.5:7b", "llama3.2")
    if model is None:
        return {"status": "unavailable",
                "message": f"No model installed at {OLLAMA_HOST}. Run: docker exec finance_ollama ollama pull {OLLAMA_MODEL}"}

    payload = json.dumps({
        "model":   model,
        "prompt":  prompt,
        "stream":  False,
        "options": {"temperature": 0.3, "num_predict": 1200},
    }).encode()

    try:
        req = urllib.request.Request(
            f"{OLLAMA_HOST}/api/generate",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=180) as resp:
            result = json.loads(resp.read())

        return {
            "status":   "ok",
            "model":    model,
            "response": result.get("response", "").strip(),
            "context":  {
                "net_worth":      ctx["networth"]["total"],
                "monthly_income": ctx["monthly_income"],
                "savings_rate":   ctx["savings_rate"],
                "goals_count":    len(ctx["goals"]),
                "india_cpi":      ctx["india_cpi"],
            },
        }
    except OSError as exc:
        msg = (
            f"Ollama not reachable at {OLLAMA_HOST}.\n"
            f"To enable AI insights (100% local):\n"
            f"  1. Install: https://ollama.ai\n"
            f"  2. Pull model: ollama pull {OLLAMA_MODEL}\n"
            f"  3. Start: ollama serve\n"
            f"  (Error: {exc})"
        )
        return {"status": "unavailable", "message": msg}
    except Exception as exc:
        logger.error("Ollama call failed: %s", exc)
        return {"status": "error", "error": str(exc)}
