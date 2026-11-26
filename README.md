# IoC-Moltbook PoC: Moltbook Integration and Autonomous multi-agent topic discovery

This is a collection of prototypes to bring the IoC capabilities to the Moltbook world. We started our journey with the integration of [**Moltbook**](#https://www.moltbook.com) (a decentralized social platform) with **Mycelium** (an agent negotiation platform). It bridges these two platforms using **Hermes Agents** to execute multi-agent validation and automated consensus publishing. Then we transformed the prototype into a governed negotiation prototype by building an autonomous topic mining, scoring and governance pipeline.

---

## 📋 Table of Contents

* [0. Example Moltbook post](#0-example-moltbook-post)
* [1. Architecture Evolution & Flow](#1-architecture-evolution--flow)
  * [Phase 1: Moltbook–Mycelium Bridge](#phase-1-moltbook-mycelium-bridge)
  * [Phase 2: Autonomous Topic Mining](#phase-2-autonomous-topic-mining)
  * [System Flow Diagrams](#system-flow-diagrams)
* [2. Project Structure](#2-project-structure)
* [3. Prerequisites & Environment Variables](#3-prerequisites--environment-variables)
  * [Infrastructure Requirements](#infrastructure-requirements)
  * [Environment Variables & Colab Secrets](#environment-variables--colab-secrets)
  * [Security Guidelines](#security-guidelines)
* [4. Getting Started](#4-getting-started)
  * [Step 1: Mycelium Setup (WSL)](#step-1-mycelium-setup-wsl)
  * [Step 2: Expose Mycelium via Cloudflare Tunnel](#step-2-expose-mycelium-via-cloudflare-tunnel)
  * [Step 3: Deploy Hermes Agent Spokes](#step-3-deploy-hermes-agent-spokes)
  * [Step 4: Run Orchestrator in Google Colab](#step-4-run-orchestrator-in-google-colab)
  * [Configuration & Maintenance Reference](#configuration--maintenance-reference)
* [5. Observability, Evaluation & Benchmarking](#5-observability-evaluation--benchmarking)
  * [Token & Cost Telemetry](#token--cost-telemetry)
  * [Moltbook Verification Challenge Solver](#moltbook-verification-challenge-solver)
  * [Empirical Scoring Rubric Evaluation](#empirical-scoring-rubric-evaluation)
* [6. Key Technologies & Agent Guardrails](#6-key-technologies--agent-guardrails)
* [7. License](#7-license)

---

## 0. Example Moltbook post

This sample post has been used as input in the Phase 1 experiments.
### Post title: 
NYU no aid vs ASU scholarship
### Post content: 
Evaluate whether a student should choose NYU with no aid or Arizona State with a $25000/year scholarship.

## 1. Architecture Evolution & Flow

### Phase 1: Moltbook–Mycelium Bridge 
Phase 1 implements a hub-and-spoke multi-agent system where four isolated Hermes agents—`financeguard`, `careermax`, `studentlife`, and `riskanalyst`—are deployed inside containerized spokes managed by a Docker compose and triggered via the Google Colab orchestrator (`Moltbook_Mycelium_bridge_svc_orchestrator.ipynb`. The bridge service (`bridge_service.py`):
1. Polls specified submolts on Moltbook for user posts and mathematical verification challenges via Moltbook REST API endpoints.
2. Solves automated math challenges to post messages and create threads.
3. Coordinates real-time agent room sessions over Mycelium REST API endpoints (`v1.1.3`).
4. Summarizes room dialogues, synthesizes consensus outcomes into public-safe JSON (`consensus_outcome` + 4 direct-voice `agent_comments`) via `summariser_module.py` and posts nested multi-account threads back to the originating submolt.
For testing, refer the sources from this [commit](https://github.com/techm-2025/ioc-moltbook-poc/commit/4f347167666de8f7bb2394a2a9d48610d5a8d639).

### Phase 2: Autonomous Topic Mining
Phase 2 shifts the system from human-triggered sessions to a fully autonomous, 13-stage pipeline executed via `topic_engine.py`:
* **Stage 1 (Ingestion):** Ingests live Moltbook streams or local JSON datasets (`structured_conversation.json`).
* **Stage 2 (Topic Discovery):** Extracts candidate topics.
* **Stage 3 (Semantic Enrichment):** Maps context, facts, and participant intents.
* **Stage 4 (Actionability & Score Calculation):** Computes a 3-dimensional score.
* **Stage 5 (Ranking & Selection):** Ranks candidate topics by actionability score.
* **Stage 6 (Deduplication):** Removes redundant topics using semantic similarity thresholds.
* **Stage 7 (Multi-Agent Validation):** Dispatches candidates to virtual agents for preliminary checks.
* **Stage 8 (NegMAS Unanimous Governance Gate):** Enforces a strict unanimous voting policy across all 4 agents before topic approval.
* **Stage 9 (Candidate Intelligence Output):** Finalizes approved candidate topic metadata.
* **Stage 10 (Mission Compilation):** Formats approved topics into structured negotiation missions.
* **Stage 11 (Mycelium Negotiation Execution):** Spawns Mycelium sessions and coordinates agent discussions.
* **Stage 12 (Session Auditing):** Audits token consumption and message trace integrity.
* **Stage 13 (Summarization & Publishing):** Synthesizes consensus outcomes and posts back to Moltbook.

### System Flow Diagrams

#### Phase 1: Reactive Bridge Flow
```
+------------------+         +-----------------------+         +--------------------+
|  Moltbook Stream |  Poll   |   bridge_service.py   |  Spawn  |  Mycelium Room     |
|   (Submolts)     | ------> |   (Central Hub)       | ------> |  Session     |
+------------------+         +-----------------------+         +--------------------+
                                         |                               |
                                  Solves Challenges               4 Hermes Agents
                                         |                      (Docker Containers)
                                         v                               v
                             +-----------------------+         +--------------------+
                             | Moltbook Consensus    | <------ | Consensus Summary  |
                             | Thread Publishing     |         | Generation         |
                             +-----------------------+         +--------------------+
```

#### Phase 2: Autonomous Topic Mining (ATM Pipeline)
```
+------------------+
| Moltbook Stream  |
| / Local JSON     |
+------------------+
         |
         v
+-----------------------------------------------------------------------------------+
| Stage 1–3: Ingestion, Windowing & Topic Discovery                                 |
+-----------------------------------------------------------------------------------+
         |
         v
+-----------------------------------------------------------------------------------+
| Stage 4–6: 3D GLM Logit Scoring -> Ranking -> Semantic Deduplication              |
+-----------------------------------------------------------------------------------+
         |
         v
+-----------------------------------------------------------------------------------+
| Stage 7–8: Multi-Agent Validation & NegMAS Unanimous Governance Gate              |
+-----------------------------------------------------------------------------------+
         |
         v
+-----------------------------------------------------------------------------------+
| Stage 9–10: Mission Compilation                                                   |
+-----------------------------------------------------------------------------------+
         |
         v
+-----------------------------------------------------------------------------------+
| Stage 11: Multi-Agent Mycelium Room Session (Ports 8641-8644)                    |
+-----------------------------------------------------------------------------------+
         |
         v
+-----------------------------------------------------------------------------------+
| Stage 12–13: Token Auditing & Public-Safe Consensus Summarization                |
+-----------------------------------------------------------------------------------+
         |
         v
+-----------------------------------------------------------------------------------+
| Moltbook Target Post: Multi-Account Nested Response Threads                       |
+-----------------------------------------------------------------------------------+
```

---

## 2. Project Structure

```
ioc-moltbook-poc/
├── README.md                                                       
├── requirements.txt                                                
├── .gitignore
│
├── System flow diagrams                                                       
│   ├── Flow diagrams
│       ├── Phase 1.svg
│       ├── Phase 2.svg
|
├── Core Pipeline & Orchestration
│   ├── Moltbook_Mycelium_bridge_svc_orchestrator.ipynb             # Colab orchestrator notebook
│   ├── bridge_service.py                                           # Central bridge hub, clients, & state manager
│   ├── topic_engine.py                                             # ATM Control Plane (Pipeline Stages & scoring)
│   └── summariser_module.py                                        # Public-safe consensus summary generator
│
├── Agent Persona and behaviour instructions                                                       
│   ├── hermes_agent_persona_prompts
│       ├── careermax
|           ├── SOUL.md
│       ├── financeguard
|           ├── SOUL.md
|       ├── riskanalyst
|           ├── SOUL.md
│       ├── studentlife
|           ├── SOUL.md
|
│
├── Agent Configurations (System level and profile specific)                                                        
│   ├── hermes_agent_configs
│       ├── careermax
|           ├── config.yaml
│       ├── financeguard
|           ├── config.yaml
|       ├── riskanalyst
|           ├── config.yaml
│       ├── studentlife
|           ├── config.yaml
|       ├── config.yaml
|
├── Infrastructure & Deployment
│   ├── docker-compose.yml                                          # 4-spoke Hermes agent container definition (keep it at the root of `hermes_network/` directory)
│   └── prep_hermes_environment.sh                                  # WSL environment cleanup, patch & boot script
│
├── Experiments & Operational Benchmarks
│   ├── token_usage_calc_test.py                                    # LiteLLM token auditor test
│   └── verif_challenge_solver_benchmark.py                         # 100-run Moltbook math solver benchmark
│
└── Offline Evaluation & Synthetic Dataset Generation
    ├── Conversation_Synthesis_06Nov.ipynb                          # Synthetic conversation generator notebook
    ├── generate_synthetic_dataset.py                               # Automation engine for synthetic dataset creation
    ├── scenario_matrix.py                                          # Multi-topic scenario configuration matrix
    ├── synthetic_conversation_generator_prompt.txt                 # Prompt definitions for conversation synthesis
    ├── [OFFLINE] Evaluation_of_the_topic_actionability_scoring_rubric_06Nov.ipynb # Rubric ML evaluation notebook
    ├── dr_spearman.py                                              # Spearman rank correlation & feature pruner
    ├── sm_lassocv.py                                               # LassoCV and Binomial GLM trainer
    ├── flattened_topic_features.csv                                # Extracted 172-payload feature matrix
    ├── correlation_heatmap_cleaned.png                             # Feature independence correlation matrix plot
    └── lasso_coefficients.png                                      # LassoCV feature weight visualization plot
```

---

## 3. Prerequisites & Environment Variables

### Infrastructure Requirements
* **Operating System:** Windows Subsystem for Linux (WSL2, `Ubuntu 24.04.0+ LTS` recommended) or Linux.
* **Containers & Orchestration:** Docker Engine `v29.0+` and Docker Compose `v5.0+`.
* **Platform CLIs:** Mycelium CLI `v1.1.3` (`mycelium-cli`).
* **Tunneling:** Cloudflare Tunnel (`cloudflared version 2026.6.0`).
* **Execution Environment:** Google Colab (for running the orchestrator notebook).

### Environment Variables & Colab Secrets

#### Required Colab Secrets / Environment Keys
| Variable Name | Description | Required By |
| :--- | :--- | :--- |
| `LITELLM_API_KEY` | Primary LiteLLM API Key (CE & Summarizer) | `bridge_service.py`, `topic_engine.py` |
| `LITELLM_API_KEY2` | Secondary LiteLLM API Key (Hermes Agent Spokes) | `bridge_service.py` |
| `LITELLM_USER_ID1` | User ID associated with Primary Key | Token Telemetry |
| `LITELLM_USER_ID2` | User ID associated with Secondary Key | Token Telemetry |
| `MOLTBOOK_API_KEY` | Main Moltbook API key for bridge agent     | Orchestrator / Bridge |
| `MOLTBOOK_API_KEY_FINANCEGUARD` | API key for FinanceGuard agent account | Multi-Account Publisher |
| `MOLTBOOK_API_KEY_CAREERMAX` | API key for CareerMax agent account | DO |
| `MOLTBOOK_API_KEY_STUDENTLIFE` | API key for StudentLife agent account | DO |
| `MOLTBOOK_API_KEY_RISKANALYST` | API key for RiskAnalyst agent account | DO |

#### Runtime Configuration Parameters
| Variable Name | Default / Example | Purpose |
| :--- | :--- | :--- |
| `MOLTBOOK_BASE_URL` | `https://api.moltbook.com` | Base URL for Moltbook REST API |
| `MOLTBOOK_SUBMOLT` | `student-finance` | Target Submolt channel |
| `MYCELIUM_BASE_URL` | `https://<tunnel-id>.trycloudflare.com` | Public Cloudflare Tunnel endpoint |
| `MYCELIUM_ROOM` | `moltbook-mycelium-demo` | Active Mycelium room identifier |
| `MYCELIUM_SENDER` | `bridge_orchestrator` | Originator ID for bridge messages |
| `LITELLM_URL` | `http://<host>:4000` | LiteLLM Proxy endpoint |
| `LITELLM_MODEL` | `gpt-4o-mini` | LLM identifier |
| `USE_LOCAL_JSON` | `"True"` | Toggles demo mode using local payload |
| `TARGET_POST_ID` | `post_98765` | Direct post override for publishing |

### Security Guidelines
* `docker-compose.yml` uses dummy placeholders (`CUSTOM_API_KEY`); actual credentials must be provided at runtime via environment files or Colab secrets.

---

## 4. Getting Started

Follow these step-by-step instructions to initialize the environment and run the pipeline.

### Step 1: Mycelium setup (WSL)
Execute the following commands in your WSL terminal to install and configure Mycelium:

```bash
# Install Mycelium CLI v1.1.3
curl -fsSL https://mycelium-io.github.io/mycelium/install.sh | bash -s -- --version 1.1.3

# Initialize, verify and fix any issue - Mycelium installation
mycelium install
mycelium up
mycelium status
mycelium doctor
mycelium doctor -fix
mycelium --version

# Create and select the working negotiation room
mycelium room create <room>
mycelium room use <room>

# Launch Mycelium frontend UI
docker run -d --name mycelium-frontend -p 3000:3000 --network mycelium_default -e MYCELIUM_INTERNAL_API_URL=http://mycelium-backend:8000 ghcr.io/mycelium-io/mycelium-frontend:latest

# Check room details in JSON format
mycelium --json room ls --name "<room>"

# Check registered agents inside a room
mycelium agent ls --room "<room>"

# To monitor room events
mycelium room watch "<room>"

# To stop Mycelium
mycelium down

# To see workspace details
curl -s http://localhost:9000/api/workspaces | jq

# Check MAS id and Workspace ID of a room
curl -s "http://localhost:8000/api/rooms?name=<room>&limit=1"   | jq '.[0] | {name, mas_id, workspace_id}'

# For other CLI commands
mycelium --help
```

### Step 2: Expose Mycelium via Cloudflare tunnel
To allow the Google Colab runtime to reach your local Mycelium daemon, launch a Cloudflare tunnel targeting port `8000`:

```bash
docker run --rm -it cloudflare/cloudflared:2026.6.0 tunnel --url http://host.docker.internal:8000
```
*Copy the generated `https://<random-subdomain/tunnel-id>.trycloudflare.com` URL. You will set this as `MYCELIUM_BASE_URL` in Colab.*

### Step 3: Deploy Hermes agent spokes
Run the preparation script to reset state, build containers, inject Mycelium CLI, patch Mycelium-Hermes agent adapters, and launch the Gateway:

```bash
# Create agent profiles
1) mkdir -p hermes_network/profiles/{financeguard,careermax,studentlife,riskanalyst}
2) for agent in financeguard studentlife careermax riskanalyst; do
   cat ./profiles/$agent/config.yaml
    model:
        default: <model-id>
        provider: custom
        base_url: https://litellm.prod.outshift.ai
        api_key: sk-**********************
    done
3) chmod +x hermes_network/prep_hermes_environment.sh
4) hermes_network/prep_hermes_environment.sh
```

This script will spin up four containerized spokes bound to local ports:
* **`financeguard`**: `http://localhost:8641`
* **`careermax`**: `http://localhost:8642`
* **`studentlife`**: `http://localhost:8643`
* **`riskanalyst`**: `http://localhost:8644`

### Step 4: Run Orchestrator in Google Colab
1. Open `Moltbook_Mycelium_bridge_svc_orchestrator.ipynb` in Google Colab.
2. Upload the required Python modules to the notebook session workspace:
   * `bridge_service.py`
   * `topic_engine.py`
   * `summariser_module.py`
   * `structured_conversation.json` (if running with `USE_LOCAL_JSON="True"`)
3. Set your secrets in Colab (`LITELLM_API_KEY`, `MOLTBOOK_API_KEY`, etc.).
4. Execute all notebook cells sequentially

### Configuration & Maintenance Reference
* **Mycelium Config Files:** 
First make changes to `~/.mycelium/config.toml` 
Then check `~/.mycelium/.env` 
Finally `mycelium config apply` or `mycelium config apply -restart`
* **To change agent specific budget for Mycelium session:** `~/.mycelium/rooms/<room>/agents/<agent>.md`
* **To change any docker service level config of the hermes agent instances:** `hermes_network/docker-compose.yml`
* **Hermes Config Files:** `~/.hermes/config.yaml`, `hermes_network/profiles/<agent>/SOUL.md`, '`hermes_network/profiles/<agent>/config.yaml`' (Copy the files from the `hermes_agent_persona_prompts/` & `hermes_agent_configs`)
* **Hermes commands:** `hermes gateway status` | `hermes gateway start` | `hermes gateway stop` | `hermes gateway restart`
* **Bridge Persistent State:** `bridge_state.json` (stores post tracking and session history).
* **Cloudfare API monitoring:**
`curl -v https://api.trycloudflare.com/tunnel` |
`dig api.trycloudflare.com` |
`nslookup api.trycloudflare.com`
---

## 5. Observability, Evaluation & Benchmarking

### Token & Cost Telemetry
The platform implements dual-key LiteLLM tracking to segregate orchestrator costs from agent costs:
* **Key 1 (`LITELLM_API_KEY`):** Tracks consumption for candidate intelligence discovery and consensus summarization.
* **Key 2 (`LITELLM_API_KEY2`):** Apportioned across the 4 Hermes agent spokes.

Call metrics are queried directly from the LiteLLM proxy `/user/info` endpoint before and after room sessions to compute exact delta token usage and dollar expenditure. Test script verification:

```bash
python token_usage_calc_test.py
```

### Moltbook Verification Challenge Solver
Moltbook requires automated math challenge verification before accepting posts. `bridge_service.py` includes a solver module (`solver.py` with `solve_challenge_legacy` fallback) and an automated circuit breaker:
* **Circuit Breaker Threshold:** Triggers after **8 consecutive failures**, halting outreach to prevent account lockouts.
* **Benchmark Harness:** Standard 100-run verification test:

```bash
python verif_challenge_solver_benchmark.py
```

### Empirical Scoring Rubric Evaluation
The candidate topic actionability scoring mechanism in `topic_engine.py` was derived through rigorous offline ML evaluation over a synthetic dataset of 50 multi-topic conversations (172 extracted topic payloads).

```
+-----------------------------------------------------------------------------------+
| 50 Synthetic Multi-Topic Conversations                          |
+-----------------------------------------------------------------------------------+
                                         |
                                         v
+-----------------------------------------------------------------------------------+
| Feature Extraction & Spearman Rank Correlation Pruning (|r| >= 0.85)             |
+-----------------------------------------------------------------------------------+
                                         |
                                         v
+-----------------------------------------------------------------------------------+
| Principal Component Analysis (PCA): 3-Component Variance Floor (94.24% Variance)  |
+-----------------------------------------------------------------------------------+
                                         |
                                         v
+-----------------------------------------------------------------------------------+
| 10-Fold Cross-Validation: Bounded Binomial GLM & LassoCV Model Fitting            |
+-----------------------------------------------------------------------------------+
```

#### Key Empirical Findings:
1. **Dimensionality Reduction (PCA):** PCA demonstrated that 3 orthogonal components explain $94.24\%$ of total variance across topic features.
2. **Correlation Pruning:** Spearman rank correlation analysis dropped collinear metrics ($|r| \ge 0.85$) and zero-variance penalty parameters, identifying three core orthogonal dimensions:
   * **`conflict_strength`** ($x_1$)
   * **`supporting_evidence`** ($x_2$)
   * **`coordination_value`** ($x_3$)
3. **Cross-Validation Results:**

| Model Type | Out-of-Fold (OOF) $R^2$ | Mean Absolute Error (MAE) | Boundary Violations |
| :--- | :---: | :---: | :---: |
| **Bounded Binomial GLM** | **$0.9776$** | **$0.0209 \pm 0.0029$** | **$0 / 172$** |
| **LassoCV** | **$0.9888$** | **$0.0145 \pm 0.0160$** | **$0 / 172$** |

---

## 6. Key Technologies & Agent Guardrails

### Key Technologies
* **Agent Framework:** [Hermes Agents](https://hermes-agent.nousresearch.com/)
* **Negotiation Layer:** [Mycelium](https://github.com/mycelium-io)
* **Social Platform API:** [Moltbook Official](https://github.com/Moltbook-Official/moltbook) | [Moltbook API](https://moltbook.apidog.io/) 
* **LLM Gateway:** OutShift LiteLLM Proxy
* **Unanimous Voting Engine:** [NegMAS](https://negmas.readthedocs.io/en/latest/)
* **Modeling & Analytics:** `scikit-learn`, `statsmodels`, `pandas`, `numpy`
* **Infrastructure:** Docker Compose, WSL2, Cloudflare Tunnels, Google Colab

### Agent Lenses & Guardrails
* **`financeguard`:** Focuses on financial feasibility, budget constraints, and risk mitigation.
* **`careermax`:** Focuses on career trajectory impact, skill acquisition, and professional value.
* **`studentlife`:** Focuses on student experience, work-life balance, and wellness.
* **`riskanalyst`:** Focuses on compliance, operational risks, and contingency planning.

#### Public Sanitization Guardrails
Before any room consensus outcome is posted back to Moltbook, `summariser_module.py` runs `sanitize_public_text()` to strip prompt leakage, internal agent state identifiers, and raw metadata, producing a clean, public-safe JSON summary with distinct agent comment voices.

---

## 7. License

This repository is distributed under the Apache2.0 License. See `LICENSE` for details.