#!/bin/sh
# Updating in place (SPEC.md section 14.3): an installation stopped and started again with the two commands of an
# update keeps its data, and a version started on a database a later one changed refuses to start.
# Run by tests/e2e/run.sh, on the installation tests/e2e/install.sh made.
set -eu
image=${HG_E2E_IMAGE:?} url=${HG_URL:?} key=${HG_KEY:?}
cd "${HG_E2E_DIR:?}"
fail() { echo "FAIL  $*" >&2; exit 1; }
pass() { echo "PASS  $*"; }
call() { curl -fsS -H "Authorization: Bearer $key" -H "Content-Type: application/json" "$@"; }
field() { python3 -c "import json, sys; print(json.load(sys.stdin)$1)"; }

first=$(call -X POST "$url/v1/responses" -d '{"model": "default/shell/none/none", "input": "echo kept > kept.txt; echo made"}')
[ "$(printf '%s' "$first" | field '["status"]')" = completed ] || fail "the turn before the update did not complete: $first"
id=$(printf '%s' "$first" | field '["id"]')
session=$(printf '%s' "$first" | field '["metadata"]["hg_session"]')

docker compose down >/dev/null 2>&1                                    # the version that was running is stopped,
docker run --rm "$image" harnessgateway compose > compose.yaml          # and the two commands of an update are run
docker compose up --detach --wait >/dev/null 2>&1 || fail "the updated installation did not become ready"
[ "$(call "$url/v1/sessions/$session" | field '["turns"]')" = 1 ] || fail "the session is gone after the update"
again=$(call -X POST "$url/v1/responses" -d "{\"model\": \"default/shell/none/none\", \"input\": \"cat kept.txt\", \"previous_response_id\": \"$id\"}")
printf '%s' "$again" | grep -q 'kept' || fail "the session's files did not survive the update: $again"
pass "an installation updated in place keeps its key, its sessions, and their files"

# A later version of the program ran on this database: it has a schema file this version does not.
docker compose exec -T db psql -q -U hg -d hg -c "INSERT INTO migrations (owner, name) VALUES ('core', '9999_later.sql')"
docker compose stop serve >/dev/null 2>&1
if docker compose run --rm -T serve harnessgateway serve </dev/null > refused.log 2>&1; then fail "it started on a database a later version changed"; fi
grep -q "changed by a later version than this one (it has core/9999_later.sql)" refused.log || fail "it did not say why it would not start: $(cat refused.log)"
pass "a version started on a database a later one changed refuses to start, and says so"
docker compose exec -T db psql -q -U hg -d hg -c "DELETE FROM migrations WHERE name = '9999_later.sql'"
docker compose up --detach --wait >/dev/null 2>&1 || fail "the installation did not come back"
