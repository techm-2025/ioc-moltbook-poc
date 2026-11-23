!/usr/bin/env bash
set -e

AGENTS=("financeguard" "careermax" "studentlife" "riskanalyst")


echo "======================================================================"
echo " 🧹 DEEP PURGING PERSISTENT AGENT STATE & DATABASES"
echo "======================================================================"

# 1. Stop host gateway
echo "Stopping host Hermes gateway..."
hermes gateway stop || true

# 2. Stop and remove Docker containers and volumes
docker compose down -v

# 3. Target Purge across all profiles (~/hermes_network/profiles/*)
PROFILES_DIR="$HOME/hermes_network/profiles"

for PROFILE in "$PROFILES_DIR"/*/; do
  if [ -d "$PROFILE" ]; then
    echo "Clearing profile state in: $PROFILE"

    sudo chmod 755 "$PROFILE"
    # Remove state databases (SQLite state trees)
    sudo rm -f "$PROFILE"/state.db*
    sudo rm -f "$PROFILE"/projects.db*
    sudo rm -f "$PROFILE"/kanban.db*
    sudo rm -rf "$PROFILE"/kanban/*

    # Remove active session & memory folders
    sudo rm -rf "$PROFILE"/sessions/*
    sudo rm -rf "$PROFILE"/memories/*
    sudo rm -rf "$PROFILE"/cron/*
    sudo rm -rf "$PROFILE"/logs/*
    sudo rm -rf "$PROFILE"/cache/*
    sudo rm -rf "$PROFILE"/skills/*
    sudo rm -rf "$PROFILE"/.venv

    # Remove tracking & prompt snapshot files
    sudo rm -f "$PROFILE"/.restart_failure_counts
    sudo rm -f "$PROFILE"/.skills_prompt_snapshot.json
    sudo rm -f "$PROFILE"/gateway_state.json
    sudo rm -f "$PROFILE"/channel_directory.json

    # ------------------------------------------------------------------
    # CONDITION: Purge all markdown files EXCEPT customized SOUL.md
    # ------------------------------------------------------------------
    sudo find "$PROFILE" -maxdepth 1 -type f -name "*.md" ! -name "SOUL.md" -exec sudo rm -f {} +
  fi
done

# 4. Clear shared data directory
sudo rm -rf /opt/data/*
sudo rm -rf ~/.hermes/sessions/*

# 5. Fix permissions so non-root container users can create fresh DBs
sudo mkdir -p /opt/data
sudo chmod -R 777 /opt/data "$PROFILES_DIR"


echo "======================================================================"
echo " REBUILDING DOCKER CONTAINERS"

docker compose up -d --force-recreate

# Wait briefly for container daemons to initialize
sleep 3

echo "======================================================================"
echo " 3. INJECTING MYCELIUM CLI & CREATING AGENTS"
echo "======================================================================"
for AGENT in "${AGENTS[@]}"; do
  echo "🚀 Injecting Mycelium CLI into hermes_spoke_${AGENT}..."
  docker exec -i "hermes_spoke_${AGENT}" bash -c "
    echo -e '#!/bin/sh\nexit 0' > /usr/local/bin/docker && \
    chmod +x /usr/local/bin/docker && \
    cp /usr/local/bin/docker /usr/local/bin/systemctl && \
    curl -fsSL https://mycelium-io.github.io/mycelium/install.sh | bash -s -- --version 1.1.3 && \
    export PATH=\"\$HOME/.local/bin:\$PATH\" && \
    mycelium init --api-url http://host.docker.internal:8000 && \
    mycelium adapter add hermes && \
    mycelium agent create ${AGENT} --adapter hermes --room moltbook-mycelium-demo
  "
  echo "✅ ${AGENT} configured!"
done

echo "======================================================================"
echo " 4. UPDATING PATH IN .bashrc"
echo "======================================================================"
for AGENT in "${AGENTS[@]}"; do
  echo "Updating PATH for hermes_spoke_${AGENT}..."
  docker exec -it "hermes_spoke_${AGENT}" bash -c \
    'echo "export PATH=\"/root/.local/bin:\$PATH\"" >> ~/.bashrc && source ~/.bashrc && echo $PATH'
done

echo "======================================================================"
echo " 5. PATCHING MYCELIUM ADAPTER"
echo "======================================================================"
for AGENT in "${AGENTS[@]}"; do
  echo "Patching adapter.py in hermes_spoke_${AGENT}..."
  docker exec -it "hermes_spoke_${AGENT}" \
    sed -i 's/async def connect(self)/async def connect(self, *args, **kwargs)/g' /opt/data/plugins/mycelium/adapter.py
done

echo "======================================================================"
echo " 6. COPYING BINARY & GRANTING ROOT TRAVERSAL PERMISSIONS"
echo "======================================================================"
for AGENT in "${AGENTS[@]}"; do
 echo "🔧Copying Mycelium v1.1.3 binary to /usr/local/bin +  Granting /root traversal + Verifying non-root execution for hermes_spoke_${AGENT}..."
  docker exec -u 0 -i "hermes_spoke_${AGENT}" bash -c "
    chmod -R 755 /root && \
    cp -f /root/.local/bin/mycelium /usr/local/bin/mycelium && \
    chmod 755 /usr/local/bin/mycelium
  "

  echo "🧪 Non-root Verification Test for ${AGENT}:"
  docker exec -i "hermes_spoke_${AGENT}" /bin/bash -c "which mycelium && mycelium --version"
done

echo "======================================================================"
echo " 7. STARTING HERMES GATEWAY"
echo "======================================================================"
hermes gateway start

echo "🎉 Environment setup complete!"