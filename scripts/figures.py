"""
Key figures of the live system, and what moved since the last accepted baseline.

    python scripts/figures.py            # show what changed vs data/figures_baseline.json
    python scripts/figures.py --accept   # the changes are intended: make this the new baseline

Run it before and after a change: any number that moves and that the change was not meant to move
is a regression. The baseline lives in data/ (private, git-ignored).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

BASELINE = Path("data/figures_baseline.json")


def collect() -> dict:
    from app.engine.goals import project_all_goals
    from app.engine.networth import real_owners
    from app.engine.portfolio_xirr import compute_owner_returns
    from app.engine.settings_store import get_inflation
    from app.models.database import SessionLocal
    from app.models.schemas import ManualAsset
    from app.consistency import _latest_nw
    db = SessionLocal()
    try:
        out: dict = {"inflation_pct": get_inflation(db)}
        nw = _latest_nw(db)
        for o in [x.id for x in real_owners(db)] + ["family"]:
            r = compute_owner_returns(db, o)
            out[f"{o}.xirr_pct"] = r["total"]["xirr_pct"]
            out[f"{o}.value_all"] = r["total"]["value_all"]
            out[f"{o}.coverage_pct"] = r["total"]["coverage_pct"]
            for c in r["by_class"]:
                out[f"{o}.class.{c['class']}.value"] = c["current_all"]
                out[f"{o}.class.{c['class']}.xirr_pct"] = c["xirr_pct"]
            out[f"{o}.manual_rows"] = len(r["manual_assets"])
            out[f"{o}.instruments"] = len(r["instruments"])
            if o in nw:
                for k in ("bank_balance", "equity_value", "mf_value", "ppf_value", "fd_value", "total_liabilities", "net_worth"):
                    out[f"{o}.nw.{k}"] = float(getattr(nw[o], k) or 0)
        for p in project_all_goals(db):
            out[f"goal.{p.name}.sip_needed"] = round(p.monthly_sip_needed)
            out[f"goal.{p.name}.corpus"] = round(p.current_corpus)
        liab = db.query(ManualAsset).filter(ManualAsset.asset_type == "liability").first()
        out["loan.principal"] = abs(float(liab.current_value)) if liab else None
        return json.loads(json.dumps(out, default=float))
    finally:
        db.close()


def diff(old: dict, new: dict) -> list[str]:
    rows = []
    for k in sorted(set(old) | set(new)):
        a, b = old.get(k), new.get(k)
        if a == b:
            continue
        if isinstance(a, (int, float)) and isinstance(b, (int, float)) and abs(a - b) < 0.5:
            continue
        rows.append(f"  {k:55} {a!s:>16} -> {b!s:<16}")
    return rows


if __name__ == "__main__":
    new = collect()
    if not BASELINE.exists():
        BASELINE.parent.mkdir(parents=True, exist_ok=True)
        BASELINE.write_text(json.dumps(new, indent=1, sort_keys=True))
        print(f"baseline created: {len(new)} figures")
        sys.exit(0)
    changes = diff(json.loads(BASELINE.read_text()), new)
    if "--accept" in sys.argv:
        BASELINE.write_text(json.dumps(new, indent=1, sort_keys=True))
        print(f"baseline updated ({len(changes)} figures changed)")
    elif changes:
        print(f"{len(changes)} figures moved since the baseline:")
        print("\n".join(changes))
        sys.exit(1)
    else:
        print(f"no figure moved ({len(new)} checked)")
