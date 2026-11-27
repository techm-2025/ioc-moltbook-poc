# Runbook: Mycelium + Hermes stack (v2)

How to bring the v2 stack up from a fresh clone, verify it, operate it, and fix the usual failures.
Everything runs from the `v2/` folder with one `v2/.env` and one `docker compose` project (`mycelium`). Run `cd v2` first. Every path and command below is relative to `v2/`.

## 1. What runs

| Service | Container | Host port | Purpose |
| :--- | :--- | :--- | :--- |
| Postgres | `mycelium-db` | `127.0.0.1:5433` | Mycelium + CFN databases (`mycelium`, `cfn_mgmt`, `cfn_cp`) |
| Mycelium backend | `mycelium-backend` | `8000` | Rooms, negotiation sessions API |
| Room setup (one-shot) | `mycelium-setup` | none | Creates `MYCELIUM_ROOM` + links it to the CFN on every `up` (3.4) |
| Mycelium UI | `mycelium-frontend` | `3000` | Web UI |
| CFN mgmt plane | `ioc-cfn-mgmt-plane-svc` | `9000` | Workspaces / CFN control |
| CFN node | `ioc-cognition-fabric-node-svc` | `9002` | Runs negotiations (calls the LLM) |
| 4 Hermes agents | `hermes_spoke_<agent>` | financeguard `8641`, studentlife `8643`, riskanalyst `8644`, careermax `8645` | Agents that listen in the Mycelium room |
| Stack health check | `stack-health` | none | Re-checks the whole stack; `healthy` only while everything passes (3.6) |
| Bridge (on demand) | `ioc_bridge` | none | Moltbook <-> Mycelium; compose profile `bridge`, not started by `up` |

Startup order is enforced by healthchecks: db -> mgmt plane -> backend (resolves the workspace) -> mycelium-setup (room) -> agents -> stack-health.

## 2. Prerequisites

- Docker with Compose v2.20+ (Docker Desktop on Windows is fine; no Mycelium CLI needed on the host).
- An **OpenAI-compatible LiteLLM proxy running on the host** (default `:4000`) and a key it accepts.
  Check from the host: `curl http://localhost:4000/v1/models -H "Authorization: Bearer <key>"`.
- Use a paid or high-quota model. One negotiation makes many calls within seconds; free tiers (e.g. Gemini 15 req/min) return HTTP 429.
- Internet access on first run (pulls `ghcr.io/mycelium-io/*`, `ghcr.io/outshift-open/*`, `nousresearch/hermes-agent`, and the Mycelium installer during the Hermes image build).

## 3. First-time bring-up

### 3.1 Configure `.env`

```powershell
cd v2
Copy-Item .env.example .env      # Linux/WSL: cp .env.example .env
```

Fill in at least these (containers cannot use `localhost`; use `host.docker.internal`):

| Variable | Value |
| :--- | :--- |
| `CUSTOM_API_KEY` | LiteLLM key the agents use (**required**, compose refuses to start without it) |
| `HERMES_DASHBOARD_PASSWORD` | Dashboard login password (**required**) |
| `AGENT_LLM_BASE_URL` | `http://host.docker.internal:4000/v1` |
| `AGENT_LLM_MODEL` | Model name served by the proxy, no `openai/` prefix |
| `LLM_BASE_URL` | `http://host.docker.internal:4000/v1` |
| `LLM_MODEL` | `openai/<model>` (the `openai/` prefix is for litellm routing) |
| `LLM_API_KEY` | Proxy key (usually same as `CUSTOM_API_KEY`) |
| `WORKSPACE_ID` | Leave empty: resolved at backend startup (3.4). Set only to pin another CFN workspace |
| `MYCELIUM_DB_PASSWORD`, `ADMIN_USER_PASSWORD` | Change from `change-me` |
| `MYCELIUM_ROOM` | Room name, default `moltbook-mycelium-demo` |

Only for the bridge: `LITELLM_URL` (base URL, e.g. `http://host.docker.internal:4000`), `LITELLM_MODEL`, `LITELLM_API_KEY*`, `MOLTBOOK_API_KEY*`.

Notes:
- Agent model, base URL and room are no longer hard-coded. They come from `AGENT_LLM_MODEL`, `AGENT_LLM_BASE_URL`, `MYCELIUM_ROOM`. `AGENT_LLM_MODEL` falls back to `LITELLM_MODEL`, and `AGENT_LLM_BASE_URL` falls back to `LLM_BASE_URL`.
- `agent-seed.sh` **refuses to start an agent** if any of `AGENT_LLM_MODEL`, `AGENT_LLM_BASE_URL`, `MYCELIUM_ROOM`, `MYCELIUM_API_URL` is empty, or contains `|`.
- `CUSTOM_API_KEY` is never written to disk; Hermes expands it at runtime.
- `LITELLM_URL` may be a base URL, `.../v1` or the full `.../v1/chat/completions`; the code normalizes all three.

