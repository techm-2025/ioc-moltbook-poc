"""
Automated synthetic conversation generator
"""

import os
import sys
import json
import time
import re
from datetime import datetime
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Any, Tuple
import requests

from scenario_matrix import get_scenarios

# Configuration & Constants
LITELLM_URL = os.getenv(
    "LITELLM_URL", 
    "https://litellm.prod.outshift.ai/v1/chat/completions"
)
LITELLM_API_KEY = os.getenv("LITELLM_API_KEY", "sk-****")
MODEL_NAME = os.getenv("MODEL_NAME", "azure/gpt-5.2")

SYSTEM_PROMPT_PATH = Path("synthetic_conversation_generator_prompt.txt")
OUTPUT_DIR = Path("generated_conversations")
MAX_WORKERS = 4
MAX_RETRIES = 3
REQUEST_TIMEOUT_SECONDS = 120

VALID_PERSONAS = {
    "financeguard_agent",
    "careermax_agent",
    "studentlife_agent",
    "riskanalyst_agent"
}


def load_system_prompt() -> str:
    if not SYSTEM_PROMPT_PATH.exists():
        raise FileNotFoundError(
            f"Could not locate {SYSTEM_PROMPT_PATH}. Please ensure system_prompt.txt is in the directory."
        )
    with open(SYSTEM_PROMPT_PATH, "r", encoding="utf-8") as f:
        return f.read().strip()


def validate_conversation(events: List[Dict[str, Any]], expected_post_id: str) -> Tuple[bool, str]:
    """
    Validates array count, root post schema, parent-lineage tree integrity,
    persona boundaries, and monotonic timestamp progression.
    """
    if not isinstance(events, list):
        return False, "Payload is not a JSON list."

    # 1. Exact 31 events check (1 root post + 30 comments)
    if len(events) != 31:
        return False, f"Expected exactly 31 items, received {len(events)}."

    # 2. Root Post Validation
    root = events[0]
    if root.get("id") != expected_post_id:
        return False, f"Root ID must be '{expected_post_id}', found '{root.get('id')}'."
    if root.get("author", {}).get("name") != "mbagent1":
        return False, f"Root post author must be 'mbagent1', found '{root.get('author', {}).get('name')}'."
    if not root.get("title") or not root.get("content"):
        return False, "Root post missing title or content."

    # 3. Comment Validation & Parentage Lineage Graph
    seen_ids = {expected_post_id}
    prev_time = None

    try:
        prev_time = datetime.fromisoformat(root["created_at"].replace("Z", "+00:00"))
    except (KeyError, ValueError):
        return False, "Invalid ISO-8601 timestamp in root post."

    for idx, comment in enumerate(events[1:], start=1):
        c_id = comment.get("id")
        p_id = comment.get("parent_id")
        post_ref = comment.get("post_id")
        author_name = comment.get("author", {}).get("name")
        created_at_str = comment.get("created_at")

        if not c_id:
            return False, f"Item index {idx} missing 'id'."
        if post_ref != expected_post_id:
            return False, f"Item {c_id} has invalid post_id '{post_ref}'. Must be '{expected_post_id}'."
        if p_id not in seen_ids:
            return False, f"Item {c_id} has parent_id '{p_id}' which does not link to any prior message."
        if author_name not in VALID_PERSONAS:
            return False, f"Item {c_id} has invalid or unauthorized author '{author_name}'."
        if not comment.get("content"):
            return False, f"Item {c_id} is missing content."

        # Chronological monotonicity check
        try:
            curr_time = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return False, f"Item {c_id} has malformed created_at timestamp."

        if curr_time <= prev_time:
            return False, f"Item {c_id} timestamp ({curr_time}) is not strictly greater than previous ({prev_time})."

        seen_ids.add(c_id)
        prev_time = curr_time

    return True, "Valid"


