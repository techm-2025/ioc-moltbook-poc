# syntax=docker/dockerfile:1
# Hermes agent image with the Mycelium CLI pre-installed.
#
# Why: `mycelium adapter add hermes` / `mycelium agent create` need the CLI, and installing it
# inside each running container takes ~10 min and is lost on container recreate. Baking it
# into the image means one download at build time and a ~20 s registration per agent.
#
# The CLI is only an installer/registrar: the Hermes plugin it writes (mycelium-room) talks to
# Mycelium over HTTP + SSE and does not call the CLI at runtime.
#
# Build (this runs the official Mycelium installer script, pinned to MYCELIUM_VERSION):
#   docker compose build
ARG HERMES_BASE=nousresearch/hermes-agent:latest
FROM ${HERMES_BASE}

ARG MYCELIUM_VERSION=1.1.3
ARG MYCELIUM_INSTALL_URL=https://mycelium-io.github.io/mycelium/install.sh

USER root

# The Mycelium installer / `adapter add` probe for `docker` and `systemctl`; neither exists
# (or is wanted) inside this container, so provide no-op stubs.
RUN printf '#!/bin/sh\nexit 0\n' > /usr/local/bin/docker \
 && chmod +x /usr/local/bin/docker \
 && cp -f /usr/local/bin/docker /usr/local/bin/systemctl

# Install the CLI (pinned), expose it to every user, and fail the build if it does not run.
RUN curl -fsSL "${MYCELIUM_INSTALL_URL}" | bash -s -- --version "${MYCELIUM_VERSION}" \
 && cp -f /root/.local/bin/mycelium /usr/local/bin/mycelium \
 && chmod 755 /usr/local/bin/mycelium /root /root/.local /root/.local/bin \
 && mycelium --version

# Boot-time seeding of the agent definition (SOUL.md, config.yaml, Mycelium plugin + CLI config)
# from hermes/agents, mounted read-only at /opt/agent-def by docker-compose.yml. s6 runs
# /etc/cont-init.d/* in order on every container start, after 01-hermes-setup.
COPY agent-seed.sh /etc/cont-init.d/03-agent-seed
RUN sed -i 's/\r$//' /etc/cont-init.d/03-agent-seed \
 && chmod 755 /etc/cont-init.d/03-agent-seed