### 3.2 Start the stack

```powershell
bash scripts/up.sh
```

This is `docker compose up -d --build --wait` followed by the health report and the list of UIs/dashboards (3.6, 3.7); it returns once the stack is set up and verified. With plain `docker compose up -d` you only see container states, because Compose never prints container output in detached mode; the report is in `docker compose logs stack-health`.

The first run builds the Hermes image (installs Mycelium CLI 1.1.3) and pulls images; allow several minutes.

### 3.3 Verify infrastructure

```powershell
docker compose ps
curl.exe -s http://127.0.0.1:8000/health
curl.exe -s http://127.0.0.1:9000/api/workspaces
```

All services should be `healthy`/`running`; `mycelium-data-init` and `mycelium-setup` should show `Exited (0)` (one-shots).

### 3.4 Workspace and room (automatic)

Both happen on every `docker compose up`, so a fresh clone or a `down -v` reset needs no manual steps:

1. **Workspace.** The backend's startup wrapper `mycelium/scripts/backend-start.sh` (mounted read-only; it then runs the image's own `./start.sh`) asks the CFN for its workspaces. It keeps `WORKSPACE_ID` if the CFN has it, otherwise uses the one named `CFN_WORKSPACE_NAME` (default `Default Workspace`). This matters because the CFN creates a **new random** id on every fresh database, so a hard-coded id goes stale after a reset. Check: `docker compose logs mycelium-backend | grep workspace`.
2. **Room.** The one-shot `mycelium-setup` service (`mycelium/scripts/setup-room.sh`) creates `MYCELIUM_ROOM` if it is missing and calls `sync-mas`, which links the room to the CFN and gives it a `mas_id` (without it negotiations fail with "CFN not configured"). It fails unless the room ends up linked to a workspace the CFN really has. The agents and the bridge start only after it succeeds. Check: `docker compose logs mycelium-setup`.

To re-run the room step by hand: `docker compose up -d mycelium-setup`. The equivalent manual calls:

```powershell
curl.exe -X POST http://127.0.0.1:8000/api/rooms -H "Content-Type: application/json" -d '{\"name\":\"moltbook-mycelium-demo\",\"is_public\":true}'
curl.exe -X POST http://127.0.0.1:8000/api/rooms/moltbook-mycelium-demo/sync-mas
```

### 3.5 Verify the agents

```powershell
bash scripts/apply_agents.sh
```

Per agent it prints: `SOUL.md matches repo: yes`, `room handle: <agent>`, `room connected: <timestamp>`. Exit code 0 means all four are healthy. Dashboards: `http://localhost:8641` / `8643` / `8644` / `8645`, login `admin` + `HERMES_DASHBOARD_PASSWORD`.

### 3.6 Full health check (automatic on every `docker compose up`)

`docker compose up` also starts a small `stack-health` container. It re-runs the full check from inside the Docker network (every 5 s for the first 5 minutes, then every 60 s, `HC_INTERVAL` in `.env`) and is `healthy` only while **every** check passes:

```powershell
bash scripts/up.sh                       # up -d --build --wait + prints the latest report and the links
docker compose up -d --build --wait      # same wait, but shows only container states; non-zero exit if not verified (after ~5 min)
docker compose ps stack-health           # healthy / unhealthy at any time
docker compose logs stack-health         # the report, printed again whenever the result changes, plus the UI links
```

Because the workspace and room are set up automatically (3.4), `--wait` also succeeds on a fresh database. If something does fail later, `stack-health` turns healthy again by itself once it is fixed, no restart needed. Without `--wait`, `up -d` returns immediately as before and the check runs in the background.

The same check can be run by hand from the host. It also covers what the container cannot see (container states, restarts, Postgres):

```powershell
bash scripts/healthcheck.sh            # check once
bash scripts/healthcheck.sh --wait     # retry until everything passes (timeout 180 s; --wait 300 for longer)
```

Read-only: it changes nothing and never prints secrets from `.env`. Exit code 0 = everything passed. Each failure prints the command to look into it. It checks (section 1 and the Postgres check run on the host only; the container relies on compose `depends_on` for those):

