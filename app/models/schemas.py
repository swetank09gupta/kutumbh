"""SQLAlchemy ORM models for the entire Finance OS."""
from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    Boolean, Column, Date, DateTime, ForeignKey, Integer,
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
    tax_regime     = Column(String(10), default="old")   # "old" | "new" — drives 80C dead-weight check
    epf_monthly    = Column(Numeric(10, 2), default=0)   # employee EPF / month (₹)
    home_loan_share_pct = Column(Numeric(5, 2), default=0)  # % of home-loan EMI this owner can claim under 80C (principal) / 24(b) (interest)
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
    # How `category` was decided (app.txn.pipeline): trade | rule | paired | memory | llm | user.
    # 'user' and 'paired' are never overwritten by automatic re-tagging.
    category_source  = Column(String(10))
    category_conf    = Column(Numeric(4, 2))            # 0-1; only meaningful for llm/memory
    needs_review     = Column(Boolean, default=False)   # low-confidence guess, waiting in the Review list
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
    source_file     = Column(String(300))   # 'ledger:rollforward' for rows derived by app.ledger
    cost_basis_quality = Column(String(10))  # exact | partial | None (statement rows carry no cost)
    price_source    = Column(String(20))     # amfi | yahoo | last_trade | statement (ledger rows only)
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
class RecommendationSnapshot(Base):
    """
    Persistent memory of recommendations issued over time.

    A *fingerprint* is `owner_id|category|title` — used to detect "same rec
    persisting" vs "new rec appeared" vs "resolved". The detail/action fields
    can drift across snapshots (₹ amounts change as portfolio grows); the
    fingerprint stays stable so we can track lifecycle.

    metrics: JSON of the key numbers that drove the rec (e.g.
        {"midcap_pct": 26.1, "equity_total": 3246595, "nifty50_pe": 20.2}).
    Useful for showing trend (midcap % went 28 → 26 → 24 over 3 months).
    """
    __tablename__ = "recommendation_snapshots"

    id            = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    snapshot_date = Column(Date, nullable=False)
    owner_id      = Column(String(20), nullable=False, default="family")
    fingerprint   = Column(String(120), nullable=False)  # category|title (stable across time)
    priority      = Column(String(10), nullable=False)
    category      = Column(String(40), nullable=False)
    title         = Column(String(120), nullable=False)
    detail        = Column(Text)
    action        = Column(Text)
    metrics       = Column(Text)  # JSON-encoded
    created_at    = Column(DateTime(timezone=True), server_default=func.now())


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
    monthly_payment = Column(Numeric(15, 2))  # EMI for liabilities; SIP for assets (optional)
    quantity        = Column(Numeric(15, 4))  # grams for gold/silver; units for SGB
    metal_purity    = Column(String(4))       # '24K' | '22K' | '18K' (gold only)
    start_date           = Column(Date)
    maturity_date        = Column(Date)
    is_liquid            = Column(Boolean, default=False)  # MOD/sweep FDs count toward emergency fund
    notes                = Column(Text)
    source_statement_date = Column(Date)   # date of the statement this record came from
    accrued_interest     = Column(Numeric(15, 2))  # interest earned but not yet added to the principal, at that statement date
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
    expected_return_rate = Column(Numeric(6, 4), default=12.0)   # % per year (manual override)
    inflation_rate       = Column(Numeric(6, 4), default=6.0)    # % per year
    use_manual_return    = Column(Boolean, default=False)        # False = use blended XIRR from tagged holdings
    sip_stepup_pct       = Column(Numeric(5, 2))                 # % per year; NULL = auto-detect
    glide_enabled        = Column(Boolean, default=True)         # taper return toward end_return_pct near goal date
    glide_start_years_before = Column(Integer, default=3)
    glide_end_return_pct = Column(Numeric(5, 2), default=8.0)
    notes                = Column(Text)
    created_at           = Column(DateTime(timezone=True), server_default=func.now())
    updated_at           = Column(DateTime(timezone=True), server_default=func.now(),
                                  onupdate=func.now())

    owner = relationship("Owner", back_populates="goals")


