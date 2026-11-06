import json
import os
import re
import requests
import time
import random
from http import HTTPStatus
from typing import Any, Dict, Optional


LITELLM_URL = os.getenv("LITELLM_URL", "https://litellm.prod.outshift.ai/v1/chat/completions")
LITELLM_API_KEY = os.getenv("LITELLM_API_KEY", "")
MODEL_NAME = os.getenv("LITELLM_MODEL", "azure/gpt-4o-mini")

EXPECTED_AGENTS = {"financeguard", "careermax", "studentlife", "riskanalyst"}

RETRYABLE_STATUS_CODES = {
        HTTPStatus.TOO_MANY_REQUESTS,      # 429
        HTTPStatus.REQUEST_TIMEOUT,        # 408
        HTTPStatus.INTERNAL_SERVER_ERROR,  # 500
        HTTPStatus.BAD_GATEWAY,            # 502
        HTTPStatus.SERVICE_UNAVAILABLE,    # 503
        HTTPStatus.GATEWAY_TIMEOUT         # 504
    }

MAX_RETRIES = 5
BASE_DELAY = 1
MAX_DELAY = 60

SUMMARIZER_PROMPT = """
You are the Mycelium Consensus Summarizer for Moltbook submolt, an agent that converts private Mycelium multi-agent negotiation payloads into public-safe, machine-parseable outcomes for the Moltbook social layer.

Your job: retain the full decision context while stripping all internal operational machinery. 

CRITICAL ARCHITECTURE CONTEXT:
You are generating data that will be published as a nested, multi-account thread on a social platform. 
- The `consensus_outcome` will be posted as the "Root Comment" by a central system orchestrator.
- The `agent_comments` will be published as independent "Child Replies" directly from the individual social media accounts of those specific agents.

HARD CONSTRAINTS:
1. Never expose internals: Remove room/session IDs, task-state markers ([x]), escape sequences, raw JSON, and ...[truncated] markers.
2. Direct Persona Voice: Because each agent's comment will be posted under their own account, write the `comment` value directly from their perspective (e.g., "I prioritized...", "My analysis shows..."). Do NOT use third-person reporting prefixes.
3. Zero overlap: Assign each agent a distinct slice of the outcome based on their analytical lane. 

OUTPUT — RETURN ONLY THIS JSON OBJECT (no prose, no markdown, no code fences):
{
  "consensus_outcome": "string — 2–4 sentences outlining the final agreed conclusion, key conditions, and what would change the outcome. Write this objectively as the system orchestrator.",
  "agent_comments": [
    // YOU MUST GENERATE EXACTLY 4 OBJECTS, ONE FOR EACH AGENT: financeguard, careermax, studentlife, riskanalyst.
    {
      "agent": "string — the exact lowercase agent handle (e.g., financeguard)",
      "comment": "string — 1–2 sentences. Written in the direct voice of the agent, focusing ONLY on their specific analytical lane."
    }
  ]
}

EDGE CASES:
- Silent agent: If an agent contributed no distinct position to the payload, YOU MUST STILL INCLUDE THEM in the array. Set their comment strictly to: "I deferred to the consensus on this topic as my primary metrics were not triggered." Do not drop them from the list.
- Ambiguous consensus: State plainly in the consensus_outcome that the session did not converge.
""".strip()


def build_user_prompt(consensus_payload: Any) -> str:
    if isinstance(consensus_payload, (dict, list)):
        payload_text = json.dumps(consensus_payload, default=str, ensure_ascii=False, indent=2)
    else:
        payload_text = str(consensus_payload)

    return f"""
CONSENSUS_PAYLOAD:
{payload_text}

Return valid JSON only.
""".strip()


def _parse_model_json(content: str) -> Dict[str, Any]:
    """Parse strict JSON, tolerating an accidental Markdown code fence."""
    text = (content or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"Model output was not valid JSON even after brace extraction: {exc}"
                ) from exc
        raise ValueError("Model output contained no JSON object")


