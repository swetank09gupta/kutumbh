#!/usr/bin/env bash
# Full restore on a new machine from the repo folder alone.
#
# What this does:
#   1. Starts the docker stack (postgres + app + grafana)
#   2. Waits for postgres and the app to be healthy
#   3. Restores the latest config backup (owners / manual_assets / goals)
#   4. Triggers reprocess of all PDFs already in data/raw/
#   5. Polls until reprocess finishes and prints the result
#
# Usage:  ./scripts/restore.sh
# Pre-req: Docker installed, .env file present

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BACKUP_DIR="$REPO_ROOT/data/backup"
APP_URL="${FINANCE_APP_URL:-http://localhost:8000}"

CONTAINER="${FINANCE_POSTGRES_CONTAINER:-finance_postgres}"
PG_USER="${POSTGRES_USER:-financeuser}"
PG_DB="${POSTGRES_DB:-financedb}"

# ── 1. Start the stack ────────────────────────────────────────────────────────
echo "==> Starting docker stack…"
cd "$REPO_ROOT"
docker compose up -d

# ── 2. Wait for postgres ──────────────────────────────────────────────────────
echo "==> Waiting for postgres to be ready…"
for i in $(seq 1 30); do
  if docker exec "$CONTAINER" pg_isready -U "$PG_USER" -d "$PG_DB" -q 2>/dev/null; then
    echo "    Postgres ready (${i}s)"
    break
  fi
  if [ "$i" -eq 30 ]; then
    echo "ERROR: Postgres did not become ready in 30s. Check: docker logs $CONTAINER"
    exit 1
  fi
  sleep 1
done

# ── 3. Restore config backup ──────────────────────────────────────────────────
LATEST=$(ls -1t "$BACKUP_DIR"/config_*.sql 2>/dev/null | head -1)
if [ -z "$LATEST" ]; then
  echo "ERROR: no backup found in $BACKUP_DIR. Run ./scripts/backup.sh on the old machine first."
  exit 1
fi

echo "==> Restoring config from $(basename "$LATEST")…"

# Clear existing config data in FK-safe order before re-inserting
docker exec -i "$CONTAINER" psql -U "$PG_USER" -d "$PG_DB" <<'SQL'
DELETE FROM goals;
DELETE FROM manual_asset_contributions;
DELETE FROM manual_assets;
DELETE FROM owners;
SQL

docker exec -i "$CONTAINER" psql -U "$PG_USER" -d "$PG_DB" < "$LATEST"
echo "    Config restored."

# ── 4. Wait for the app ───────────────────────────────────────────────────────
echo "==> Waiting for app to be ready…"
for i in $(seq 1 60); do
  if curl -sf "$APP_URL/api/sync/status" -o /dev/null 2>/dev/null; then
    echo "    App ready (${i}s)"
    break
  fi
  if [ "$i" -eq 60 ]; then
    echo "ERROR: App did not become ready in 60s. Check: docker logs finance_app"
    exit 1
  fi
  sleep 1
done

# ── 5. Trigger reprocess ──────────────────────────────────────────────────────
RAW_COUNT=$(find "$REPO_ROOT/data/raw" -type f \( -name "*.pdf" -o -name "*.html" -o -name "*.csv" \) 2>/dev/null | wc -l | tr -d ' ')
echo "==> Triggering reprocess of $RAW_COUNT local files…"
curl -sf -X POST "$APP_URL/api/sync/reprocess" -o /dev/null

# ── 6. Poll until done ────────────────────────────────────────────────────────
echo "    (polling every 10s — this may take a few minutes)"
DOTS=0
while true; do
  sleep 10
  STATUS=$(curl -sf "$APP_URL/api/sync/status" 2>/dev/null || echo '{"running":true}')
  RUNNING=$(echo "$STATUS" | python3 -c "import sys,json; print(json.load(sys.stdin)['running'])")
  if [ "$RUNNING" = "False" ]; then
    break
  fi
  printf "."
  DOTS=$((DOTS + 1))
  if [ "$((DOTS % 6))" -eq 0 ]; then echo ""; fi
done

echo ""
echo "==> Reprocess complete!"
curl -sf "$APP_URL/api/sync/status" | python3 -c "
import sys, json
d = json.load(sys.stdin)
r = d.get('last_result', {}) or {}
e = d.get('last_error')
if e:
    print(f'  ERROR: {e}')
else:
    print(f'  Attachments processed : {r.get(\"attachments_fetched\", 0)}')
    print(f'  Transactions inserted  : {r.get(\"transactions_inserted\", 0)}')
    print(f'  Holdings updated       : {r.get(\"holdings_inserted\", 0)}')
    print(f'  NW snapshots updated   : {r.get(\"snapshots_updated\", 0)}')
    print(f'  Parse errors           : {r.get(\"parse_errors\", 0)}')
"

echo ""
echo "All done. Open http://localhost:3000 for Grafana or http://localhost:8000 for the app."
