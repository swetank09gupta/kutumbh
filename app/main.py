"""
Finance OS — FastAPI entry point.
On startup:
  1. Creates all DB tables
  2. Seeds owners + goals from config/accounts.yml (if present)
  3. Starts the background scheduler
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path

import yaml
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)


def _migrate_db() -> None:
    """Add new columns to existing tables — safe to run repeatedly (IF NOT EXISTS)."""
    from app.models.database import engine
    from sqlalchemy import text
    migrations = [
        "ALTER TABLE owners ADD COLUMN IF NOT EXISTS birth_date DATE",
        "ALTER TABLE owners ADD COLUMN IF NOT EXISTS risk_appetite VARCHAR(20)",
        "ALTER TABLE owners ADD COLUMN IF NOT EXISTS retirement_age INTEGER",
        "ALTER TABLE manual_assets ADD COLUMN IF NOT EXISTS quantity NUMERIC(15,4)",
        "ALTER TABLE manual_assets ADD COLUMN IF NOT EXISTS metal_purity VARCHAR(4)",
        "ALTER TABLE manual_assets ADD COLUMN IF NOT EXISTS source_statement_date DATE",
        "ALTER TABLE manual_assets ADD COLUMN IF NOT EXISTS is_liquid BOOLEAN DEFAULT FALSE",
        "ALTER TABLE holdings ADD COLUMN IF NOT EXISTS goal_tag VARCHAR(30)",
        # XIRR / returns tracking additions
        "ALTER TABLE transactions ADD COLUMN IF NOT EXISTS isin VARCHAR(12)",
        "ALTER TABLE holdings ADD COLUMN IF NOT EXISTS first_buy_date DATE",
        # Per-goal projection knobs (blended return, SIP step-up, glide path)
        "ALTER TABLE goals ADD COLUMN IF NOT EXISTS use_manual_return BOOLEAN DEFAULT FALSE",
        "ALTER TABLE goals ADD COLUMN IF NOT EXISTS sip_stepup_pct NUMERIC(5,2)",
        "ALTER TABLE goals ADD COLUMN IF NOT EXISTS glide_enabled BOOLEAN DEFAULT TRUE",
        "ALTER TABLE goals ADD COLUMN IF NOT EXISTS glide_start_years_before INTEGER DEFAULT 3",
        "ALTER TABLE goals ADD COLUMN IF NOT EXISTS glide_end_return_pct NUMERIC(5,2) DEFAULT 8.0",
        # Owner tax profile fields (drive 80C dead-weight recommendation)
        "ALTER TABLE owners ADD COLUMN IF NOT EXISTS tax_regime VARCHAR(10) DEFAULT 'old'",
        "ALTER TABLE owners ADD COLUMN IF NOT EXISTS epf_monthly NUMERIC(10,2) DEFAULT 0",
        "ALTER TABLE owners ADD COLUMN IF NOT EXISTS home_loan_share_pct NUMERIC(5,2) DEFAULT 0",
        # Loan tracking: monthly EMI on liability rows (drives accurate 80C principal split)
        "ALTER TABLE manual_assets ADD COLUMN IF NOT EXISTS monthly_payment NUMERIC(15,2)",
        # Recommendation memory — tracks what was advised over time so the UI
        # can show "new this month / persisting / resolved" deltas.
        """CREATE TABLE IF NOT EXISTS recommendation_snapshots (
            id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            snapshot_date DATE NOT NULL,
            owner_id      VARCHAR(20) NOT NULL DEFAULT 'family',
            fingerprint   VARCHAR(120) NOT NULL,
            priority      VARCHAR(10) NOT NULL,
            category      VARCHAR(40) NOT NULL,
            title         VARCHAR(120) NOT NULL,
            detail        TEXT,
            action        TEXT,
            metrics       TEXT,
            created_at    TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        )""",
        "CREATE INDEX IF NOT EXISTS ix_recsnap_owner_date ON recommendation_snapshots(owner_id, snapshot_date DESC)",
        "CREATE INDEX IF NOT EXISTS ix_recsnap_fp_date    ON recommendation_snapshots(fingerprint, snapshot_date DESC)",
        # Cross-account ISIN dedup view — keeps the highest-priority source when the
        # same ISIN appears in both a depository (CDSL) and a broker mirror (Zerodha),
        # or in both CAMS and Zerodha Coin.  This view is the single dedup point for
        # all API endpoints, goals engine, and recommendations.  Created here because
        # SQLAlchemy metadata.create_all() does not know about views.
        # DROP first: CREATE OR REPLACE VIEW cannot change the column list.
        "DROP VIEW IF EXISTS latest_holdings_deduped CASCADE",
        """CREATE VIEW latest_holdings_deduped AS
