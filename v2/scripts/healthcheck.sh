#!/usr/bin/env bash
# Health check for the whole stack: containers, Mycelium, CFN, the 4 Hermes agents (gateway,
# room connection, dashboard login) and the LLM proxy. Ends with the list of UIs / dashboards /
# APIs you can open.
#
# On the host:
#   scripts/healthcheck.sh             # check once
#   scripts/healthcheck.sh --wait      # retry until everything passes (default timeout 180 s)
#   scripts/healthcheck.sh --wait 300  # custom timeout in seconds
#
# Inside Docker (the `stack-health` service in docker-compose.yml runs this automatically):
#   healthcheck.sh --watch             # re-check every HC_INTERVAL s (default 60), print the report
#                                      # whenever the result changes, and mark the container healthy
#                                      # (/tmp/stack-healthy) while everything passes.
#   `docker compose up -d --wait` therefore returns only once the whole stack is verified.
#
# Exit code 0 = all checks passed, 1 = at least one failed. Host mode needs bash, curl and docker
# (Linux, macOS, WSL or Git Bash). Secrets are never printed.
set -uo pipefail

MODE=once; TIMEOUT=180
case "${1:-}" in
  --wait)  MODE=wait;  [ -n "${2:-}" ] && TIMEOUT="$2" ;;
  --watch) MODE=watch ;;
  "") ;;
  *) echo "usage: $0 [--wait [seconds] | --watch]" >&2; exit 2 ;;
esac

if [ "$MODE" = watch ]; then
  # Container mode: settings come from the compose environment; services are reached by name.
  IN_DOCKER=1
else
  IN_DOCKER=0
  cd "$(dirname "$0")/.."
  [ -f .env ] || { echo "No .env found in $(pwd); copy .env.example to .env first." >&2; exit 1; }
  set -a; # shellcheck disable=SC1091
  . ./.env >/dev/null 2>&1; set +a
fi

BACKEND_PORT="${MYCELIUM_BACKEND_PORT:-8000}"
UI_PORT="${MYCELIUM_UI_PORT:-3000}"
DB_PORT="${MYCELIUM_DB_PORT:-5433}"
ROOM="${MYCELIUM_ROOM:-moltbook-mycelium-demo}"
DASH_USER="${HERMES_DASHBOARD_USER:-admin}"
# CFN ports are fixed to 9000 / 9002 in mycelium/docker-compose.yml.
CFN_MGMT=9000; CFN_NODE=9002
AGENTS=(financeguard careermax studentlife riskanalyst)
agent_port() {  # host port of an agent's dashboard
  case "$1" in
    financeguard) echo "${FINANCEGUARD_PORT:-8641}" ;;
    careermax)    echo "${CAREERMAX_PORT:-8645}" ;;
    studentlife)  echo "${STUDENTLIFE_PORT:-8643}" ;;
    riskanalyst)  echo "${RISKANALYST_PORT:-8644}" ;;
  esac
}

if [ $IN_DOCKER = 1 ]; then
  BACKEND_URL=http://mycelium-backend:8000
  UI_URL=http://mycelium-frontend:3000
  MGMT_URL=http://ioc-cfn-mgmt-plane-svc:$CFN_MGMT
  NODE_URL=http://ioc-cognition-fabric-node-svc:$CFN_NODE
  agent_url() { echo "http://$1:9119"; }            # compose service name, container port
  where() { echo "$1"; }
else
  BACKEND_URL=http://127.0.0.1:$BACKEND_PORT
  UI_URL=http://127.0.0.1:$UI_PORT
  MGMT_URL=http://127.0.0.1:$CFN_MGMT
  NODE_URL=http://127.0.0.1:$CFN_NODE
  agent_url() { echo "http://127.0.0.1:$(agent_port "$1")"; }
  where() { echo "$1 (:$(agent_port "$1"))"; }
fi

