#!/usr/bin/env bash
# Start the whole stack, wait until it is set up and verified, then print the health report and
# the list of UIs / dashboards to open.
#
#   scripts/up.sh                       # = docker compose up -d --build --wait, then the report
#   scripts/up.sh --wait-timeout 600    # extra arguments go to `docker compose up`
#
# Why a script: in detached mode (-d) Docker Compose prints only container states, never container
# output, so the report that the stack-health container prints is not shown by `docker compose up -d`.
# It is always available afterwards with: docker compose logs stack-health
set -uo pipefail
cd "$(dirname "$0")/.."

docker compose up -d --build --wait "$@"
rc=$?

# The latest report block (stack-health prints one whenever the result changes).
report=$(docker compose logs --no-log-prefix stack-health 2>/dev/null \
  | awk '/^===== /{buf=""} {buf=buf $0 "\n"} END{printf "%s", buf}')

echo
if [ -n "$report" ]; then
  printf '%s\n' "$report"
else
  echo "No health report yet: stack-health did not start (a service it waits for failed)."
  echo "Check:  docker compose ps -a ; docker compose logs mycelium-setup ; docker compose logs mycelium-backend"
fi

if [ $rc -ne 0 ]; then
  echo
  echo "docker compose up failed (exit $rc). Fix the [FAIL] items above; full log: docker compose logs stack-health"
fi
exit $rc