WITH latest_per_account AS (
    SELECT h.id, h.account_id, h.snapshot_date, h.symbol, h.isin,
           h.instrument_name, h.units, h.avg_cost_nav, h.current_nav,
           h.current_value, h.invested_value, h.gain_loss, h.gain_loss_pct,
           h.holding_type, h.goal_tag, h.first_buy_date, h.source_file,
           a.owner_id, a.account_type, a.institution
    FROM holdings h
    JOIN accounts a ON h.account_id = a.id
    WHERE a.is_active = TRUE
      AND h.snapshot_date = (
          SELECT MAX(h2.snapshot_date) FROM holdings h2 WHERE h2.account_id = h.account_id
      )
),
isin_deduped AS (
    SELECT *,
           ROW_NUMBER() OVER (
               PARTITION BY owner_id, isin
               ORDER BY
                   CASE LOWER(institution)
                       WHEN 'cdsl'      THEN 1
                       WHEN 'cams'      THEN 1
                       WHEN 'kfintech'  THEN 1
                       WHEN 'zerodha'   THEN 2
                       ELSE 3
                   END,
                   snapshot_date DESC
           ) AS rn
    FROM latest_per_account
    WHERE isin IS NOT NULL
)
SELECT id, account_id, snapshot_date, symbol, isin, instrument_name,
       units, avg_cost_nav, current_nav, current_value, invested_value,
       gain_loss, gain_loss_pct, holding_type, goal_tag, first_buy_date,
       source_file, owner_id, account_type, institution
FROM isin_deduped WHERE rn = 1
UNION ALL
SELECT id, account_id, snapshot_date, symbol, isin, instrument_name,
       units, avg_cost_nav, current_nav, current_value, invested_value,
       gain_loss, gain_loss_pct, holding_type, goal_tag, first_buy_date,
       source_file, owner_id, account_type, institution
