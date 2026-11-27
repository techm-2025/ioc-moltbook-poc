#!/bin/sh
# Startup wrapper for the mycelium-backend container (mounted read-only, see docker-compose.yml).
#
# The backend reads WORKSPACE_ID once at startup; without a valid one, rooms get no CFN mas_id and
# every negotiation fails with "CFN not configured". The CFN creates a NEW random "Default Workspace"
# id whenever its database is recreated (fresh clone, `docker compose down -v`), so a hard-coded
# value in .env goes stale. This wrapper resolves it before handing over to the image's own start.sh:
#
#   WORKSPACE_ID set in .env and present in the CFN  -> used as-is
#   empty, or not (any longer) present in the CFN    -> the CFN workspace named $CFN_WORKSPACE_NAME
#                                                       ("Default Workspace"), or the only workspace
#
# Runs as the image's normal user; only needs curl + python (both in the image).
set -eu

WS_NAME="${CFN_WORKSPACE_NAME:-Default Workspace}"
log() { echo "[workspace] $*"; }

pick_workspace() {  # stdin: GET /api/workspaces JSON; prints the id to use, or nothing
  WANT="${WORKSPACE_ID:-}" WS_NAME="$WS_NAME" python3 -c '
import json, os, sys
try:
    ws = json.load(sys.stdin).get("workspaces") or []
except Exception:
    sys.exit(0)
want, name = os.environ["WANT"], os.environ["WS_NAME"]
ids = [w.get("id") for w in ws]
if want and want in ids:
    print(want)
else:
    named = [w["id"] for w in ws if w.get("name") == name]
    if named:
        print(named[0])
    elif len(ws) == 1:
        print(ws[0]["id"])
'
}

if [ -z "${CFN_MGMT_URL:-}" ]; then
  log "CFN_MGMT_URL unset; starting without resolving WORKSPACE_ID"
else
  resolved=""
  # The mgmt plane is healthy before the backend starts, but may create its default workspace a moment later.
  for _ in $(seq 1 30); do
    resolved="$(curl -sf -m 5 "$CFN_MGMT_URL/api/workspaces" 2>/dev/null | pick_workspace || true)"
    [ -n "$resolved" ] && break
    sleep 2
  done
  if [ -z "$resolved" ]; then
    log "ERROR: no CFN workspace found at $CFN_MGMT_URL/api/workspaces after 60 s" >&2
    exit 1
  fi
  if [ "$resolved" = "${WORKSPACE_ID:-}" ]; then
    log "using WORKSPACE_ID from .env: $resolved"
  elif [ -n "${WORKSPACE_ID:-}" ]; then
    log "WORKSPACE_ID from .env ($WORKSPACE_ID) is not a CFN workspace (database was reset?); using '$WS_NAME' $resolved"
  else
    log "WORKSPACE_ID not set; using CFN workspace '$WS_NAME' $resolved"
  fi
  WORKSPACE_ID="$resolved"
  export WORKSPACE_ID
fi

exec ./start.sh
