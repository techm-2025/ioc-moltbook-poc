#!/usr/bin/env bash
# Apply the tracked agent definitions in ./hermes/agents to the 4 Hermes agents.
#
# Each agent's container re-applies ./hermes/agents on every boot (hermes/agent-seed.sh, installed
# as /etc/cont-init.d/03-agent-seed), so all this script has to do is rebuild the image if
# needed, recreate the agents, and verify the result.
#
#   scripts/apply_agents.sh            # all four agents
#   scripts/apply_agents.sh careermax  # just one
#
# A changed SOUL.md archives that agent's conversation history (state.db) to
# /opt/data/backups/persona-change-<timestamp>/ inside its volume, so the new persona applies.
set -euo pipefail
cd "$(dirname "$0")/.."

AGENTS=("$@")
[ ${#AGENTS[@]} -eq 0 ] && AGENTS=(financeguard careermax studentlife riskanalyst)

docker compose build "${AGENTS[0]}"
docker compose up -d --force-recreate "${AGENTS[@]}"

# s6 does not forward cont-init output to `docker logs`, and agent.log lives on the persistent
# volume (it holds lines from earlier boots), so wait for a room connection logged AFTER this
# container started (~15-40 s after boot).
last_sse() { docker exec "hermes_spoke_$1" sh -c 'grep "SSE connected" /opt/data/logs/agent.log 2>/dev/null | tail -1 | cut -c1-19'; }
started() { docker inspect -f '{{.State.StartedAt}}' "hermes_spoke_$1" | cut -c1-19 | tr T ' '; }

echo "Waiting for the agents to finish booting..."
for a in "${AGENTS[@]}"; do
  since=$(started "$a")
  for _ in $(seq 1 60); do
    s=$(last_sse "$a" || true)
    [ -n "$s" ] && [[ "$s" > "$since" || "$s" == "$since" ]] && break
    sleep 2
  done
done

fail=0
for a in "${AGENTS[@]}"; do
  c="hermes_spoke_$a"
  since=$(started "$a")
  soul_ok=$(docker exec "$c" sh -c "cmp -s /opt/data/SOUL.md /opt/agent-def/$a/SOUL.md && echo yes || echo NO")
  room=$(docker exec "$c" sh -c "grep -A1 'agents:' /opt/data/config.yaml | tail -1 | tr -d ' -'")
  sse=$(last_sse "$a" || true)
  if [ -n "$sse" ] && [[ "$sse" > "$since" || "$sse" == "$since" ]]; then conn="$sse"; else conn="NO (since $since)"; fi
  printf '%-13s SOUL.md matches repo: %-3s  room handle: %-13s  room connected: %s\n' "$a" "$soul_ok" "$room" "$conn"
  [ "$soul_ok" = yes ] && [ "$room" = "$a" ] && [ "${conn#NO}" = "$conn" ] || fail=1
done
exit $fail