def generate_single_conversation(
    session: requests.Session,
    system_prompt: str,
    scenario: Dict[str, Any]
) -> List[Dict[str, Any]]:
    """
    Executes Cisco Outshift LiteLLM proxy Call via requests with retry and validation logic to guarantee schema compliance.
    """
    run_id = scenario["run_id"]
    post_id = scenario["post_id"]

    user_prompt = f"""Generate Conversation #{run_id} conforming to the specified schema:
- Root Post ID: {post_id}
- Topic 1 (Primary Existential Dilemma): {scenario['t1']}
- Topic 2 (Secondary Profile Fit): {scenario['t2']}
- Topic 3 (Tertiary Financing/Insurance): {scenario['t3']}

Chunking & Lineage Requirements:
- Use contiguous temporal blocks of 4-6 comments per topic.
- Format all 31 discrete events in sequential timestamp order on date 2026-10-07.
- Event 0 is the root post ({post_id}).
- Events 1 to 30 are comments (m01 to m30).
- Explicitly thread tokens: "GPA", "debt", "internships", "wellness", and "scholarships" across all three topics.

Return ONLY the raw JSON array of 31 items."""

    headers = {
        "Authorization": f"Bearer {LITELLM_API_KEY}",
        "Content-Type": "application/json",
    }

    payload = {
        "model": MODEL_NAME,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        "temperature": 0.7,
        "max_tokens": 4096  # Ample token headroom for 31 full message objects
    }

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = session.post(
                LITELLM_URL,
                headers=headers,
                json=payload,
                timeout=REQUEST_TIMEOUT_SECONDS
            )

            if response.status_code != 200:
                print(f"[Warning] Run {run_id} HTTP {response.status_code}: {response.text}. Retrying...")
                time.sleep(2 * attempt)
                continue

            response_data = response.json()
            raw_text = response_data["choices"][0]["message"]["content"].strip()

            # Strip markdown code fencing if returned
            if raw_text.startswith("```"):
                raw_text = re.sub(r"^```(?:json)?\n", "", raw_text)
                raw_text = re.sub(r"\n```$", "", raw_text)

            data = json.loads(raw_text)
            is_valid, reason = validate_conversation(data, post_id)

            if is_valid:
                return data
            else:
                print(f"[Warning] Run {run_id} schema check failed on attempt {attempt}: {reason}. Retrying...")

        except requests.exceptions.RequestException as req_err:
            print(f"[Warning] Run {run_id} Network error on attempt {attempt}: {req_err}. Retrying...")
        except json.JSONDecodeError as json_err:
            print(f"[Warning] Run {run_id} JSON parse error on attempt {attempt}: {json_err}. Retrying...")
        except Exception as e:
            print(f"[Warning] Run {run_id} Unexpected error on attempt {attempt}: {e}. Retrying...")

        time.sleep(2 * attempt)

    raise RuntimeError(f"Failed to generate valid conversation for Run {run_id} after {MAX_RETRIES} attempts.")


def main():
    if not LITELLM_API_KEY or LITELLM_API_KEY == "sk-****":
        print(
            "Error: Please set your LITELLM_API_KEY environment variable or update the script constant.",
            file=sys.stderr
        )
        sys.exit(1)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    system_prompt = load_system_prompt()
    scenarios = get_scenarios(50)

    print(f"Connecting to Outshift LiteLLM proxy: {LITELLM_URL}")
    print(f"Target Model: {MODEL_NAME}")
    print(f"Starting generation of 50 conversations...")

    def worker_task(scen: Dict[str, Any]):
        out_file = OUTPUT_DIR / f"conversation_{scen['run_id']:02d}.json"
        if out_file.exists():
            print(f"Skipping {out_file.name} (already exists).")
            return scen["run_id"], True

        # Each worker thread uses its own session to pool connections cleanly
        with requests.Session() as thread_session:
            convo = generate_single_conversation(thread_session, system_prompt, scen)
            with open(out_file, "w", encoding="utf-8") as f:
                json.dump(convo, f, indent=2)
            print(f"Successfully generated and saved: {out_file.name}")
            return scen["run_id"], True

    success_count = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(worker_task, sc): sc for sc in scenarios}
        for future in as_completed(futures):
            try:
                run_id, success = future.result()
                if success:
                    success_count += 1
            except Exception as exc:
                print(f"Generation task failed: {exc}", file=sys.stderr)

    print(f"\nCompleted: {success_count}/50 conversations generated and verified in '{OUTPUT_DIR.resolve()}'.")


if __name__ == "__main__":
    main()