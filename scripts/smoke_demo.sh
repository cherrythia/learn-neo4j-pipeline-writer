#!/usr/bin/env bash
# Smoke-test the running demo stack (docker compose up -d --wait first).
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; . ./.env; set +a

fail() { echo "FAIL: $*" >&2; exit 1; }
sql() { docker compose exec -T mysql mysql -N "$@" 2>/dev/null; }

counts=$(sql -ureader -p"$MYSQL_READER_PASSWORD" demo_compliance -e \
  "SELECT COUNT(*) FROM issuers; SELECT COUNT(*) FROM filings; SELECT COUNT(*) FROM filings WHERE filed_on > due_on;" | tr '\n' ' ')
[ "$counts" = "20 40 3 " ] || fail "unexpected demo row counts: $counts"
echo "ok   demo data loaded (issuers filings late = $counts)"

if sql -ureader -p"$MYSQL_READER_PASSWORD" demo_compliance -e "DELETE FROM rules"; then
  fail "reader user can write"
fi
echo "ok   reader is read-only"

sql -uwriter -p"$MYSQL_WRITER_PASSWORD" governance -e "CREATE TABLE _smoke(x INT); DROP TABLE _smoke;" \
  || fail "writer cannot manage governance DB"
echo "ok   writer owns governance DB"

token=$(curl -sf -X POST localhost:8088/api/v1/security/login -H 'content-type: application/json' \
  -d "{\"username\":\"${SUPERSET_ADMIN_USERNAME}\",\"password\":\"${SUPERSET_ADMIN_PASSWORD}\",\"provider\":\"db\"}" \
  | python3 -c 'import sys, json; print(json.load(sys.stdin)["access_token"])') || fail "mock superset login"
n=$(curl -sf localhost:8088/api/v1/dataset/ -H "Authorization: Bearer $token" \
  | python3 -c 'import sys, json; print(json.load(sys.stdin)["count"])')
[ "$n" = "4" ] || fail "mock superset datasets: $n"
echo "ok   mock superset serves $n datasets"

docker compose exec -T neo4j cypher-shell -u neo4j -p "$WRITER_NEO4J_PASSWORD" "RETURN 1" >/dev/null \
  || fail "neo4j query"
echo "ok   neo4j accepts queries"
echo "demo stack healthy"