# ─────────────────────────────────────────────────────────────────────────────
# Instrument ledger — see docs/adr/0023-instrument-ledger.md
#
# InstrumentTrade   one row per executed trade (exact date, qty, price). Written
#                   by contract-note, Coin and tradebook parsers. Idempotent via
#                   natural_key. Cross-source overlap is resolved by source rank
#                   at read time (app.ledger.rules.effective_trades), never by
#                   deleting rows.
# DematMovement     one row per line of the monthly demat statement's transaction
#                   section (quantity truth, settlement date, running balance).
#                   Evidence only: it is matched to trades, never added to them.
# ReconIssue        everything the reconciliation could not prove. Re-derived on
#                   every run; rows that stop being detected are marked resolved.
# ─────────────────────────────────────────────────────────────────────────────
class InstrumentTrade(Base):
    __tablename__ = "instrument_trades"

    id               = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id         = Column(String(20), nullable=False)
    account_id       = Column(UUID(as_uuid=True), ForeignKey("accounts.id"), nullable=False)
    isin             = Column(String(12))              # NULL = unresolved (reported, not counted)
    symbol           = Column(String(200))
    instrument_name  = Column(String(200))
    holding_type     = Column(String(20))              # equity | mutual_fund | bonds
    trade_date       = Column(Date, nullable=False)
    settle_date      = Column(Date)                    # exact when the contract note prints it
    side             = Column(String(1), nullable=False)   # B | S
    quantity         = Column(Numeric(18, 6), nullable=False)
    price            = Column(Numeric(18, 6))          # gross per unit
    amount           = Column(Numeric(15, 2))          # cash incl. charges where the source has them
    source           = Column(String(20), nullable=False)  # contract_note | coin | tradebook | opening
    source_ref       = Column(String(120))             # trade no / folio / trade id
    source_file      = Column(String(300))
    natural_key      = Column(String(300), nullable=False, unique=True)
    txn_fingerprint  = Column(String(64))              # link to the cashflow row in transactions
    settled_movement_id = Column(UUID(as_uuid=True))   # DematMovement that settled it (set by reconcile)
    isin_override    = Column(String(12))              # this one trade belongs to another ISIN of the scheme (learned)
    created_at       = Column(DateTime(timezone=True), server_default=func.now())


class DematMovement(Base):
    __tablename__ = "demat_movements"

    id          = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id    = Column(String(20), nullable=False)
    account_id  = Column(UUID(as_uuid=True), ForeignKey("accounts.id"), nullable=False)
    isin        = Column(String(12), nullable=False)
    symbol      = Column(String(200))
    move_date   = Column(Date, nullable=False)
    qty_in      = Column(Numeric(18, 6), nullable=False, default=0)
    qty_out     = Column(Numeric(18, 6), nullable=False, default=0)
    running_qty = Column(Numeric(18, 6))
    seq         = Column(Integer)                      # line order within the ISIN block of its statement
    statement_date = Column(Date)                      # the statement's "Holdings as on" date
    description = Column(String(300))                  # e.g. "NSE Payout …", "Inter Depository Transfer …"
    source_file = Column(String(300))
    natural_key = Column(String(300), nullable=False, unique=True)
    created_at  = Column(DateTime(timezone=True), server_default=func.now())


class ReconIssue(Base):
    __tablename__ = "recon_issues"

    id            = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    issue_key     = Column(String(300), nullable=False, unique=True)
    owner_id      = Column(String(20))
    isin          = Column(String(12))
    instrument_name = Column(String(200))
    kind          = Column(String(40), nullable=False)
    severity      = Column(String(10), nullable=False)   # error | warn | info
    as_of_date    = Column(Date)
    ledger_qty    = Column(Numeric(18, 6))
    statement_qty = Column(Numeric(18, 6))
    detail        = Column(Text)
    status        = Column(String(10), nullable=False, default="open")   # open | resolved
    first_seen    = Column(DateTime(timezone=True), server_default=func.now())
    last_seen     = Column(DateTime(timezone=True), server_default=func.now())
    resolved_at   = Column(DateTime(timezone=True))


class IsinAlias(Base):
    """
    AMFI lists two ISINs per fund scheme and its name lookup can pick the wrong one, so a
    Coin e-mail's ISIN may differ from the demat's. Learned per owner from exact unit matches
    between trades and demat lines (app.ledger.engine.canonicalize_isins) and applied to
    TRADE rows only at ingest — statement and demat ISINs are the depository's and are never
    rewritten.
    """
    __tablename__ = "isin_aliases"

    owner_id   = Column(String(20), primary_key=True)   # per owner: an ISIN can be real for one owner
    alias_isin = Column(String(12), primary_key=True)   # and an AMFI-lookup mistake for another
    isin       = Column(String(12), nullable=False)     # the ISIN the depository uses
    votes      = Column(Integer, nullable=False, default=1)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class StatementBalance(Base):
    """
    Closing balance per ISIN from one monthly demat statement, kept per statement family
    ('zerodha' | 'cdsl') and never de-duplicated across families: CDSL's CAS also lists
    holdings outside the Zerodha demat, so the two have different scope. The ledger
    reconciles within one family. isin='' registers a statement date that held nothing.
    """
    __tablename__ = "statement_balances"

    id          = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id    = Column(String(20), nullable=False)
    family      = Column(String(10), nullable=False)
    as_on       = Column(Date, nullable=False)
    isin        = Column(String(12), nullable=False, default="")
    qty         = Column(Numeric(18, 6), nullable=False, default=0)
    name        = Column(String(200))
    rate        = Column(Numeric(15, 4))
    source_file = Column(String(300))

    __table_args__ = (UniqueConstraint("owner_id", "family", "as_on", "isin", name="uq_statement_balance"),)