def summarize_for_moltbook(
    consensus_payload: Any,
    api_key: Optional[str] = None, 
    model: Optional[str] = None,
    timeout: int = 45,
) -> Dict[str, Any]:

    api_key = api_key or os.getenv("LITELLM_API_KEY", "")
    model = model or os.getenv("LITELLM_MODEL", MODEL_NAME)
    
    if not api_key:
        raise ValueError("LITELLM_API_KEY is empty")

    session = requests.Session()
    session.trust_env = False

    base_body = {
        "model": model,
        "messages": [
            {"role": "system", "content": SUMMARIZER_PROMPT},
            {"role": "user", "content": build_user_prompt(consensus_payload)},
        ],
        "max_completion_tokens": 1000,
        "user": "mycelium-consensus-summarizer",
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    attempts = [
        {**base_body, "response_format": {"type": "json_object"}},
        base_body,
    ]

    response = None
    for attempt_number, request_body in enumerate(attempts, start=1):
        
        # Inner loop to handle Rate Limits and transient server errors
        for retry_attempt in range(MAX_RETRIES):
            response = session.post(
                LITELLM_URL,
                headers=headers,
                json=request_body,
                timeout=timeout,
            )
            
            if response.status_code < 400:
                break  # Success, exit the rate limit retry loop
                
            if response.status_code not in RETRYABLE_STATUS_CODES:
                break  # Not a retryable error, exit inner loop to evaluate fallback attempt
                
            if retry_attempt < MAX_RETRIES - 1:
                temp_delay = min(MAX_DELAY, BASE_DELAY * (2 ** retry_attempt))
                delay = (temp_delay / 2) + random.uniform(0, temp_delay / 2)
                print(f"LiteLLM rate limited/unavailable (Status {response.status_code}). Sleeping {delay:.2f}s...")
                time.sleep(delay)
            else:
                print(f"LiteLLM max retries reached for attempt {attempt_number}.")

        if response and response.status_code < 400:
            break  # Overall success, exit the outer fallback attempt loop

        print(
            f"LiteLLM summarizer attempt {attempt_number} failed: "
            f"status={response.status_code if response else 'N/A'}, model={model}, "
            f"response={response.text[:3000] if response else 'N/A'}"
        )

        # Proceed to fallback (without JSON object formatting) if it's a 400 or 422
        if response and response.status_code not in {400, 422}:
            break

    if response is None or response.status_code >= 400:
        status = response.status_code if response is not None else "no-response"
        body = response.text[:3000] if response is not None else ""
        raise RuntimeError(
            f"LiteLLM summarizer failed: status={status}, model={model}, response={body}"
        )

    data = response.json()
    choices = data.get("choices") or []
    if not choices:
        raise ValueError(f"LiteLLM response has no choices: {json.dumps(data)[:3000]}")

    content = choices[0].get("message", {}).get("content")
    if not content:
        raise ValueError(f"LiteLLM response content is empty: {json.dumps(data)[:3000]}")

    result = _parse_model_json(content)

    usage_data = data.get("usage", {})
    result["token_usage"] = {
        "prompt_tokens": usage_data.get("prompt_tokens", 0),
        "completion_tokens": usage_data.get("completion_tokens", 0),
        "total_tokens": usage_data.get("total_tokens", 0),
        "model": model
    }

    if "consensus_outcome" not in result:
        raise ValueError("Missing 'consensus_outcome' in model output")
    if "agent_comments" not in result:
        raise ValueError("Missing 'agent_comments' in model output")
    if not isinstance(result["agent_comments"], list):
        raise ValueError("'agent_comments' must be a list")

    comments = result["agent_comments"]
    handles = {str(c.get("agent", "")).lower().strip() for c in comments}
    if handles != EXPECTED_AGENTS:
        raise ValueError(
            f"Agent handle mismatch. expected={sorted(EXPECTED_AGENTS)} got={sorted(handles)}"
        )
    for c in comments:
        if not str(c.get("comment", "")).strip():
            raise ValueError(f"Empty comment for agent: {c.get('agent')!r}")

    
    return result

def process_request(input_body: Dict[str, Any]) -> Dict[str, Any]:
    if "consensus_payload" not in input_body:
        raise ValueError("Input must contain 'consensus_payload'")

    return summarize_for_moltbook(
        consensus_payload=input_body["consensus_payload"]
    )