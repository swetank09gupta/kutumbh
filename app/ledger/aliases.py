"""Owner-scoped ISIN alias lookup shared by ingest and the ledger engine."""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.schemas import IsinAlias


def load_aliases(db: Session) -> dict[tuple[str, str], str]:
    """{(owner_id, alias_isin): depository isin}"""
    return {(a.owner_id, a.alias_isin): a.isin for a in db.query(IsinAlias).all()}


def canonical(owner_id: str, isin: str | None, aliases: dict[tuple[str, str], str]) -> str | None:
    return aliases.get((owner_id, isin), isin) if isin else isin
