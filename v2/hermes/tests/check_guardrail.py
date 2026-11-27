"""Check inside a Hermes agent container: with the agent's live config.yaml, does Hermes' tool-loop
guardrail halt an agent that accepts N negotiation rounds in a row with the identical command?

    docker exec -i hermes_spoke_studentlife /opt/hermes/.venv/bin/python - < hermes/tests/check_guardrail.py

A negotiation runs up to NEGOTIATION_N_STEPS (20) rounds; rounds that arrive while the agent's turn
is still running are injected into that same turn, so every round's `mycelium negotiate respond
accept ...` counts as one more identical call. Exit 0 = no halt within ROUNDS calls."""
import sys

sys.path.insert(0, "/opt/hermes")
from ruamel.yaml import YAML  # noqa: E402  (Hermes ships ruamel, not PyYAML)
from agent.tool_guardrails import ToolCallGuardrailConfig, ToolCallGuardrailController  # noqa: E402

ROUNDS = 20
cfg = YAML(typ="safe").load(open("/opt/data/config.yaml"))
conf = ToolCallGuardrailConfig.from_mapping(cfg.get("tool_loop_guardrails"), platform="mycelium-room")
ctl = ToolCallGuardrailController(conf)
ctl.reset_for_turn()
args = {"command": "mycelium negotiate respond accept --room r:session:x --handle studentlife"}
out = '{"output": "studentlife: {\\"action\\": \\"accept\\"}\\n  Response submitted.", "exit_code": 0, "error": null}'
print(f"hard_stop_enabled={conf.hard_stop_enabled} no_progress_block_after={conf.no_progress_block_after}")
for i in range(1, ROUNDS + 1):
    before = ctl.before_call("terminal", args)
    after = ctl.after_call("terminal", args, out, failed=False)
    ctl.observe_call("terminal", args, out, tool_call_id=f"call_{i}")  # identical-call streak detector
    for d in (before, after, ctl.halt_decision):
        if d is not None and d.action in ("block", "halt"):
            print(f"FAIL: identical accept #{i} -> {d.action} ({d.code})")
            sys.exit(1)
print(f"PASS: {ROUNDS} identical accepts in one turn without a halt")
