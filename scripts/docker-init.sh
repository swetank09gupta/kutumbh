#!/bin/sh
# Runs once on every `docker compose up` inside the bootstrap container.
# Exits immediately if the DB already has data (idempotent — safe to run repeatedly).
#
# Conditions to trigger restore + reprocess:
#   1. owners table has < 2 rows (fresh DB — only 'family' pseudo-owner or empty)
#   2. A config backup exists in /data/backup/
#   3. Raw statement files exist in /data/raw/

set -e

APP_URL="http://app:8000"
PG_HOST="${POSTGRES_HOST:-postgres}"
PG_USER="${POSTGRES_USER:-financeuser}"
PG_DB="${POSTGRES_DB:-financedb}"

# ── Check if data already exists ─────────────────────────────────────────────
OWNER_COUNT=$(psql -h "$PG_HOST" -U "$PG_USER" -d "$PG_DB" -t -A \
  -c "SELECT COUNT(*) FROM owners WHERE id NOT LIKE '.%';" 2>/dev/null || echo "0")
OWNER_COUNT=$(echo "$OWNER_COUNT" | tr -d '[:space:]')

if [ "$OWNER_COUNT" -ge 2 ] 2>/dev/null; then
  echo "[bootstrap] DB already has data ($OWNER_COUNT owners) — skipping restore."
  exit 0
fi

# ── Check if backup + raw files exist ────────────────────────────────────────
LATEST_BACKUP=$(ls -1t /data/backup/config_*.sql 2>/dev/null | head -1)
if [ -z "$LATEST_BACKUP" ]; then
  echo "[bootstrap] No config backup found in /data/backup/ — nothing to restore."
  exit 0
fi

RAW_COUNT=$(find /data/raw -type f \( -name "*.pdf" -o -name "*.html" -o -name "*.csv" \) 2>/dev/null | wc -l | tr -d '[:space:]')
if [ "$RAW_COUNT" -eq 0 ] 2>/dev/null; then
  echo "[bootstrap] No raw statement files found in /data/raw/ — skipping reprocess."
fi

# ── Restore config tables ─────────────────────────────────────────────────────
echo "[bootstrap] Restoring config from $(basename "$LATEST_BACKUP")…"

psql -h "$PG_HOST" -U "$PG_USER" -d "$PG_DB" <<'SQL'
DELETE FROM goals;
DELETE FROM manual_asset_contributions;
DELETE FROM manual_assets;
DELETE FROM owners;
SQL

psql -h "$PG_HOST" -U "$PG_USER" -d "$PG_DB" -f "$LATEST_BACKUP"
echo "[bootstrap] Config restored."

# ── Trigger reprocess if raw files exist ─────────────────────────────────────
if [ "$RAW_COUNT" -gt 0 ] 2>/dev/null; then
  echo "[bootstrap] Triggering reprocess of $RAW_COUNT raw files…"
  wget -q -O - --post-data="" "$APP_URL/api/sync/reprocess" > /dev/null 2>&1 \
    && echo "[bootstrap] Reprocess started — check app logs for progress." \
    || echo "[bootstrap] WARNING: Could not reach app at $APP_URL — reprocess not triggered."
else
  echo "[bootstrap] No raw files — restore complete, no reprocess needed."
fi
