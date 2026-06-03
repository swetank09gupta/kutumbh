"""
Shared accessor for the latest_holdings_deduped PostgreSQL view.

This is the single place that queries current holdings — all API endpoints
and engine modules import from here so dedup logic lives in one spot (the view).
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session


def latest_holdings(
    db: Session,
    *,
    owner_ids: list[str] | None = None,
    account_types: tuple[str, ...] | list[str] | None = None,
):
    """
    Return deduplicated current holdings from the view.

    The view handles:
      1. Latest snapshot per account
      2. Dedup by (owner_id, ISIN) — keeps the most recent snapshot_date
         when the same holding appears in multiple accounts (e.g. Zerodha + CDSL)

    Returns SQLAlchemy Row objects with attribute access.
    """
    conditions: list[str] = []
    params: dict = {}

    if owner_ids is not None:
        conditions.append("owner_id = ANY(:owner_ids)")
        params["owner_ids"] = owner_ids
    if account_types is not None:
        conditions.append("account_type = ANY(:account_types)")
        params["account_types"] = list(account_types)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    sql = text(
        f"SELECT * FROM latest_holdings_deduped {where} "
        "ORDER BY current_value DESC NULLS LAST"
    )
    return db.execute(sql, params).all()