| # | Area | What is checked |
| :--- | :--- | :--- |
| 1 | Containers | All 9 long-running containers are `running` (and `healthy` where a healthcheck exists); `mycelium-data-init` exited 0 |
| 2 | Mycelium | Postgres `pg_isready`; backend `/health` with `database`, `embedding` and `llm` all `ok`; room `MYCELIUM_ROOM` exists **and has a `mas_id`**; UI on `:3000` responds |
| 3 | CFN | Mgmt plane `/health`; the room is linked to a workspace the CFN really has; fabric node health |
| 4 | Hermes agents | Per agent: `/api/status` reports `overall: ok` (gateway running), the `mycelium-room` platform is `connected`, and a dashboard login with `HERMES_DASHBOARD_USER` / `HERMES_DASHBOARD_PASSWORD` succeeds |
| 5 | LLM proxy | The backend reaches `LLM_BASE_URL` and the agents reach `AGENT_LLM_BASE_URL` (`/models` returns 200) **from inside the containers**, so `host.docker.internal` mistakes are caught |

It ends with the list of everything you can open (section 3.7).

### 3.7 UIs, dashboards and APIs

All ports are bound to `127.0.0.1`, so open them on the machine that runs Docker (ports follow `.env`).

| What | URL | Login | Use it for |
| :--- | :--- | :--- | :--- |
| **Mycelium UI** | `http://localhost:3000` | none | Rooms, messages posted by the bridge and agents, negotiation sessions |
| **Hermes dashboard: financeguard** | `http://localhost:8641` | `admin` / `HERMES_DASHBOARD_PASSWORD` | Agent's chats/sessions, config, logs, gateway + platform state |
| **Hermes dashboard: studentlife** | `http://localhost:8643` | same | same |
| **Hermes dashboard: riskanalyst** | `http://localhost:8644` | same | same |
| **Hermes dashboard: careermax** | `http://localhost:8645` | same | same |
| Hermes agent status (JSON) | `http://localhost:<agent port>/api/status` | none | `overall`, `gateway_state`, `gateway_platforms.mycelium-room.state` |
| Mycelium API docs (Swagger) | `http://localhost:8000/docs` | none | Try the rooms / sessions API from the browser |
| Mycelium backend health | `http://localhost:8000/health` | none | DB, embedding model and LLM status |
| Mycelium room (JSON) | `http://localhost:8000/api/rooms/<MYCELIUM_ROOM>` | none | Confirms the room and its `mas_id` |
| CFN mgmt plane | `http://localhost:9000/health`, `/api/workspaces` | none | API only (no UI); workspace ids |
| CFN fabric node | `http://localhost:9002/api/internal/diagnostics/health` | none | API only (no UI) |
| Postgres | `localhost:5433` | `postgres` / `MYCELIUM_DB_PASSWORD` | DBs `mycelium`, `cfn_mgmt`, `cfn_cp` (e.g. with psql or DBeaver) |

The Hermes dashboards use a login page (cookie session); HTTP basic-auth headers are not accepted by their APIs.

## 4. Day-to-day operations

| Task | Command |
| :--- | :--- |
| Start / stop (keeps data) | `docker compose up -d` / `docker compose stop` |
| Status | `docker compose ps` |
| Full health check + UI links | `docker compose ps stack-health` / `docker compose logs stack-health`, or `bash scripts/healthcheck.sh` from the host |
| Logs | `docker compose logs -f mycelium-backend` (or any service) |
| Agent log | `docker exec hermes_spoke_careermax tail -f /opt/data/logs/agent.log` |
| Change a persona or config | Edit `hermes/agents/<agent>/SOUL.md` or `hermes/agents/common/config.template.yaml`, then `bash scripts/apply_agents.sh [agent]` |
| Change model/URL/room | Edit `.env`, then `bash scripts/apply_agents.sh` (backend/CFN need `docker compose up -d` to pick up `LLM_*`) |
| List Moltbook posts | `docker compose run --rm bridge list` |
| Dry-run a post | `docker compose run --rm bridge process <post_id> --no-publish` |
| Publish a post | `docker compose run --rm bridge process <post_id>` (**posts public comments on Moltbook**) |

How agent definitions apply: `hermes/agents/` is mounted read-only at `/opt/agent-def`; on **every container boot** `hermes/agent-seed.sh` (installed as `/etc/cont-init.d/03-agent-seed`) copies SOUL.md, renders `config.yaml` and the Mycelium CLI config, and replaces the Mycelium plugin. If `SOUL.md` changed, `state.db` is archived to `/opt/data/backups/persona-change-<timestamp>/` in that agent's volume, because Hermes keeps each conversation's original system prompt. Agent state lives in named volumes `mycelium_hermes-<agent>-data`.

## 5. Reset

| Goal | Command | Effect |
| :--- | :--- | :--- |
| Restart one agent | `docker compose restart careermax` | Re-applies definition |
| Clear a stuck negotiation | Check `GET /api/rooms/<room>/sessions`; `"total"` should be 0 before retrying | New runs otherwise join the stuck session |
| Full wipe | `docker compose down -v` then redo section 3 | **Deletes all volumes**: database, rooms, agent state and history. v1's `prep_hermes_environment.sh` (repository root) did this implicitly; v2 does not use it. |