FROM latest_per_account
WHERE isin IS NULL""",
    ]
    with engine.connect() as conn:
        for stmt in migrations:
            conn.execute(text(stmt))
        conn.commit()


def _seed_from_config(db) -> None:
    """Seed owners and goals from config/accounts.yml on first start."""
    from app.models.schemas import Goal, Owner
    from app.engine.goals import update_goal_sip

    cfg_path = Path("config/accounts.yml")
    if not cfg_path.exists():
        return

    data = yaml.safe_load(cfg_path.read_text()) or {}

    # Owners — create on first start; backfill tax fields from yaml if blank in DB.
    for owner_cfg in data.get("owners", []):
        oid = owner_cfg.get("id")
        if not oid:
            continue
        existing = db.get(Owner, oid)
        if not existing:
            db.add(Owner(
                id=oid, name=owner_cfg.get("name", oid.capitalize()),
                tax_regime=owner_cfg.get("tax_regime", "old"),
                epf_monthly=owner_cfg.get("epf_monthly", 0),
                home_loan_share_pct=owner_cfg.get("home_loan_share_pct", 0),
            ))
        else:
            # Only fill fields that are NULL — never overwrite UI-edited values.
            if existing.tax_regime is None and owner_cfg.get("tax_regime"):
                existing.tax_regime = owner_cfg["tax_regime"]
            if (existing.epf_monthly is None or float(existing.epf_monthly) == 0) \
                    and owner_cfg.get("epf_monthly"):
                existing.epf_monthly = owner_cfg["epf_monthly"]
            if (existing.home_loan_share_pct is None or float(existing.home_loan_share_pct) == 0) \
                    and owner_cfg.get("home_loan_share_pct"):
                existing.home_loan_share_pct = owner_cfg["home_loan_share_pct"]
    # Always ensure family pseudo-owner
    if not db.get(Owner, "family"):
        db.add(Owner(id="family", name="Family"))
    db.flush()

    # Goals
    for g in data.get("goals", []):
        name = g.get("name", "")
        oid  = g.get("owner_id", "family")
        existing = db.query(Goal).filter_by(owner_id=oid, name=name).first()
        if existing:
            continue
        from datetime import date as dt
        tdate = g.get("target_date")
        if isinstance(tdate, str):
            tdate = dt.fromisoformat(tdate)
        goal = Goal(
            owner_id=oid,
            name=name,
            goal_type=g.get("goal_type", "custom"),
            target_amount=Decimal(str(g.get("target_amount", 0))),
            target_date=tdate,
            current_corpus=Decimal("0"),
            expected_return_rate=Decimal(str(g.get("expected_return_rate", 12))),
            inflation_rate=Decimal(str(g.get("inflation_rate", 6))),
            notes=g.get("notes"),
        )
        db.add(goal)
    db.flush()
    db.commit()

    # Recalculate SIP for all goals
    goals = db.query(Goal).all()
    for goal in goals:
        update_goal_sip(db, str(goal.id))

    logger.info("Seeded %d owners and %d goals from config.", len(data.get("owners", [])),
                len(data.get("goals", [])))


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── Startup ───────────────────────────────────────────────────────────────
    from app.models.database import init_db, SessionLocal
    from app.scheduler import start_scheduler

    logger.info("Finance OS starting up …")
    init_db()
    _migrate_db()
    logger.info("Database tables ready.")

    db = SessionLocal()
    try:
        _seed_from_config(db)
    finally:
        db.close()

    start_scheduler()
    yield

    # ── Shutdown ──────────────────────────────────────────────────────────────
    from app.scheduler import stop_scheduler
    stop_scheduler()
    logger.info("Finance OS shut down.")


# ─────────────────────────────────────────────────────────────────────────────
app = FastAPI(
    title="Finance OS",
    description="Family personal-finance aggregator — email-based ingestion, Grafana dashboards.",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # Grafana runs locally alongside
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Routers ────────────────────────────────────────────────────────────────────
from app.api.routes.sync         import router as sync_router
from app.api.routes.accounts     import router as accounts_router
from app.api.routes.transactions import router as transactions_router
from app.api.routes.goals        import router as goals_router
from app.api.routes.manual       import router as manual_router
from app.api.routes.profile      import router as profile_router
from app.api.routes.portfolio    import router as portfolio_router

app.include_router(sync_router)
app.include_router(accounts_router)
app.include_router(transactions_router)
app.include_router(goals_router)
app.include_router(manual_router)
app.include_router(profile_router)
app.include_router(portfolio_router)

# ── SPA static files (must be mounted AFTER API routes) ──────────────────────
_static_dir = Path(__file__).parent / "static"
_static_dir.mkdir(exist_ok=True)
app.mount("/ui", StaticFiles(directory=str(_static_dir), html=True), name="ui")


@app.get("/", include_in_schema=False)
def root():
    """Redirect browser to the SPA dashboard."""
    return RedirectResponse(url="/ui/")


@app.get("/health")
def health():
    return {"status": "ok", "service": "Finance OS"}


@app.get("/api/status")
def status():
    """Quick system status — DB connectivity + scheduler."""
    from app.models.database import SessionLocal
    from app.scheduler import _scheduler

    db = SessionLocal()
    try:
        from sqlalchemy import text
        db.execute(text("SELECT 1"))
        db_ok = True
    except Exception:
        db_ok = False
    finally:
        db.close()

    return {
        "db": "ok" if db_ok else "error",
        "scheduler": "running" if (_scheduler and _scheduler.running) else "stopped",
    }