if [ -t 1 ]; then G=$'\e[32m'; R=$'\e[31m'; Y=$'\e[33m'; B=$'\e[1m'; N=$'\e[0m'; else G=; R=; Y=; B=; N=; fi
FAILS=0; QUIET=0
ok()   { [ $QUIET = 1 ] || printf '  %s[ OK ]%s %s\n' "$G" "$N" "$1"; }
bad()  { FAILS=$((FAILS+1)); [ $QUIET = 1 ] || printf '  %s[FAIL]%s %s\n         %s-> %s%s\n' "$R" "$N" "$1" "$Y" "$2" "$N"; }
hdr()  { [ $QUIET = 1 ] || printf '\n%s%s%s\n' "$B" "$1" "$N"; }
get()  { curl -s -m 5 "$@" 2>/dev/null; }
code() { curl -s -m 5 -o /dev/null -w '%{http_code}' "$@" 2>/dev/null; }
json_escape() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }
# LLM /models probe: from the host via docker exec in the given container, in Docker directly.
llm_probe() {  # llm_probe <container> <url var> <key var>
  if [ $IN_DOCKER = 1 ]; then
    curl -s -m 8 -o /dev/null -w '%{http_code}' "${!2:-}/models" -H "Authorization: Bearer ${!3:-}" 2>/dev/null
  else
    docker exec "$1" sh -c "curl -s -m 8 -o /dev/null -w '%{http_code}' \"\$$2/models\" -H \"Authorization: Bearer \$$3\"" 2>/dev/null
  fi
}