class BankAlertRow(Base):
    """
    One bank alert e-mail (HDFC Insta Alert, SBI CBSSBI/NEFT/YONO). Evidence only — it is never
    counted as spending or income. app.txn.alert_recon links it to the statement row that later
    confirms it (status 'matched'), leaves it 'pending' while the statement has not arrived, and
    flags 'unmatched' when the statement covers the date but has no such row (a parse gap).
    """
    __tablename__ = "bank_alerts"

    id             = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id       = Column(String(20), nullable=False)
    bank           = Column(String(10), nullable=False)
    account_last4  = Column(String(4))
    direction      = Column(String(6), nullable=False)
    amount         = Column(Numeric(15, 2), nullable=False)
    txn_date       = Column(Date, nullable=False)
    kind           = Column(String(10), nullable=False)
    payee          = Column(String(200))
    vpa            = Column(String(120))
    ref            = Column(String(80))
    balance_after  = Column(Numeric(15, 2))
    narration      = Column(String(300))
    sent_at        = Column(DateTime(timezone=True))
    status         = Column(String(10), nullable=False, default="new")   # new|matched|pending|unmatched|dup|outside|untracked
    matched_txn_id = Column(UUID(as_uuid=True))
    natural_key    = Column(String(300), nullable=False, unique=True)
    created_at     = Column(DateTime(timezone=True), server_default=func.now())


class LoanAnchor(Base):
    """
    What the lender says is still owed, straight from its own letter (HDFC part-prepayment
    confirmation: "principal outstanding after this prepayment ... will be Rs. N"). The newest
    anchor, rolled forward by the EMIs debited since, is the loan liability in net worth.
    """
    __tablename__ = "loan_anchors"

    id              = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id        = Column(String(20), nullable=False)
    lender          = Column(String(20), nullable=False)
    loan_no         = Column(String(30), nullable=False)
    as_on           = Column(Date, nullable=False)
    prepaid         = Column(Numeric(15, 2))
    principal_after = Column(Numeric(15, 2), nullable=False)
    emi             = Column(Numeric(15, 2))
    rate            = Column(Numeric(6, 3))
    emis_left       = Column(Integer)
    source_file     = Column(String(300))
    natural_key     = Column(String(120), nullable=False, unique=True)
    created_at      = Column(DateTime(timezone=True), server_default=func.now())


class AppSetting(Base):
    """App-wide assumptions that are not per-person or per-goal (e.g. the inflation rate)."""
    __tablename__ = "app_settings"

    key        = Column(String(50), primary_key=True)
    value      = Column(String(200), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class DepositSnapshot(Base):
    """
    What one monthly bank statement said about one deposit or PPF account: principal, interest accrued,
    maturity value. The series is the account's history — PPF contributions are its balance steps, a part-withdrawn
    deposit shows as a drop in principal — which the statements print but the current-state tables cannot keep.
    """
    __tablename__ = "deposit_snapshots"

    id             = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id       = Column(String(20), nullable=False)
    name           = Column(String(100), nullable=False)       # "SBI FD ...7113", "SBI PPF"
    asset_type     = Column(String(10), nullable=False)        # fd | ppf
    as_on          = Column(Date, nullable=False)
    principal      = Column(Numeric(15, 2), nullable=False)
    accrued        = Column(Numeric(15, 2))
    maturity_value = Column(Numeric(15, 2))
    open_date      = Column(Date)

    __table_args__ = (UniqueConstraint("owner_id", "name", "as_on", name="uq_deposit_snapshot"),)


class TdsDeduction(Base):
    """Tax the bank deducted at source from a deposit's interest (SBI e-mail). Interest credited to savings is net of it."""
    __tablename__ = "tds_deductions"

    id            = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id      = Column(String(20), nullable=False)
    account_last4 = Column(String(4), nullable=False)
    deducted_on   = Column(Date, nullable=False)
    amount        = Column(Numeric(15, 2), nullable=False)
    natural_key   = Column(String(120), nullable=False, unique=True)


class PayeeMemory(Base):
    """
    What a payee (UPI/IMPS counterparty) is, learned once and applied everywhere.
    source 'user' = you labelled it (authoritative); 'llm' = Ollama's suggestion with a
    confidence, applied automatically only above the threshold, otherwise reviewed.
    """
    __tablename__ = "payee_memory"

    payee_key   = Column(String(120), primary_key=True)
    category    = Column(String(50), nullable=False)
    subcategory = Column(String(50))
    source      = Column(String(10), nullable=False)      # user | llm
    confidence  = Column(Numeric(4, 2), default=1)
    sample      = Column(String(200))                      # one narration, for the Review list
    updated_at  = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
