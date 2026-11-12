#!/usr/bin/env bash
# End-to-end walkthrough of the writer service against the offline demo server
# (in-memory governance store and graph, scripted LLM: no Docker, no API key).
#
#   ./scripts/demo.sh                 # runs every step, then stops the server
#   WRITER_DEMO_PORT=8123 ./scripts/demo.sh
#
# Steps (each one is checked; the script exits non-zero on the first surprise):
#   1. upload the sample annual report PDF      POST /ingest/documents
#   2. the reviewer reads the review queue      GET  /api/review
#   3. the reviewer approves one fact           POST /api/review/{id}/approve
#   4. the admin drafts + approves a mapping    POST /api/mappings/suggest/1, /approve
#   5. structured sync of Superset dataset 1    POST /ingest/superset/1
#   6. the admin rolls back the document run    POST /api/runs/{id}/rollback
#   7. the audit log records all of it          GET  /api/audit
set -euo pipefail
cd "$(dirname "$0")/.."

PORT="${WRITER_DEMO_PORT:-8765}"
BASE="http://127.0.0.1:${PORT}"
WORK="$(mktemp -d)"
export WRITER_DEMO_PORT="$PORT"
export WRITER_DEMO_ADMIN_PASSWORD="demo-admin-$RANDOM-$RANDOM"
export WRITER_DEMO_REVIEWER_PASSWORD="demo-reviewer-$RANDOM-$RANDOM"
export WRITER_DEMO_INGEST_API_KEY="demo-ingest-$RANDOM-$RANDOM-$RANDOM"
CSRF="X-Requested-With: writer-admin"
PDF="samples/aurora_pacific_annual_report_2025.pdf"

fail() { echo "FAIL: $*" >&2; [ -f "$WORK/server.log" ] && tail -20 "$WORK/server.log" >&2; exit 1; }
json() { python3 -c "import json, sys; d = json.load(sys.stdin); print($1)"; }
step() { printf '\n== %s\n' "$*"; }

server_pid=""
cleanup() {  # wait for the server: it deletes its temporary store on the way out
  if [ -n "$server_pid" ]; then kill "$server_pid" 2>/dev/null || true; wait "$server_pid" 2>/dev/null || true; fi
  rm -rf "$WORK"
}
trap cleanup EXIT

uv run writer-demo-server >"$WORK/server.log" 2>&1 &
server_pid=$!
for _ in $(seq 1 60); do
  curl -sf "$BASE/health" >/dev/null 2>&1 && break
  kill -0 "$server_pid" 2>/dev/null || fail "demo server exited"
  sleep 0.5
done
curl -sf "$BASE/health" >/dev/null || fail "demo server did not start on $BASE"
echo "demo server up at $BASE (admin UI at $BASE/admin/ if built)"

login() {  # login <user> <password> <cookie jar>
  curl -sf -c "$3" -X POST "$BASE/api/auth/login" -H "$CSRF" -H 'content-type: application/json' \
    -d "{\"username\": \"$1\", \"password\": \"$2\"}" >/dev/null || fail "login as $1"
}
as() {  # as <cookie jar> <curl args...>
  local jar="$1"; shift
  curl -sf -b "$jar" -H "$CSRF" "$@"
}
login rita "$WRITER_DEMO_REVIEWER_PASSWORD" "$WORK/rita"
login alice "$WRITER_DEMO_ADMIN_PASSWORD" "$WORK/alice"

step "1. upload $PDF (3 self-consistency runs of the scripted extractor, then gates and scoring)"
run=$(curl -sf -X POST "$BASE/ingest/documents" -H "X-API-Key: $WRITER_DEMO_INGEST_API_KEY" \
  -F "file=@$PDF;type=application/pdf") || fail "upload"
doc_run=$(echo "$run" | json 'd["run_id"]')
[ "$(echo "$run" | json 'd["status"]')" = "completed" ] || fail "run not completed: $run"
echo "run $doc_run: $(echo "$run" | json 'd["counts"]')"

step "2. review queue (the back-translation judge is unsure, so a human decides)"
queue=$(as "$WORK/rita" "$BASE/api/review") || fail "review queue"
n=$(echo "$queue" | json "len([i for i in d if i['run_id'] == '$doc_run'])")
[ "$n" -ge 10 ] || fail "expected the document's facts in review, got $n"
echo "$n open items from run $doc_run, e.g.:"
echo "$queue" | json "'\n'.join([f\"  #{i['id']} {i['reason']}: {i['evidence'][0]['quote']!r} ({i['evidence'][0]['locator']})\" for i in d if i['run_id'] == '$doc_run'][:3])"

step "3. approve one fact (written to the graph under its own approval run)"
item=$(echo "$queue" | json "next(i['id'] for i in d if i['run_id'] == '$doc_run')")
as "$WORK/rita" -X POST "$BASE/api/review/$item/approve" -H 'content-type: application/json' \
  -d '{}' >/dev/null || fail "approve #$item"
echo "approved #$item"

step "4. superset mapping for dataset 1 (LLM drafts it, the admin approves it)"
draft=$(as "$WORK/alice" -X POST "$BASE/api/mappings/suggest/1") || fail "suggest mapping"
mapping=$(echo "$draft" | json 'd["id"]')
echo "draft mapping #$mapping: $(echo "$draft" | json 'd["mapping"]["entities"]')"
as "$WORK/alice" -X POST "$BASE/api/mappings/$mapping/approve" >/dev/null || fail "approve mapping"
echo "mapping #$mapping approved"

step "5. superset sync (deterministic rows to facts through the approved mapping)"
sync=$(curl -sf -X POST "$BASE/ingest/superset/1" -H "X-API-Key: $WRITER_DEMO_INGEST_API_KEY") \
  || fail "superset sync"
[ "$(echo "$sync" | json 'd["status"]')" = "completed" ] || fail "sync not completed: $sync"
echo "run $(echo "$sync" | json 'd["run_id"]'): $(echo "$sync" | json 'd["counts"]')"

step "6. roll back the document run (its open review items close; the approval stays)"
undo=$(as "$WORK/alice" -X POST "$BASE/api/runs/$doc_run/rollback") || fail "rollback"
echo "rolled back $doc_run: $undo"
left=$(as "$WORK/rita" "$BASE/api/review" | json "len([i for i in d if i['run_id'] == '$doc_run'])")
[ "$left" = "0" ] || fail "run $doc_run still has $left open review items"
echo "no open review items left for $doc_run"

step "7. audit log"
audit=$(as "$WORK/alice" "$BASE/api/audit") || fail "audit"
for action in run.complete review.approve run.rollback; do
  echo "$audit" | json "any(a['action'] == '$action' for a in d)" | grep -qx True \
    || fail "no $action in the audit log"
done
echo "$audit" | json "'\n'.join(f\"  {a['actor']:>14}  {a['action']:<18} {a['entity_type']} {a['entity_id']}\" for a in d[:8])"

printf '\ndemo complete\n'
