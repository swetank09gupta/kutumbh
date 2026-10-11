"""
Local-LLM tagging of payees the rules cannot place.

The model only ever sees the payee name, one sample narration and the direction/amount — never
account numbers or balances — and runs on this machine. Its answer is a *suggestion*: it is stored
with a confidence and applied automatically only when confident; everything else goes to the Review
list for you to label once (app.txn.pipeline).
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from app.llm import ollama

logger = logging.getLogger(__name__)

TAXONOMY: dict[str, list[str]] = {
    "food": ["dining_out", "groceries"],
    "shopping": ["online", "upi_merchant"],
    "transport": ["travel_commute", "fuel", "toll"],
    "utilities": ["electricity", "water", "gas", "internet", "mobile", "cable_tv"],
    "housing": ["rent_maintenance", "maintenance", "renovation"],
    "medical": [],
    "entertainment": ["subscriptions", "recreation"],
    "child_expenses": ["education_care"],
    "family_support": [],
    "insurance": [],
    "taxes": [],
    "cash": [],
    "other": [],
}
# Local models are overconfident on names (a 3B model tagged people as "shopping" at 0.9), so a
# suggestion is never applied automatically: it pre-fills the Review list and you confirm it.
AUTO_APPLY_CONFIDENCE = None
_PROMPT = """You label payees on an Indian family's bank statement.

For each item decide "kind":
- "business": a shop, brand, service, company or app (e.g. SWIGGY, FIRSTCRY, APOLLO PHARMACY, EKART)
- "person": a person's name, or an account/phone number (e.g. RAJESH B, DEEPIKA, MOHAMMAD, Bank Acc)
Only a "business" gets a category. A "person" always gets category "other" and confidence 0.2,
because the reason for paying a person cannot be known from a statement.

Categories and subcategories: {taxonomy}

Examples:
payee="SWIGGY" -> {{"kind":"business","category":"food","subcategory":"dining_out","confidence":0.95}}
payee="APOLLO PHARMACY" -> {{"kind":"business","category":"medical","subcategory":null,"confidence":0.9}}
payee="RAJESH B" -> {{"kind":"person","category":"other","subcategory":null,"confidence":0.2}}
payee="Bank Acc" -> {{"kind":"person","category":"other","subcategory":null,"confidence":0.2}}

Answer as JSON: {{"results":[{{"id":0,"kind":"business","category":"...","subcategory":null,"confidence":0.9}}]}}

Items:
{items}
"""


def _model() -> Optional[str]:
    # 7B follows the person/business instruction (3B tagged people as shopping at 0.9 confidence)
    return ollama.pick_model(os.getenv("OLLAMA_CATEGORISE_MODEL"), "qwen2.5:7b", os.getenv("OLLAMA_MODEL"), "llama3.2")


def classify(items: list[dict]) -> dict[str, tuple[str, Optional[str], float]]:
    """
    items: [{"key", "payee", "sample", "direction", "amount"}] → {key: (category, subcategory, confidence)}.
    Unknown categories, malformed rows and an unavailable model yield no entry (stays unreviewed).
    """
    model = _model()
    if not model or not items:
        return {}
    taxonomy = "; ".join(f"{c}" + (f" ({', '.join(s)})" if s else "") for c, s in TAXONOMY.items())
    out: dict[str, tuple[str, Optional[str], float]] = {}
    for i in range(0, len(items), 12):
        batch = items[i:i + 12]
        lines = "\n".join(
            f'{n}. payee="{b["payee"]}" narration="{(b["sample"] or "")[:70]}" {b["direction"]} ~₹{b["amount"]:,.0f}'
            for n, b in enumerate(batch))
        res = ollama.generate_json(_PROMPT.format(taxonomy=taxonomy, items=lines), model)
        rows = (res or {}).get("results") if isinstance(res, dict) else None
        for r in rows or []:
            try:
                b = batch[int(r["id"])]
                cat = str(r["category"]).strip().lower()
                sub = (r.get("subcategory") or None)
                conf = max(0.0, min(1.0, float(r.get("confidence", 0))))
            except (KeyError, ValueError, IndexError, TypeError):
                continue
            if cat not in TAXONOMY:
                continue
            sub = sub if sub in TAXONOMY[cat] else None
            if str(r.get("kind", "")).lower() != "business" or cat == "other":
                cat, sub, conf = "other", None, min(conf, 0.2)
            out[b["key"]] = (cat, sub, round(conf, 2))
    return out
