"""SQLAlchemy ORM models for the entire Finance OS."""
from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    Boolean, Column, Date, DateTime, ForeignKey,
    Numeric, String, Text, UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.models.database import Base


# ─────────────────────────────────────────────────────────────────────────────
# Owner  (user / wife)
# ─────────────────────────────────────────────────────────────────────────────
class Owner(Base):
    __tablename__ = "owners"

    id             = Column(String(20), primary_key=True)  # "user" | "wife"
    name           = Column(String(100), nullable=False)
    birth_date     = Column(Date)
    risk_appetite  = Column(String(20))   # conservative | moderate | aggressive
    retirement_age = Column(String(3))    # e.g. "60"
    created_at     = Column(DateTime(timezone=True), server_default=func.now())

    accounts     = relationship("Account",       back_populates="owner")
    manual_assets= relationship("ManualAsset",   back_populates="owner")
    goals        = relationship("Goal",          back_populates="owner")


# ─────────────────────────────────────────────────────────────────────────────
# Account  (bank / stocks / mutual_fund / ppf / fd / insurance)
# ─────────────────────────────────────────────────────────────────────────────
class Account(Base):
    __tablename__ = "accounts"

    id           = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id     = Column(String(20), ForeignKey("owners.id"), nullable=False)
    institution  = Column(String(60))   # HDFC, SBI, Zerodha, Coin …
    account_type = Column(String(30))   # bank | stocks | mutual_fund | ppf | fd
    account_ref  = Column(String(60))   # last-4 digits or client-id
    currency     = Column(String(3), default="INR")
    is_active    = Column(Boolean, default=True)
    created_at   = Column(DateTime(timezone=True), server_default=func.now())

    owner        = relationship("Owner",       back_populates="accounts")
    transactions = relationship("Transaction", back_populates="account")
    balances     = relationship("Balance",     back_populates="account")
    holdings     = relationship("Holding",     back_populates="account")

    __table_args__ = (
        UniqueConstraint("owner_id", "institution", "account_type", "account_ref",
                         name="uq_account"),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Transaction
# ─────────────────────────────────────────────────────────────────────────────
class Transaction(Base):
    __tablename__ = "transactions"

    id               = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    account_id       = Column(UUID(as_uuid=True), ForeignKey("accounts.id"), nullable=False)
    date             = Column(Date, nullable=False)
    value_date       = Column(Date)
    amount           = Column(Numeric(15, 2), nullable=False)
    transaction_type = Column(String(10))   # debit | credit
    description      = Column(String(500))
    raw_description  = Column(String(500))
    category         = Column(String(50))   # emi | insurance | salary | investment …
    subcategory      = Column(String(50))
    counterparty     = Column(String(200))
    reference_number = Column(String(100))
    balance_after    = Column(Numeric(15, 2))
    isin             = Column(String(12))   # populated for equity/MF trades; enables per-instrument XIRR
    source_file      = Column(String(300))
    fingerprint      = Column(String(64), unique=True)  # SHA-256 dedup key
    created_at       = Column(DateTime(timezone=True), server_default=func.now())

    account = relationship("Account", back_populates="transactions")


# ─────────────────────────────────────────────────────────────────────────────
# Holding  (point-in-time snapshot of stocks / MF units)
# ─────────────────────────────────────────────────────────────────────────────
class Holding(Base):
    __tablename__ = "holdings"

    id              = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    account_id      = Column(UUID(as_uuid=True), ForeignKey("accounts.id"), nullable=False)
    snapshot_date   = Column(Date, nullable=False)
    symbol          = Column(String(200))
    isin            = Column(String(12))
    instrument_name = Column(String(200))
    units           = Column(Numeric(15, 4))
    avg_cost_nav    = Column(Numeric(15, 4))
    current_nav     = Column(Numeric(15, 4))
    current_value   = Column(Numeric(15, 2))
    invested_value  = Column(Numeric(15, 2))
    gain_loss       = Column(Numeric(15, 2))
    gain_loss_pct   = Column(Numeric(8, 4))
    holding_type    = Column(String(20))  # equity | mutual_fund
    goal_tag        = Column(String(30))  # retirement | child_education | child_marriage | etc.
    first_buy_date  = Column(Date)        # earliest known purchase date for this position (CAGR denominator)
    source_file     = Column(String(300))
    created_at      = Column(DateTime(timezone=True), server_default=func.now())

    account = relationship("Account", back_populates="holdings")

    __table_args__ = (
        UniqueConstraint("account_id", "snapshot_date", "isin", name="uq_holding"),
    )


# ─────────────────────────────────────────────────────────────────────────────
# ClosedHolding  (archive of fully-sold equity/MF positions)
# Written by the normaliser before it deletes stale holdings on a full snapshot.
# Preserves the last known avg_cost_nav + close_value so XIRR can include
# round-trip returns on instruments that were completely exited.
# ─────────────────────────────────────────────────────────────────────────────
class ClosedHolding(Base):
    __tablename__ = "closed_holdings"

    id              = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    account_id      = Column(UUID(as_uuid=True), ForeignKey("accounts.id"), nullable=False)
    isin            = Column(String(12))
    symbol          = Column(String(200))
    instrument_name = Column(String(200))
    holding_type    = Column(String(20))   # equity | mutual_fund
    units           = Column(Numeric(15, 4))
    avg_cost_nav    = Column(Numeric(15, 4))
    invested_value  = Column(Numeric(15, 2))
    close_nav       = Column(Numeric(15, 4))   # NAV/price on the close date
    close_value     = Column(Numeric(15, 2))   # units × close_nav
    close_date      = Column(Date, nullable=False)
    first_seen_date = Column(Date)             # earliest snapshot_date for this position
    goal_tag        = Column(String(30))
    source_file     = Column(String(300))
    created_at      = Column(DateTime(timezone=True), server_default=func.now())

    account = relationship("Account")


# ─────────────────────────────────────────────────────────────────────────────
# ManualAssetContribution  (individual deposits into PPF / NPS / RD)
# Each row is one cash outflow; together they enable true XIRR on assets
# that receive recurring contributions instead of a single lump-sum.
# ─────────────────────────────────────────────────────────────────────────────
class ManualAssetContribution(Base):
    __tablename__ = "manual_asset_contributions"

    id            = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    asset_id      = Column(UUID(as_uuid=True), ForeignKey("manual_assets.id",
                             ondelete="CASCADE"), nullable=False)
    date          = Column(Date, nullable=False)
    amount        = Column(Numeric(15, 2), nullable=False)
    notes         = Column(String(200))
    created_at    = Column(DateTime(timezone=True), server_default=func.now())

    asset = relationship("ManualAsset", back_populates="contributions")


# ─────────────────────────────────────────────────────────────────────────────
# Balance  (closing balance per account per date)
# ─────────────────────────────────────────────────────────────────────────────
class Balance(Base):
    __tablename__ = "balances"

    id            = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    account_id    = Column(UUID(as_uuid=True), ForeignKey("accounts.id"), nullable=False)
    snapshot_date = Column(Date, nullable=False)
    balance       = Column(Numeric(15, 2), nullable=False)
    source_file   = Column(String(300))
    created_at    = Column(DateTime(timezone=True), server_default=func.now())

    account = relationship("Account", back_populates="balances")

    __table_args__ = (
        UniqueConstraint("account_id", "snapshot_date", name="uq_balance"),
    )


# ─────────────────────────────────────────────────────────────────────────────
# ManualAsset  (PPF / NPS / Gold / Real-Estate — no email statements)
# ─────────────────────────────────────────────────────────────────────────────
class ManualAsset(Base):
    __tablename__ = "manual_assets"

    id              = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id        = Column(String(20), ForeignKey("owners.id"), nullable=False)
    asset_type      = Column(String(30))   # ppf | fd | rd | nps | gold | silver | sgb | real_estate | insurance | other
    institution     = Column(String(100))
    name            = Column(String(200))
    current_value   = Column(Numeric(15, 2))
    invested_amount = Column(Numeric(15, 2))
    interest_rate   = Column(Numeric(6, 4))   # annual %, e.g. 7.1 for PPF
    quantity        = Column(Numeric(15, 4))  # grams for gold/silver; units for SGB
    metal_purity    = Column(String(4))       # '24K' | '22K' | '18K' (gold only)
    start_date           = Column(Date)
    maturity_date        = Column(Date)
    is_liquid            = Column(Boolean, default=False)  # MOD/sweep FDs count toward emergency fund
    notes                = Column(Text)
    source_statement_date = Column(Date)   # date of the statement this record came from
    goal_tag             = Column(String(30))  # emergency | retirement | child_education | child_marriage | health | wife_retirement
    last_updated         = Column(DateTime(timezone=True), server_default=func.now(),
                                  onupdate=func.now())

    owner         = relationship("Owner", back_populates="manual_assets")
    contributions = relationship("ManualAssetContribution", back_populates="asset",
                                 cascade="all, delete-orphan",
                                 order_by="ManualAssetContribution.date")


# ─────────────────────────────────────────────────────────────────────────────
# NetWorthSnapshot  (aggregated daily snapshot)
# ─────────────────────────────────────────────────────────────────────────────
class NetWorthSnapshot(Base):
    __tablename__ = "net_worth_snapshots"

    id                = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    snapshot_date     = Column(Date, nullable=False)
    owner_id          = Column(String(20))   # user | wife | family
    bank_balance      = Column(Numeric(15, 2), default=0)
    equity_value      = Column(Numeric(15, 2), default=0)
    mf_value          = Column(Numeric(15, 2), default=0)
    ppf_value         = Column(Numeric(15, 2), default=0)
    fd_value          = Column(Numeric(15, 2), default=0)
    real_estate_value = Column(Numeric(15, 2), default=0)
    other_assets      = Column(Numeric(15, 2), default=0)
    total_assets      = Column(Numeric(15, 2), default=0)
    total_liabilities = Column(Numeric(15, 2), default=0)
    net_worth         = Column(Numeric(15, 2), default=0)
    created_at        = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("snapshot_date", "owner_id", name="uq_networth_snapshot"),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Goal  (financial goals with projections)
# ─────────────────────────────────────────────────────────────────────────────
class Goal(Base):
    __tablename__ = "goals"

    id                   = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id             = Column(String(20), ForeignKey("owners.id"), nullable=False)
    name                 = Column(String(200), nullable=False)
    goal_type            = Column(String(30))  # child_education | child_marriage |
                                               # retirement | health | emergency | custom
    target_amount        = Column(Numeric(15, 2))
    target_date          = Column(Date)
    current_corpus       = Column(Numeric(15, 2), default=0)
    monthly_sip_needed   = Column(Numeric(15, 2))
    expected_return_rate = Column(Numeric(6, 4), default=12.0)   # % per year
    inflation_rate       = Column(Numeric(6, 4), default=6.0)    # % per year
    notes                = Column(Text)
    created_at           = Column(DateTime(timezone=True), server_default=func.now())
    updated_at           = Column(DateTime(timezone=True), server_default=func.now(),
                                  onupdate=func.now())

    owner = relationship("Owner", back_populates="goals")
