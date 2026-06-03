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

    # Owners
    for owner_cfg in data.get("owners", []):
        oid = owner_cfg.get("id")
        if oid and not db.get(Owner, oid):
            db.add(Owner(id=oid, name=owner_cfg.get("name", oid.capitalize())))
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
