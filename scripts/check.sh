#!/bin/sh
# One command before every push:   scripts/check.sh          (tests, scratch database)
#                                  scripts/check.sh --live   (also: invariants + figure diff on the live data)
# Needs the compose stack running (the Postgres container) and the kutumbh-app image.
set -e
cd "$(dirname "$0")/.."
set -a; [ -f .env ] && . ./.env; set +a
NET="${COMPOSE_NETWORK:-kutumbh_default}"
PG="${POSTGRES_CONTAINER:-finance_postgres}"
SCRATCH=kutumbh_test
URL="postgresql://$POSTGRES_USER:$POSTGRES_PASSWORD@postgres:5432/$SCRATCH"

run() { docker run --rm -i --network "$NET" -v "$PWD":/app -w /app -e TZ=Asia/Kolkata "$@"; }

echo "== tests (unit, scratch-database integration, UI/API contract)"
docker exec "$PG" psql -q -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "DROP DATABASE IF EXISTS $SCRATCH" -c "CREATE DATABASE $SCRATCH"
trap 'docker exec "$PG" psql -q -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "DROP DATABASE IF EXISTS '$SCRATCH'" >/dev/null' EXIT
run -e TEST_DATABASE_URL="$URL" kutumbh-app python -m unittest discover -s tests -t . 2>&1 | grep -v " INFO \|DeprecationWarning\|age_hr" | tail -25
# discover exits 0/1 through the pipe; re-run quietly for the exit status
run -e TEST_DATABASE_URL="$URL" kutumbh-app python -m unittest discover -s tests -t . >/dev/null 2>&1 || { echo "TESTS FAILED"; exit 1; }

if [ "$1" = "--accept" ]; then
  run -e DATABASE_URL="postgresql://$POSTGRES_USER:$POSTGRES_PASSWORD@postgres:5432/$POSTGRES_DB" --env-file .env --entrypoint python kutumbh-app scripts/figures.py --accept
  exit 0
fi

if [ "$1" = "--live" ]; then
  echo "== live data: invariants and figure diff"
  LIVE="postgresql://$POSTGRES_USER:$POSTGRES_PASSWORD@postgres:5432/$POSTGRES_DB"
  run -e DATABASE_URL="$LIVE" --env-file .env --entrypoint python kutumbh-app - <<'PY'
from app.models.database import SessionLocal
from app.invariants import INVARIANTS
db = SessionLocal(); bad = 0
for name, fn in INVARIANTS:
    v = fn(db); bad += bool(v)
    print(("ok    " if not v else "BROKEN"), name)
    for x in v[:5]: print("        ", x)
raise SystemExit(1 if bad else 0)
PY
  rc=0
  run -e DATABASE_URL="$LIVE" --env-file .env --entrypoint python kutumbh-app scripts/figures.py || rc=$?
  [ "$rc" -gt 1 ] && { echo "figures.py failed"; exit 1; }
  [ "$rc" -eq 1 ] && echo "(figures moved above: if intended, accept them with: scripts/check.sh --accept)"
fi
echo "== all checks passed"
