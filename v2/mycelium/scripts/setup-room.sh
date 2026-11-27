#!/bin/sh
# One-shot `mycelium-setup` service (see docker-compose.yml): makes sure the negotiation room exists
# and is linked to the CFN, on every `docker compose up`. Safe to repeat:
#   POST /api/rooms                  -> 200 created, or 400 "Room already exists" (both fine)
#   POST /api/rooms/<room>/sync-mas  -> links the room to the CFN; keeps an existing mas_id
# Exits non-zero if the room still has no mas_id, so the agents (which wait for this) do not start
# against a broken room. Needs curl + python (it runs on the mycelium-backend image).
set -eu

API="${MYCELIUM_API_URL:-http://mycelium-backend:8000}"
ROOM="${MYCELIUM_ROOM:?MYCELIUM_ROOM is not set}"
log() { echo "[room-setup] $*"; }

field() {  # field <name>: read one top-level field from the room JSON on stdin
  python3 -c 'import json,sys
try: print(json.load(sys.stdin).get(sys.argv[1]) or "")
except Exception: pass' "$1"
}

code=$(curl -s -m 15 -o /tmp/create.json -w '%{http_code}' -X POST "$API/api/rooms" \
  -H 'Content-Type: application/json' -d "{\"name\":\"$ROOM\",\"is_public\":true}") || code=000
case "$code" in
  200|201) log "created room '$ROOM'" ;;
  400|409) log "room '$ROOM' already exists" ;;
  *) log "ERROR: creating room '$ROOM' failed (HTTP $code): $(cat /tmp/create.json 2>/dev/null)" >&2; exit 1 ;;
esac

ws_known() {  # ws_known <id>: is <id> a workspace in the CFN? (skipped when CFN_MGMT_URL is unset)
  [ -z "${CFN_MGMT_URL:-}" ] && return 0
  curl -s -m 10 "$CFN_MGMT_URL/api/workspaces" | python3 -c 'import json,sys
try: ids=[w.get("id") for w in json.load(sys.stdin).get("workspaces") or []]
except Exception: ids=[]
sys.exit(0 if sys.argv[1] in ids else 1)' "$1"
}

# sync-mas (re)links the room to the backend's current workspace. A failed call leaves any OLD
# mas_id in place, so success = HTTP 200 + a mas_id + a workspace the CFN really has.
mas=""; ws=""
for i in 1 2 3 4 5; do
  code=$(curl -s -m 30 -o /tmp/sync.json -w '%{http_code}' -X POST "$API/api/rooms/$ROOM/sync-mas") || code=000
  room=$(curl -s -m 10 "$API/api/rooms/$ROOM")
  mas=$(printf '%s' "$room" | field mas_id); ws=$(printf '%s' "$room" | field workspace_id)
  if [ "$code" = 200 ] && [ -n "$mas" ] && [ -n "$ws" ] && ws_known "$ws"; then
    log "room '$ROOM' ready: mas_id=$mas workspace_id=$ws"
    exit 0
  fi
  log "sync-mas attempt $i: HTTP $code, mas_id='${mas:-none}', workspace_id='${ws:-none}': $(head -c 300 /tmp/sync.json 2>/dev/null)"
  sleep 3
done
log "ERROR: room '$ROOM' is not linked to a live CFN workspace; negotiations would fail. Check: docker compose logs mycelium-backend ioc-cfn-mgmt-plane-svc" >&2
exit 1
