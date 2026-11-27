#!/command/with-contenv sh
# shellcheck shell=sh
# Apply this agent's tracked definition (hermes/agents, mounted read-only at /opt/agent-def)
# to its data volume on EVERY boot, so the persona and config can't drift.
#
# Installed as /etc/cont-init.d/03-agent-seed. Runs as root after 01-hermes-setup (the
# stage2 hook has chowned and seeded $HERMES_HOME) and before s6 starts the gateway.
#
#   hermes/agents/<name>/SOUL.md              -> $HERMES_HOME/SOUL.md                (overwritten)
#   hermes/agents/common/config.template.yaml -> $HERMES_HOME/config.yaml            (rendered, overwritten)
#   hermes/agents/common/plugins/mycelium/    -> $HERMES_HOME/plugins/mycelium/      (replaced)
#   hermes/agents/common/mycelium-cli.toml    -> $HERMES_HOME/home/.mycelium/config.toml
#
# If SOUL.md changed since the last boot, state.db is archived to backups/ first: Hermes
# stores each conversation's system prompt, so old conversations would keep the old persona.
set -eu

HERMES_HOME="${HERMES_HOME:-/opt/data}"
DEF="${AGENT_DEF_DIR:-/opt/agent-def}"
NAME="${MYCELIUM_AGENT_NAME:-}"

log() { echo "[agent-seed] $*"; }

if [ -z "$NAME" ]; then log "MYCELIUM_AGENT_NAME unset; skipping"; exit 0; fi
if [ ! -d "$DEF" ]; then log "$DEF not mounted; skipping"; exit 0; fi
SOUL_SRC="$DEF/$NAME/SOUL.md"
if [ ! -f "$SOUL_SRC" ]; then log "missing $SOUL_SRC; refusing to start with a stock persona" >&2; exit 1; fi

# 1. Persona: archive conversation history when the persona changed.
new_sum="$(sha256sum "$SOUL_SRC" | cut -d' ' -f1)"
old_sum="$(cat "$HERMES_HOME/.agent-seed.soul.sha256" 2>/dev/null || true)"
if [ -n "$old_sum" ] && [ "$old_sum" != "$new_sum" ] && [ -f "$HERMES_HOME/state.db" ]; then
    ts="$(date -u +%Y%m%dT%H%M%SZ)"
    mkdir -p "$HERMES_HOME/backups/persona-change-$ts"
    mv "$HERMES_HOME"/state.db* "$HERMES_HOME/backups/persona-change-$ts/" 2>/dev/null || true
    rm -f "$HERMES_HOME/.skills_prompt_snapshot.json"
    log "SOUL.md changed: archived conversations to backups/persona-change-$ts"
fi
cp -f "$SOUL_SRC" "$HERMES_HOME/SOUL.md"
echo "$new_sum" > "$HERMES_HOME/.agent-seed.soul.sha256"

# 2. Hermes config, rendered for this agent. These values come from .env / docker-compose.yml via
#    the container env. ${CUSTOM_API_KEY} is NOT rendered: Hermes expands it at runtime, so the
#    key is never stored on disk.
RENDER_VARS="AGENT_LLM_MODEL AGENT_LLM_BASE_URL MYCELIUM_ROOM MYCELIUM_API_URL"
for var in $RENDER_VARS; do
    eval "val=\${$var:-}"
    if [ -z "$val" ]; then log "$var is empty; set it in .env" >&2; exit 1; fi
    case "$val" in *"|"*) log "$var must not contain '|'" >&2; exit 1 ;; esac
done
render() {  # render <template> <dest>: fill __AGENT__ and ${VAR} for each RENDER_VARS entry
    script="s|__AGENT__|$NAME|g"
    for var in $RENDER_VARS; do
        eval "val=\${$var}"
        script="$script;s|\${$var}|$val|g"
    done
    sed -e "$script" "$1" > "$2"
}
render "$DEF/common/config.template.yaml" "$HERMES_HOME/config.yaml"

# 3. Mycelium room plugin.
mkdir -p "$HERMES_HOME/plugins"
rm -rf "$HERMES_HOME/plugins/mycelium"
cp -R "$DEF/common/plugins/mycelium" "$HERMES_HOME/plugins/mycelium"

# 4. Mycelium CLI config for the hermes user (HOME=/opt/data/home inside the agent).
mkdir -p "$HERMES_HOME/home/.mycelium"
render "$DEF/common/mycelium-cli.toml" "$HERMES_HOME/home/.mycelium/config.toml"

chown -R hermes:hermes "$HERMES_HOME/SOUL.md" "$HERMES_HOME/config.yaml" "$HERMES_HOME/plugins" \
    "$HERMES_HOME/home" "$HERMES_HOME/.agent-seed.soul.sha256" 2>/dev/null || true
# Files copied from a Windows bind mount arrive with +x; make them plain files again.
chmod 644 "$HERMES_HOME/SOUL.md" "$HERMES_HOME/config.yaml" "$HERMES_HOME/home/.mycelium/config.toml"
find "$HERMES_HOME/plugins/mycelium" -type f -exec chmod 644 {} + 2>/dev/null || true
[ -d "$HERMES_HOME/backups" ] && chown -R hermes:hermes "$HERMES_HOME/backups" 2>/dev/null || true

log "applied definition for '$NAME' (SOUL sha256 ${new_sum%"${new_sum#????????????}"})"
