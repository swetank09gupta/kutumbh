#!/usr/bin/env bash
# Backup manually-configured tables that can't be reconstructed from raw files.
# Everything else (transactions, balances, holdings, snapshots) is rebuilt by restore.sh.
#
# Usage:  ./scripts/backup.sh
# Output: data/backup/config_YYYYMMDD_HHMMSS.sql  (keeps last 5)

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BACKUP_DIR="$REPO_ROOT/data/backup"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
OUT="$BACKUP_DIR/config_${TIMESTAMP}.sql"

CONTAINER="${FINANCE_POSTGRES_CONTAINER:-finance_postgres}"
PG_USER="${POSTGRES_USER:-financeuser}"
PG_DB="${POSTGRES_DB:-financedb}"

# ── Preflight ─────────────────────────────────────────────────────────────────
if ! docker inspect "$CONTAINER" &>/dev/null; then
  echo "ERROR: container '$CONTAINER' not found. Is the stack running?"
  exit 1
fi

mkdir -p "$BACKUP_DIR"

# ── Dump ──────────────────────────────────────────────────────────────────────
echo "Backing up config tables → $OUT"

docker exec "$CONTAINER" pg_dump \
  -U "$PG_USER" "$PG_DB" \
  --data-only \
  --column-inserts \
  -t owners \
  -t manual_assets \
  -t goals \
  > "$OUT"

SIZE=$(du -sh "$OUT" | cut -f1)
echo "Done — $SIZE written."

# ── Rotate: keep last 5 backups ───────────────────────────────────────────────
KEPT=$(ls -1t "$BACKUP_DIR"/config_*.sql 2>/dev/null | tail -n +6)
if [ -n "$KEPT" ]; then
  echo "$KEPT" | xargs rm -f
  echo "Rotated old backups."
fi

echo ""
echo "To restore on a new machine:"
echo "  ./scripts/restore.sh"
