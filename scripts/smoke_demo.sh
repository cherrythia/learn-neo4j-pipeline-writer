#!/usr/bin/env bash
# Smoke-test the running demo stack (docker compose up -d --wait first).
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; . ./.env; set +a

fail() { echo "FAIL: $*" >&2; exit 1; }
sql() { docker compose exec -T mysql mysql -N "$@" 2>/dev/null; }

counts=$(sql -ureader -p"$MYSQL_READER_PASSWORD" demo_itam -e \
  "SELECT COUNT(*) FROM departments; SELECT COUNT(*) FROM assets; SELECT COUNT(*) FROM assets WHERE status = 'retired';" | tr '\n' ' ')
[ "$counts" = "9 29 3 " ] || fail "unexpected demo row counts: $counts"
echo "ok   demo data loaded (departments assets retired = $counts)"

out=$(docker compose exec -T mysql mysql -ureader -p"$MYSQL_READER_PASSWORD" demo_itam \
  -e "DELETE FROM software" 2>&1 || true)
case "$out" in
  *"ERROR 1142"*) ;;  # permission denied: exactly what we want
  *) fail "reader write was not denied by privileges: $out" ;;
esac
echo "ok   reader is read-only"

fks=$(sql -ureader -p"$MYSQL_READER_PASSWORD" -e \
  "SELECT COUNT(*) FROM information_schema.REFERENTIAL_CONSTRAINTS WHERE CONSTRAINT_SCHEMA='demo_itam'")
[ "$fks" = "4" ] || fail "expected 4 foreign keys in demo_itam, got $fks"
echo "ok   foreign keys enforced"

sql -uwriter -p"$MYSQL_WRITER_PASSWORD" governance -e "CREATE TABLE _smoke(x INT); DROP TABLE _smoke;" \
  || fail "writer cannot manage governance DB"
echo "ok   writer owns governance DB"

token=$(curl -sf -X POST localhost:8088/api/v1/security/login -H 'content-type: application/json' \
  -d "{\"username\":\"${SUPERSET_ADMIN_USERNAME}\",\"password\":\"${SUPERSET_ADMIN_PASSWORD}\",\"provider\":\"db\"}" \
  | python3 -c 'import sys, json; print(json.load(sys.stdin)["access_token"])') || fail "mock superset login"
n=$(curl -sf localhost:8088/api/v1/dataset/ -H "Authorization: Bearer $token" \
  | python3 -c 'import sys, json; print(json.load(sys.stdin)["count"])')
[ "$n" = "5" ] || fail "mock superset datasets: $n"
echo "ok   mock superset serves $n datasets"

docker compose exec -T neo4j cypher-shell -u neo4j -p "$WRITER_NEO4J_PASSWORD" "RETURN 1" >/dev/null \
  || fail "neo4j query"
echo "ok   neo4j accepts queries"
echo "demo stack healthy"
