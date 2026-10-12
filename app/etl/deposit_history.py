"""Keep every statement's reading of every deposit / PPF account (see models.DepositSnapshot)."""
from __future__ import annotations

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models.schemas import DepositSnapshot


def record_snapshots(db: Session, owner_id: str, assets: list) -> int:
    """Idempotent: one row per (owner, account, statement date)."""
    rows = [{"owner_id": owner_id, "name": a.name, "asset_type": a.asset_type, "as_on": a.statement_date,
             "principal": a.current_value, "accrued": a.accrued_interest, "maturity_value": a.maturity_amount,
             "open_date": a.start_date}
            for a in assets if a.asset_type in ("fd", "ppf") and a.statement_date and a.current_value is not None
            and not getattr(a, "via_llm", False)]          # a number an LLM read is not history
    if not rows:
        return 0
    import uuid
    for r in rows:
        r["id"] = uuid.uuid4()
    stmt = pg_insert(DepositSnapshot.__table__).values(rows)
    stmt = stmt.on_conflict_do_update(
        constraint="uq_deposit_snapshot",
        set_={"principal": stmt.excluded.principal, "accrued": stmt.excluded.accrued,
              "maturity_value": stmt.excluded.maturity_value, "open_date": stmt.excluded.open_date})
    return db.execute(stmt).rowcount
