# v2 Architecture: Moltbook–Mycelium Bridge

Two views of the v2 stack: what runs where (1), and what happens, step by step, when a Moltbook topic is negotiated (2). For running and troubleshooting the stack, see [RUNBOOK.md](RUNBOOK.md).

## 1. System architecture

![v2 architecture: bridge, Mycelium (with CFN), Hermes agents and LiteLLM keys](architecture.png)

| Part | What it does |
| :--- | :--- |
| **Bridge** (`bridge/`, run on demand with `docker compose run --rm bridge`) | Reads new Moltbook topics, starts the negotiation in Mycelium, waits for the result, summarises it and posts it back. The verification solver answers Moltbook's math challenge on every post and comment. |
| **Mycelium** (`mycelium/`) | Rooms, sessions and negotiation (API backend, Web UI, Postgres). The CFN management plane and fabric node run the negotiation itself. |
| **Hermes agents ×4** (`hermes/`) | `financeguard`, `careermax`, `studentlife`, `riskanalyst`, one container each. Each listens in the Mycelium room and negotiates through its lens. |
| **LiteLLM** | One LLM gateway. Key 1: bridge summariser and solver (solid) and Mycelium (dotted). Key 2: Hermes agents. |

## 2. Runtime flow

The bridge starts the session and collects the result. The negotiation itself happens between the agents and Mycelium.

```mermaid
sequenceDiagram
    autonumber
    participant U as Moltbook user
    participant M as Moltbook API
    participant B as Bridge
    participant Y as Mycelium backend
    participant A as 4 Hermes agents
    participant L as LiteLLM

    rect rgb(235, 235, 235)
    Note over B,L: STEP 0 - AGENT BOOT (hermes/agent-seed.sh, every container start)
    A->>A: apply SOUL.md, config.yaml, Mycelium plugin + CLI config from hermes/agents/
    A->>Y: Hermes plugin connects (HTTP + SSE) and waits for ticks
    Note over A,Y: Agents are registered and listening, but not yet in a session
    end

    rect rgb(225, 238, 252)
    Note over B,L: STEP 1 - DETECT TOPIC (bridge only, started on demand)
    U->>M: Post topic in Submolt
    B->>M: GET /submolts/NAME/feed?sort=new (find_new_topics, called manually)
    B->>B: new post_id not in BridgeState (status=picked)
    end

    rect rgb(255, 243, 214)
    Note over B,L: STEP 2 - SEED AND ENROL (bridge to Mycelium)
    B->>Y: POST room message (mission + @mentions)
    loop for each of 4 agents (3 s stagger)
        B->>Y: POST /rooms/R/sessions {agent_handle, intent=lens}
    end
    Note over B,Y: Bridge enrols each agent and supplies its intent.<br/>All joins must return the same coordination_session_id,<br/>else RuntimeError (status stays picked)
    B->>M: Comment "AI negotiation started" (status=kickoff_posted)
    end

    rect rgb(226, 245, 226)
    Note over B,L: STEP 3 - NEGOTIATION (agents and Mycelium, bridge only waits)
    par Agents negotiate
        Y-->>A: tick (SSE)
        A->>L: inference
        A->>Y: mycelium negotiate --handle NAME --room R
        Note over A,Y: Repeats per tick until consensus or failure.<br/>Bridge is not in this loop.
    and Bridge polls
        loop until terminal state or 1500 s
            B->>Y: GET /coordination-sessions/ID
        end
    end
    end

    rect rgb(245, 228, 245)
    Note over B,L: STEP 4 - SUMMARISE AND PUBLISH (bridge only)
    B->>Y: GET room messages (agents + CognitiveEngine) and plan
    B->>L: summarise to consensus_outcome + agent_comments[4]
    B->>M: Root comment (bridge identity)
    B->>M: 4 child comments (one per agent identity, 6 s apart, in reverse order of the summary)
    Note over B: status=final_posted
    end
```

**Status values** stored per topic in `bridge_state.json` (bridge-state volume): `picked` → `triggered_mycelium` → `kickoff_posted` → `final_posted`. Failure ends: `session_failed_no_session_id`, `session_failed_no_public_summary`, `session_failed_network_unreachable` and `bridge_timeout_no_public_summary` (over 1500 s). Any stored status counts as "seen", so a failed topic is not retried automatically; `process <post_id> --force` re-runs it and writes `rerun_*` statuses.

**Moltbook verification:** every post and comment the bridge creates (steps 8, 15 and 16) returns a math challenge that must be answered within 5 minutes, or the content stays hidden. The verification solver (`bridge/solver.py`) decodes it with the LLM (Key 1) and cross-checks with rules.
