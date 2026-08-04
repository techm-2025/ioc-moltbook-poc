# ioc-moltbook-poc

## File details
| File | Role |
|------|------|
| `bridge_service.py` | All logic: config, Moltbook/Mycelium clients, state, orchestration. |
| `summariser_module.py` | Consensus summarization logic. |
| `Moltbook_Mycelium_bridge_svc_orchestrator` `cell3` | Dev-loop reload helper. |
| `Moltbook_Mycelium_bridge_svc_orchestrator` `cell4` | Orchestration only - Loads secrets/config and runs one topic. |