run_checks() {
  FAILS=0

  if [ $IN_DOCKER = 0 ]; then
    hdr "1. Containers"
    for c in mycelium-db ioc-cfn-mgmt-plane-svc ioc-cognition-fabric-node-svc mycelium-backend mycelium-frontend \
             hermes_spoke_financeguard hermes_spoke_careermax hermes_spoke_studentlife hermes_spoke_riskanalyst; do
      st=$(docker inspect -f '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{else}}-{{end}} {{.RestartCount}}' "$c" 2>/dev/null)
      if [ -z "$st" ]; then bad "$c" "container not found; run: docker compose up -d"; continue; fi
      read -r state health restarts <<<"$st"
      if [ "$state" = running ] && { [ "$health" = healthy ] || [ "$health" = - ]; }; then
        ok "$(printf '%-30s running%s' "$c" "$([ "$health" = - ] || echo ", $health")$([ "$restarts" = 0 ] || echo ", restarts=$restarts")")"
      else
        bad "$c is $state/$health" "docker compose logs --tail 50 ${c#hermes_spoke_}"
      fi
    done
    for c in mycelium-data-init mycelium-setup; do
      st=$(docker inspect -f '{{.State.Status}} {{.State.ExitCode}}' "$c" 2>/dev/null)
      [ "$st" = "exited 0" ] && ok "$(printf '%-30s exited 0 (one-shot, expected)' "$c")" \
                             || bad "$c: ${st:-missing}" "docker compose logs $c"
    done
  fi

  hdr "2. Mycelium"
  if [ $IN_DOCKER = 0 ]; then
    if docker exec mycelium-db pg_isready -U postgres -d mycelium >/dev/null 2>&1; then ok "Postgres accepts connections (host port $DB_PORT)"
    else bad "Postgres not ready" "docker compose logs mycelium-db"; fi
  fi

  h=$(get "$BACKEND_URL/health")
  if printf '%s' "$h" | grep -q '"status":"ok","service":"mycelium-backend"'; then
    ok "Backend /health ok (version $(printf '%s' "$h" | grep -o '"version":"[^"]*"' | head -1 | cut -d'"' -f4))"
    for part in database embedding llm; do
      printf '%s' "$h" | grep -q "\"$part\":{\"status\":\"ok\"" && ok "  backend $part: ok" \
        || bad "  backend $part not ok" "curl -s localhost:$BACKEND_PORT/health  (check LLM_* vars in .env)"
    done
  else bad "Backend /health not ok" "docker compose logs --tail 50 mycelium-backend"; fi

  r=$(get "$BACKEND_URL/api/rooms/$ROOM")
  ROOM_WS=$(printf '%s' "$r" | grep -o '"workspace_id":"[^"]*"' | cut -d'"' -f4)
  if printf '%s' "$r" | grep -q "\"name\":\"$ROOM\""; then
    if printf '%s' "$r" | grep -q '"mas_id":"[^"]'; then ok "Room '$ROOM' exists and has a CFN mas_id"
    else bad "Room '$ROOM' has no mas_id" "docker compose up -d mycelium-setup ; docker compose logs mycelium-setup"; fi
  else bad "Room '$ROOM' does not exist" "docker compose up -d mycelium-setup ; docker compose logs mycelium-setup"; fi

  [ "$(code "$UI_URL/")" = 200 ] && ok "Frontend UI responds (host port $UI_PORT)" \
    || bad "Frontend UI not reachable (host port $UI_PORT)" "docker compose logs --tail 50 mycelium-frontend"

  hdr "3. Cognition Fabric (CFN)"
  get "$MGMT_URL/health" | grep -qi 'ok\|healthy' && ok "Mgmt plane /health ok (:$CFN_MGMT)" \
    || bad "Mgmt plane /health failed" "docker compose logs --tail 50 ioc-cfn-mgmt-plane-svc"
  # The backend resolves its workspace at startup (mycelium/scripts/backend-start.sh); what matters
  # is that the room is linked to a workspace the CFN really has.
  w=$(get "$MGMT_URL/api/workspaces")
  if [ -z "$ROOM_WS" ]; then bad "Room '$ROOM' has no workspace_id" "docker compose up -d mycelium-setup ; docker compose logs mycelium-setup"
  elif printf '%s' "$w" | grep -q "\"id\":\"$ROOM_WS\""; then ok "Room is linked to live CFN workspace $ROOM_WS"
  else bad "Room's workspace $ROOM_WS is not in the CFN (stale after a reset)" "docker compose up -d --force-recreate mycelium-backend mycelium-setup"; fi
  get "$NODE_URL/api/internal/diagnostics/health" | grep -q '"status":"healthy"' && ok "Fabric node health ok (:$CFN_NODE)" \
    || bad "Fabric node health failed" "docker compose logs --tail 50 ioc-cognition-fabric-node-svc"

  hdr "4. Hermes agents"
  body="{\"provider\":\"basic\",\"username\":\"$(json_escape "$DASH_USER")\",\"password\":\"$(json_escape "${HERMES_DASHBOARD_PASSWORD:-}")\"}"
  for a in "${AGENTS[@]}"; do
    u=$(agent_url "$a"); s=$(get "$u/api/status")
    if [ -z "$s" ]; then bad "$(where "$a"): dashboard not reachable" "docker compose logs --tail 50 $a"; continue; fi
    printf '%s' "$s" | grep -q '"overall":"ok"' && ok "$(where "$a") overall ok, gateway running" \
      || bad "$(where "$a") status not ok" "curl -s localhost:$(agent_port "$a")/api/status ; docker compose logs --tail 50 $a"
    printf '%s' "$s" | grep -q '"mycelium-room":{"state":"connected"' && ok "  $a connected to Mycelium room" \
      || bad "  $a not connected to the Mycelium room" "room missing or backend down; then scripts/apply_agents.sh $a"
    lc=$(code -H 'Content-Type: application/json' --data "$body" "$u/auth/password-login")
    [ "$lc" = 200 ] && ok "  $a dashboard login works (user '$DASH_USER')" \
      || bad "  $a dashboard login failed (HTTP $lc)" "check HERMES_DASHBOARD_USER/PASSWORD in .env, then docker compose up -d"
  done

  hdr "5. LLM proxy (from inside Docker)"
  lb=$(llm_probe mycelium-backend LLM_BASE_URL LLM_API_KEY)
  [ "$lb" = 200 ] && ok "LLM_BASE_URL reachable with LLM_API_KEY (backend/CFN)" \
    || bad "LLM_BASE_URL not usable from Docker (HTTP ${lb:-none})" "check LLM_BASE_URL / LLM_API_KEY; use host.docker.internal, not localhost"
  la=$(llm_probe hermes_spoke_financeguard AGENT_LLM_BASE_URL CUSTOM_API_KEY)
  [ "$la" = 200 ] && ok "AGENT_LLM_BASE_URL reachable with CUSTOM_API_KEY (agents)" \
    || bad "AGENT_LLM_BASE_URL not usable from Docker (HTTP ${la:-none})" "check AGENT_LLM_BASE_URL / CUSTOM_API_KEY in .env"
}

print_links() {
  printf '\n%sWhat you can open%s\n' "$B" "$N"
  printf '  %-34s %s\n' "Mycelium UI (rooms, sessions)" "http://localhost:$UI_PORT"
  printf '  %-34s %s\n' "Mycelium API docs (Swagger)" "http://localhost:$BACKEND_PORT/docs"
  printf '  %-34s %s\n' "Mycelium API health" "http://localhost:$BACKEND_PORT/health"
  printf '  %-34s %s\n' "Mycelium room (JSON)" "http://localhost:$BACKEND_PORT/api/rooms/$ROOM"
  for a in "${AGENTS[@]}"; do
    printf '  %-34s %s\n' "Hermes dashboard: $a" "http://localhost:$(agent_port "$a")   (status: /api/status)"
  done
  printf '  %-34s %s\n' "CFN mgmt plane (API)" "http://localhost:$CFN_MGMT/health , /api/workspaces"
  printf '  %-34s %s\n' "CFN fabric node (API)" "http://localhost:$CFN_NODE/api/internal/diagnostics/health"
  printf '  %-34s %s\n' "Postgres" "localhost:$DB_PORT  (user postgres, db mycelium)"
  printf '  Dashboard login: user %s, password = HERMES_DASHBOARD_PASSWORD in .env\n' "'$DASH_USER'"
  printf '  All ports are bound to 127.0.0.1 only.\n'
}

if [ "$MODE" = watch ]; then
  # Fast retries until the first full pass, then every HC_INTERVAL s. The report is printed to the
  # container log only when the set of failures changes, so `docker compose logs stack-health` stays short.
  MARKER=/tmp/stack-healthy; INTERVAL="${HC_INTERVAL:-60}"; last=unset
  fast_until=$(( $(date +%s) + 300 ))   # retry every 5 s for the first 5 min after boot
  rm -f "$MARKER"
  while :; do
    report=$(run_checks; echo "__FAILS=$FAILS")
    FAILS=${report##*__FAILS=}; report=${report%__FAILS=*}
    sig=$(printf '%s' "$report" | grep -F '[FAIL]')
    if [ "$sig" != "$last" ]; then
      printf '\n===== %s  stack health: %s =====\n' "$(date -u '+%Y-%m-%d %H:%M:%SZ')" \
        "$([ "$FAILS" = 0 ] && echo 'ALL CHECKS PASSED' || echo "$FAILS check(s) failing")"
      printf '%s\n' "$report"
      [ "$FAILS" = 0 ] && print_links
      last=$sig
    fi
    if [ "$FAILS" = 0 ]; then touch "$MARKER"; sleep "$INTERVAL"
    else rm -f "$MARKER"; [ "$(date +%s)" -lt "$fast_until" ] && sleep 5 || sleep "$INTERVAL"; fi
  done
fi

if [ "$MODE" = wait ]; then
  deadline=$(( $(date +%s) + TIMEOUT ))
  QUIET=1
  until run_checks; [ $FAILS = 0 ] || [ "$(date +%s)" -ge "$deadline" ]; do
    printf 'waiting: %d check(s) failing, retrying in 5 s...\n' "$FAILS"; sleep 5
  done
  QUIET=0
fi
run_checks
print_links
if [ $FAILS = 0 ]; then printf '\n%sAll checks passed.%s\n' "$G" "$N"; exit 0; fi
printf '\n%s%d check(s) failed.%s See docs/RUNBOOK.md (Troubleshooting).\n' "$R" "$FAILS" "$N"; exit 1