## 6. Troubleshooting

| Symptom | Cause / fix |
| :--- | :--- |
| `docker compose up` fails: `set CUSTOM_API_KEY in .env` or `HERMES_DASHBOARD_PASSWORD` | Required vars missing in `.env` |
| Agent restarts; seed log says `<VAR> is empty; set it in .env` | Set `AGENT_LLM_MODEL` / `AGENT_LLM_BASE_URL` / `MYCELIUM_ROOM` (or their fallbacks) |
| Negotiation fails "CFN not configured" | Room not linked to the CFN. `docker compose up -d --force-recreate mycelium-backend mycelium-setup`, then check `docker compose logs mycelium-setup` |
| `mycelium-setup` exited 1 / agents never start | Read `docker compose logs mycelium-setup` and `docker compose logs mycelium-backend \| grep workspace`; usually the CFN mgmt plane is unhealthy |
| LLM errors 429 | Proxy/model quota (known blocker with the Gemini free tier). Use a higher-quota model |
| LLM errors `400 No connected db` | Proxy has no DB; use a key it accepts without lookup (e.g. its master key) |
| Connection refused to `localhost:4000` from a container | Use `host.docker.internal`, not `localhost` |
| Port already in use | Change the `*_PORT` vars in `.env` (5432 and 8642 are commonly taken, hence 5433 / 8645) |
| Bridge: `Model output contained no JSON object` or `output was cut off by the token limit` | The summary model ran out of tokens; reasoning models spend most of the budget on hidden reasoning. Raise `SUMMARIZER_MAX_TOKENS` in `.env` (default 8192) and re-run with `--force` |
| Bridge: `Proxy query returned HTTP 403 ... key not allowed to access this user's info` | Only the token-cost audit is affected (spend shows 0); the run continues. Set `LITELLM_USER_ID1` / `LITELLM_USER_ID2` to the user id each key belongs to |
| Bridge: `Verification failed` / Moltbook comments show `failed` | The answer to Moltbook's math challenge was wrong; unverified content stays **hidden**, and 10 failures in a row suspend the account. `solver.py` decodes challenges with the LLM (rule-based fallback); check `[SOLVER]` lines in the bridge output and `/data/verification_log.jsonl` in the `bridge-state` volume, and add failing texts to `bridge/tests/test_solver.py` |
| Negotiation `state=failed`, backend log `agents silent for 300s, aborting session` | An agent's turn was halted mid-negotiation by Hermes' tool-loop guardrail (agent log: `reason=guardrail_halt`, `identical_call_streak_halt`). `hard_stop_after.idempotent_no_progress` in `hermes/agents/common/config.template.yaml` must stay above `NEGOTIATION_N_STEPS`; check with `docker exec -i hermes_spoke_studentlife /opt/hermes/.venv/bin/python - < hermes/tests/check_guardrail.py` |
| `docker compose up --wait` exits with `container stack-health is unhealthy` | Read `docker compose logs stack-health`: each `[FAIL]` line says what to fix |
| `apply_agents.sh` reports `room connected: NO` | Room setup failed (`docker compose logs mycelium-setup`) or backend unhealthy; check `docker compose logs mycelium-backend` |
| Agent has the wrong persona | Run `bash scripts/apply_agents.sh <agent>`; verify `SOUL.md matches repo: yes` |
| Colab orchestrator cannot reach Mycelium | Expose the backend: `docker run --rm -it cloudflare/cloudflared:latest tunnel --url http://host.docker.internal:8000`, then set the printed URL as `MYCELIUM_BASE_URL` in Colab |

## 7. What changed from v1

v2 (in `v2/`) sits next to v1 (the repository root), which is left as it was. Compared with v1:
- The whole stack (Mycelium, CFN services, the 4 Hermes agents, the bridge) is one Compose project, started with `docker compose up -d --build`. The host needs no Mycelium CLI.
- The four agents are defined in tracked files under `hermes/agents/` and applied on every boot. Their state lives in named volumes, not on the host.
- Agent LLM model, base URL and room come from `.env`, not hard-coded values. `agent-seed.sh` fails fast if any of them is empty.
- `scripts/apply_agents.sh` replaces the destructive `prep_hermes_environment.sh`.
- `LITELLM_URL` can be a base URL, `/v1` or the full endpoint.
- v2 contains only what is needed to run. The offline scoring-rubric evaluation (`scoring_rubric_eval/`) and the test/benchmark utilities (`misc_utilities/`) stay at the repository root.

Known open item: LLM quota (Gemini free tier) can still block negotiation runs.
