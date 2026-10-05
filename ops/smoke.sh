#!/bin/bash
# Pre-gate smoke test for the two-process shape. Loopback only.
# Does NOT touch nginx, 443, or the public name; tears itself down.
#
#   ./ops/smoke.sh <path-to-staged-release>
#
# Expects the release tree to have been transferred to the host already. The
# tree is NOT copied from the repo here - the repo lives on the dev machine,
# not on the prod host, and assuming otherwise is how a smoke test silently
# tests nothing.
#
# It does NOT leave the machine clean: it opens exactly one throwaway account
# (carrying the C2.3 opening grant) in the database it is pointed at, and the
# process is killed rather than the row. "Tears itself down" covers ports and
# logs, not ledger rows.
set -uo pipefail

DEST="${1:-/srv/pocketful/releases/smoke}"
DB=/var/lib/pocketful/pocketful.db
STORE=/var/lib/pocketful/ui-pending.json
LOGDIR=/var/tmp/pocketful-smoke
API_PORT=8001   # NOT 8000 - the getn.space app owns 8000
UI_PORT=8080

echo "=== paths must be on instance-local storage ==="
findmnt -no FSTYPE,SOURCE,TARGET --target /var/lib/pocketful
echo "db    -> $DB"
echo "store -> $STORE"

mkdir -p "$LOGDIR"
cd "$DEST" || { echo "no staged tree at $DEST"; exit 1; }

python3 -m api --db "$DB" --host 127.0.0.1 --port "$API_PORT" > "$LOGDIR/api.log" 2>&1 &
API_PID=$!
sleep 2.5
kill -0 "$API_PID" 2>/dev/null || { echo "api DIED:"; cat "$LOGDIR/api.log"; exit 1; }
echo "api: alive (pid $API_PID)"

# C2.1: the account id is REQUIRED and CLIENT-SUPPLIED, and it is the ledger's
# exactly-once guard - so a FIXED id makes this script pass exactly once and
# 409 `account_exists` on every run after, with an empty ACCT and a dead exit
# at the line below. Unique per run. The round-trip is asserted as well,
# because "the server quietly minted its own id" is the regression this test
# exists to catch and the old body could not have seen it.
SMOKE_ACCT="acct-smoke-$(date +%s)-$$"
echo -n "POST /accounts -> "
RESP=$(curl -sS --max-time 5 -X POST "http://127.0.0.1:$API_PORT/accounts" \
  -H 'Content-Type: application/json' \
  -d "{\"account_id\":\"$SMOKE_ACCT\",\"owner_id\":\"smoke\",\"currency\":\"USD\",\"allow_overdraft\":false}")
echo "$RESP"
ACCT=$(python3 -c "import json,sys;print(json.loads(sys.argv[1]).get('account_id',''))" "$RESP" 2>/dev/null)
[ -z "$ACCT" ] && { kill "$API_PID"; echo "no account_id"; exit 1; }
[ "$ACCT" != "$SMOKE_ACCT" ] && { kill "$API_PID"; echo "SMOKE FAIL: asked for $SMOKE_ACCT, got $ACCT"; exit 1; }

python3 -m app.web --api "http://127.0.0.1:$API_PORT" --store "$STORE" \
    --account "$ACCT" --host 127.0.0.1 --port "$UI_PORT" > "$LOGDIR/web.log" 2>&1 &
WEB_PID=$!
sleep 2.5
kill -0 "$WEB_PID" 2>/dev/null || { echo "web DIED:"; cat "$LOGDIR/web.log"; kill "$API_PID"; exit 1; }
echo "web: alive (pid $WEB_PID)"

echo -n "GET /health -> "; curl -sS --max-time 5 "http://127.0.0.1:$UI_PORT/health"; echo
echo -n "GET /       -> HTTP "; curl -sS -o /dev/null -w "%{http_code}\n" --max-time 5 "http://127.0.0.1:$UI_PORT/"

# The load-bearing rule: no key material may reach the browser.
KEYS=$(curl -sS --max-time 5 "http://127.0.0.1:$UI_PORT/" | grep -c "Idempotency-Key")
echo "occurrences of 'Idempotency-Key' in the page: $KEYS (must be 0)"
[ "$KEYS" != "0" ] && echo "SMOKE FAIL: key material reached the browser"

kill "$WEB_PID" "$API_PID" 2>/dev/null
sleep 1
rm -rf "$LOGDIR"
echo "stopped; ports $API_PORT/$UI_PORT released"
