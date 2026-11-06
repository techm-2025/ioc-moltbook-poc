from __future__ import annotations
import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

import requests
import urllib3

import random
from http import HTTPStatus

from summariser_module import summarize_for_moltbook

try:
    # from solver import calculate_answer as _ext_calculate_answer
    _ext_calculate_answer = None
except Exception:
    _ext_calculate_answer = None

RETRYABLE_STATUS_CODES = {
HTTPStatus.TOO_MANY_REQUESTS,      # 429
HTTPStatus.REQUEST_TIMEOUT,        # 408
HTTPStatus.INTERNAL_SERVER_ERROR,  # 500
HTTPStatus.BAD_GATEWAY,            # 502
HTTPStatus.SERVICE_UNAVAILABLE,    # 503
HTTPStatus.GATEWAY_TIMEOUT         # 504
}

# Blended Rates (85% Input / 15% Output ratio) based on official model pricing per token:
# - Gemini 2.5 Flash : ($0.10 * 0.85 + $0.40 * 0.15) / 1,000,000 = $0.000000145 per token
# - GPT-4o-mini      : ($0.15 * 0.85 + $0.60 * 0.15) / 1,000,000 = $0.0000002175 per token
# - Claude 3.5 Sonnet: ($3.00 * 0.85 + $15.00 * 0.15) / 1,000,000 = $0.0000048 per token
MODEL_BLENDED_RATES = {
    "gemini": Decimal("0.000000145"),
    "gpt-4o-mini": Decimal("0.0000002175"),
    "sonnet": Decimal("0.0000048")
}

urllib3.disable_warnings()

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def estimate_tokens_from_spend(spend_usd: Decimal, model_family: str = "gemini") -> int:
    """Reverse-engineers estimated token consumption from a Decimal USD spend delta."""
    if spend_usd <= Decimal("0"):
        return 0
    rate = MODEL_BLENDED_RATES.get(model_family, MODEL_BLENDED_RATES["gemini"])
    return int(spend_usd / rate)

@dataclass
class BridgeConfig:
    moltbook_base_url: str = "https://www.moltbook.com/api/v1"
    moltbook_api_key: str = ""
    submolt_name: str = "secure-dev-logic"
    submolt_display_name: str = "Private Agent Logic Exchange"
    submolt_description: str = "A restricted room for back-end multi-agent synchronization."

    mycelium_base_url: str = ""
    mycelium_room: str = "moltbook-mycelium-demo"
    mycelium_sender_handle: str = "amlan"

    state_path: str = "bridge_state.json"
    poll_interval_seconds: int = 5
    negotiation_timeout_seconds: int = 1500
    midpoint_after_seconds: int = 9999

    litellm_api_key: str = ""
    litellm_user_id1: str = ""
    litellm_api_key2: str = ""
    litellm_user_id2: str = ""

    litellm_model: str = "azure/gpt-4o-mini"

    agent_handles: Tuple[str, ...] = (
        "financeguard",
        "careermax",
        "studentlife",
        "riskanalyst",
    )

    scenario_facts: str = ""
    # agent_lens_templates: Dict[str, str] = field(default_factory=lambda: {
    #     "financeguard": "FinanceGuard lens: affordability, debt, scholarship value, ROI, and financial flexibility.",
    #     "careermax":    "CareerMax lens: career outcomes, internships, alumni access, major fit, and long-term optionality.",
    #     "studentlife":  "StudentLife lens: campus fit, city experience, personal growth, and quality of life.",
    #     "riskanalyst":  "RiskAnalyst lens: downside risk, fallback plans, scholarship renewal, and financial resilience.",
    # })

    agent_lens_templates: Dict[str, str] = field(default_factory=lambda: {
        "financeguard": (
            "FinanceGuard lens: Focus EXCLUSIVELY on zero-debt and out-of-pocket costs under $20k/yr. "
            "Accept offers that meet zero debt. Do NOT evaluate career, salary, or campus culture terms."
        ),
        "careermax": (
            "CareerMax lens: Focus EXCLUSIVELY on employment outcomes and internship programs. "
            "Accept offers with 90%+ placement or guaranteed internships. "
            "Do NOT reject based on salary documentation or emergency fund amounts."
        ),
        "studentlife": (
            "StudentLife lens: Focus EXCLUSIVELY on campus community, student wellness, and lifestyle. "
            "Accept offers providing a positive campus experience. "
            "STRICT RULE: Do NOT evaluate or reject based on emergency funds, salaries, or debt. Leave those entirely to the other agents."
        ),
        "riskanalyst": (
            "RiskAnalyst lens: Focus EXCLUSIVELY on downside mitigation and scholarship renewal terms. "
            "Accept any available emergency fund option ($5,000+) as adequate risk mitigation. "
            "Support agreement once GPA terms (3.0) and debt-free status are present."
        ),
    })
    


class BridgeState:
    def __init__(self, path: str):
        self.path = Path(path)
        self.data = self._load()

    def _load(self) -> Dict[str, Any]:
        if self.path.exists():
            return json.loads(self.path.read_text())
        return {"processed_topics": {}, "submolts": {}}

    def save(self) -> None:
        self.path.write_text(json.dumps(self.data, indent=2, sort_keys=True))

    def has_seen_topic(self, post_id: str) -> bool:
        return str(post_id) in self.data.setdefault("processed_topics", {})

    def mark_topic(self, post_id: str, **updates: Any) -> None:
        entry = self.data.setdefault("processed_topics", {}).setdefault(str(post_id), {})
        entry.update(updates)
        entry["updated_at"] = utc_now()
        self.save()

    def mark_submolt(self, name: str, data: Dict[str, Any]) -> None:
        self.data.setdefault("submolts", {})[name] = data
        self.save()

    def get_topic(self, post_id: str) -> Dict[str, Any]:
        return self.data.setdefault("processed_topics", {}).get(str(post_id), {})


class MoltbookClient:
    MAX_RETRIES = 5
    BASE_DELAY = 1
    MAX_DELAY = 60
    RETRYABLE_STATUS_CODES = RETRYABLE_STATUS_CODES
    def __init__(self, api_key: str, base_url: str = "https://www.moltbook.com/api/v1", verify_ssl: bool = False):
        self.base_url = base_url.rstrip("/")
        self.verify_ssl = verify_ssl
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        self.consecutive_verify_failures = 0

    def _request(self, method: str, path: str, *, params: Optional[Dict[str, Any]] = None, json_body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = f"{self.base_url}{path}"
        for attempt in range(self.MAX_RETRIES):
            res = requests.request(
                method,
                url,
                headers=self.headers,
                params=params or {},
                json=json_body,
                verify=self.verify_ssl,
                timeout=30,
            )
            print(f"MOLTBOOK {method} {path} (Attempt {attempt+1}) - Status: {res.status_code}")

            if res.status_code < 400:
                return res.json()

            if res.status_code not in self.RETRYABLE_STATUS_CODES:
                raise RuntimeError(f"Moltbook {method} failed {res.status_code}: {res.text}")

            if attempt < self.MAX_RETRIES - 1:
                temp = min(self.MAX_DELAY, self.BASE_DELAY * (2 ** attempt))  # exp backoff
                delay = (temp / 2) + random.uniform(0, temp / 2)             # equal jitter
                print(f"Moltbook {method} rate limited/unavailable "
                      f"[Attempt {attempt+1}] Status {res.status_code}. Retrying in {delay:.2f}s...")
                time.sleep(delay)

        raise RuntimeError(
            f"Moltbook {method} failed after {self.MAX_RETRIES} retries "
            "due to rate limiting or server errors."
        )

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return self._request("GET", path, params=params)

    def _post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self._request("POST", path, json_body=payload)

    def create_submolt(self, name: str, display_name: str, description: str) -> Dict[str, Any]:
        payload = {
            "name": name,
            "display_name": display_name,
            "description": description,
        }
        return self._post("/submolts", payload)

    def get_submolt(self, name: str) -> Dict[str, Any]:
        return self._get(f"/submolts/{name}")

    def ensure_submolt(self, name: str, display_name: str, description: str) -> Dict[str, Any]:
        try:
            data = self.get_submolt(name)
            print(f"SubMolt exists: {name}")
            return data
        except Exception as get_exc:
            print(f"SubMolt lookup failed, trying create: {get_exc}")
            try:
                data = self.create_submolt(name, display_name, description)
                print(f"SubMolt created: {name}")
                return data
            except Exception as create_exc:
                # If create races with an existing SubMolt, one final get is useful.
                print(f"SubMolt create failed, retrying get: {create_exc}")
                return self.get_submolt(name)

    def solve_challenge_legacy(self, text: str) -> float:
        import re
        
        # 1. Strip ALL punctuation. Only keep letters, numbers, and spaces.
        # This prevents Moltbook's random noise symbols (/, -, *, +) from triggering the wrong math operation.
        clean_text = re.sub(r'[^a-zA-Z0-9\s]', ' ', text).lower()
        
        number_map = {
            "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
            "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
            "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
            "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
            "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40,
            "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
        }

        def extract_nums(raw_text):
            found = []
            for m in re.finditer(r'\d+', raw_text):
                found.append((m.start(), int(m.group())))

            temp = raw_text
            for word, val in sorted(number_map.items(), key=lambda x: len(x[0]), reverse=True):
                pattern = r'\b' + r'\s*'.join(re.escape(c) for c in word) + r'\b'
                for m in re.finditer(pattern, temp):
                    found.append((m.start(), val))
                    temp = temp[:m.start()] + " " * (m.end() - m.start()) + temp[m.end():]

            # Sort and merge tens+units (e.g., 20, 3 -> 23)
            sorted_found = [v for _, v in sorted(found)]
            merged = []
            i = 0
            while i < len(sorted_found):
                curr = sorted_found[i]
                if curr in [20, 30, 40, 50, 60, 70, 80, 90] and i + 1 < len(sorted_found):
                    next_val = sorted_found[i + 1]
                    if 1 <= next_val <= 9:
                        merged.append(curr + next_val)
                        i += 2
                        continue
                merged.append(curr)
                i += 1
            return merged

        nums = extract_nums(clean_text)
        if not nums:
            return 0.0

        # 2. Operator Detection (Words ONLY, no symbols)
        words = clean_text.split()
        if any(k in words for k in ['divide', 'divided', 'over', 'half', 'split']):
            return float(nums[0] / nums[1]) if len(nums) >= 2 and nums[1] != 0 else float(nums[0])
        elif any(k in words for k in ['times', 'multiply', 'multiplied', 'product', 'claws', 'each']):
            return float(nums[0] * nums[1]) if len(nums) >= 2 else float(nums[0])
        elif any(k in words for k in ['minus', 'reduces', 'decreases', 'subtracted', 'subtract', 'lost', 'loses']):
            return float(nums[0] - nums[1]) if len(nums) >= 2 else float(nums[0])
        else:
            # Default to Addition (plus, gains, total, added, combined, etc.)
            return float(sum(nums))

    def verify_content(self, verification: Dict[str, Any]) -> Dict[str, Any]:
        # CIRCUIT BREAKER: Halt before hitting the 10-failure suspension limit
        if getattr(self, "consecutive_verify_failures", 0) >= 8:
            raise RuntimeError(
                "CIRCUIT BREAKER TRIGGERED: 8 consecutive verification failures detected. "
                "Halting operations to prevent automatic Moltbook account suspension."
            )

        challenge_text = verification.get("challenge_text", "")
        v_code = verification.get("verification_code")
        
        final_answer = None

        # ATTEMPT 1: Local execution of 3rd-party solver
        if _ext_calculate_answer is not None:
            try:
                ans = _ext_calculate_answer(challenge_text)
                if ans is not None:
                    final_answer = float(ans)
                    print(f"\n[MATH VERIFIER] Primary Solver calculated: {final_answer:.2f}")
            except Exception as e:
                print(f"\n[MATH VERIFIER] Primary solver crashed locally ({e}). Initiating local fallback...")

        # ATTEMPT 2: Local fallback to Legacy Matrix Solver if primary failed
        if final_answer is None:
            final_answer = self.solve_challenge_legacy(challenge_text)
            print(f"\n[MATH VERIFIER] Legacy Solver calculated: {final_answer:.2f}")

        # ONE-SHOT API SUBMISSION
        try:
            response = self._post("/verify", {
                "verification_code": v_code,
                "answer": f"{final_answer:.2f}",
            })
            # Reset the consecutive failure counter on a successful 200 OK
            self.consecutive_verify_failures = 0
            return response
            
        except Exception as e:
            # If Moltbook returns 400 Incorrect Answer, track the failure
            if "400" in str(e) or "Incorrect answer" in str(e):
                self.consecutive_verify_failures = getattr(self, "consecutive_verify_failures", 0) + 1
                print(f"   [!] Verification failed. Consecutive rolling failures: {self.consecutive_verify_failures}/10")
            raise e

    def list_submolt_posts(self, submolt_name: str, limit: int = 20) -> List[Dict[str, Any]]:
        data = self._get(f"/submolts/{submolt_name}/feed?sort=new&limit={limit}")
        return data.get("posts", [])

    def create_post(self, submolt_name: str, title: str, content: str) -> str:
        data = self._post("/posts", {
            "submolt_name": submolt_name,
            "title": title,
            "content": content,
        })
        post = data.get("post", {})
        if post.get("verification_status") == "pending" and post.get("verification"):
            try:
                self.verify_content(post["verification"])
            except Exception as e:
                print(f"WARN: Post created but verification challenge failed: {e}")

        return str(post.get("id"))

    def comment_on_post(self, post_id: str, content: str, parent_id: Optional[str] = None) -> Dict[str, Any]:
        """Supports hierarchical child replies with robust challenge extraction."""
        payload = {"content": content}
        if parent_id:
            payload["parent_id"] = str(parent_id)

        data = self._post(f"/posts/{post_id}/comments", payload)
        # Extract verification challenge from any known response structure
        comment = data.get("comment", {}) if isinstance(data.get("comment"), dict) else {}
        verification = (
            data.get("verification") 
            or comment.get("verification") 
            or (data if "verification_code" in data else None)
        )
        status = data.get("verification_status") or comment.get("verification_status")

        if verification and "verification_code" in verification:
            # Isolate verification failures from crashing the caller
            try:
                self.verify_content(verification)
            except Exception as e:
                print(f"WARN: Comment pending but verification failed natively: {e}")
        elif status == "pending":
            print(f"WARN: Comment pending verification but missing payload structure: {data}")
        return data

class MyceliumClient:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = f"{self.base_url}{path}"
        res = requests.get(url, params=params or {}, timeout=30)
        print("MYCELIUM GET:", url, res.status_code)
        if res.status_code >= 400:
            print("ERROR:", res.text)
        res.raise_for_status()
        return res.json()

    def _post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        url = f"{self.base_url}{path}"
        res = requests.post(url, json=payload, timeout=30)
        print("MYCELIUM POST:", url, res.status_code)
        print("PAYLOAD:", payload)
        print("RESPONSE:", res.text[:1000])

        if res.status_code >= 400:
            raise RuntimeError(f"Mycelium POST failed {res.status_code}: {res.text}")
        try:
            return res.json()
        except Exception:
            return {"text": res.text}

    def get_messages(
        self,
        room_name: str,
        limit: int = 50,
        offset: int = 0,
        sender: Optional[str] = None,
        message_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"limit": limit, "offset": offset}
        if sender:
            params["sender"] = sender
        if message_type:
            params["message_type"] = message_type
        return self._get(f"/api/rooms/{room_name}/messages", params)


    def create_session(self, room_name: str, agent_handle: str = "amlan") -> Optional[Dict[str, Any]]:
        payload: Dict[str, Any] = {"agent_handle": agent_handle}
        try:
            return self._post(f"/api/rooms/{room_name}/sessions",payload)
        except Exception as exc:
            print("WARN: Mycelium session create failed. Create via CLI if needed:", exc)
            return None

    def get_session(self, coordination_session_id: str) -> Dict[str, Any]:
        return self._get(f"/api/coordination-sessions/{coordination_session_id}")

    def join_session(self, room_name: str, agent_handle: str, intent: str) -> Dict[str, Any]:
        return self._post(
            f"/api/rooms/{room_name}/sessions",
            {"agent_handle": agent_handle, "intent": intent},
        )

    def post_room_message(self, room_name: str, sender_handle: str, content: str) -> Dict[str, Any]:
        return self._post(
            f"/api/rooms/{room_name}/messages",
            {
                "sender_handle": sender_handle,
                "content": content,
                "message_type": "broadcast",  # "chat" alternative
            },
        )

    def get_room_plan(self, room_name: str) -> Optional[Dict[str, Any]]:
        try:
            return self._get(f"/api/rooms/{room_name}/plan")
        except Exception as exc:
            print("Plan not ready or endpoint unavailable:", exc)
            return None

FORBIDDEN_PUBLIC_PATTERNS = [
    r"mycelium negotiate propose [^\n]+",
    r"mycelium session join [^\n]+",
    r"mycelium room send [^\n]+",
    r"authorization[:=]\s*bearer\s+\S+",
    r"moltbook_sk_[A-Za-z0-9_\-]+",
    r"api[_\- ]?key\s*[:=]",
    r"traceback \(most recent call last\)",
]


def sanitize_public_text(text: str, max_chars: int = 2500) -> str:
    clean = text.strip()
    for pattern in FORBIDDEN_PUBLIC_PATTERNS:
        clean = re.sub(pattern, "[internal detail omitted]", clean, flags=re.I)
    clean = re.sub(r"\n{3,}", "\n\n", clean)
    if len(clean) > max_chars:
        clean = clean[:max_chars - 20].rstrip() + "\n...[truncated]"
    return clean


def build_mycelium_mission_message(topic: Dict[str, Any], cfg: BridgeConfig) -> str:
    title = topic.get("title") or topic.get("name") or "Moltbook topic"
    body = topic.get("content") or topic.get("body") or topic.get("text") or ""
    mentions = " ".join(f"@{h}" for h in cfg.agent_handles)
    return f"""{mentions}
Shared mission:
Negotiate a practical recommendation for this Moltbook SubMolt topic.

Topic:
{title}

Scenario:
{body}

You will negotiate using below-mentioned perspectives.

Agent lenses:
- financeguard: Focus ONLY on zero/low debt, out-of-pocket costs, and basic affordability.
- careermax: Focus ONLY on employment placement, internship access, and alumni networks.
- studentlife: Focus ONLY on campus fit, personal growth, and student quality of life.
- riskanalyst: Focus ONLY on scholarship renewal terms (GPA) and emergency contingency funds.

Goal:
Evaluate this topic, reach a consensus, and produce a practical recommendation.

Rules:
- STRICT ROLE ISOLATION: Vote ONLY on terms directly within your assigned domain. Never evaluate, police, or reject an offer based on another agent's criteria (e.g. studentlife must NOT block on emergency funds or salaries; careermax must NOT block on family financial ceilings).
- NO EXTERNAL ACTORS: There is NO outside university representative, admissions dean, or external document source. Do NOT roleplay emailing or waiting on outside verification. The options in the current offer are the ONLY facts that exist.
- DISCRETE SCHEMA ACCEPTANCE: Do not demand custom unlisted figures (e.g., demanding $15k instead of $5k, or $60k salaries). Accept the best-matching option available in the table.
- CONVERGENCE PRIORITY: Your primary objective is to reach a consensus agreement within 5 rounds.
- CLI Rules:
    - Do not run mycelium watch, mycelium session await, or mycelium negotiate status.
    - Do NOT attempt to run pip, uv, virtualenv, or reinstall packages if a command fails.
    - Only run the exact negotiate command from the current tick.
    - Use exact option values only.
    - If 'mycelium' command is needed, run 'mycelium' directly.
    - Whenever you use the mycelium negotiate command, you MUST include your specific --handle <your_name> and --room {cfg.mycelium_room}.
""".strip()

def kickoff_comment() -> str:
    return sanitize_public_text("""
AI negotiation started.

Four agents are reviewing this topic from finance, career value, student experience, and risk perspectives. A public-safe summary will be posted here after the group converges.
""")


class MoltbookMyceliumBridge:
    def __init__(self, cfg: BridgeConfig):
        if not isinstance(cfg, BridgeConfig):
            raise TypeError("cfg must be BridgeConfig; check for a trailing comma in make_config_from_env()")
        if not cfg.moltbook_api_key:
            raise ValueError("MOLTBOOK_API_KEY is required")
        if not cfg.mycelium_base_url:
            raise ValueError("MYCELIUM_BASE_URL is required")
        self.cfg = cfg
        self.state = BridgeState(cfg.state_path)
        self.moltbook = MoltbookClient(cfg.moltbook_api_key, cfg.moltbook_base_url)
        self.mycelium = MyceliumClient(cfg.mycelium_base_url)
        self.current_coordination_session_id: Optional[str] = None
        self.current_session_created_at: Optional[str] = None

    def ensure_submolt(self) -> Dict[str, Any]:
        data = self.moltbook.ensure_submolt(
            self.cfg.submolt_name,
            self.cfg.submolt_display_name,
            self.cfg.submolt_description,
        )
        self.state.mark_submolt(self.cfg.submolt_name, data)
        return data

    def find_new_topics(self, limit: int = 20) -> List[Dict[str, Any]]:
        self.ensure_submolt()
        posts = self.moltbook.list_submolt_posts(self.cfg.submolt_name, limit=limit)
        return [p for p in posts if p.get("id") and not self.state.has_seen_topic(str(p["id"]))]

    def process_topic_by_id(self, post_id: str):
        self.ensure_submolt()
        posts = self.moltbook.list_submolt_posts(self.cfg.submolt_name, limit=50)
        for topic in posts:
            if str(topic.get("id")) == str(post_id):
                return self.process_topic(topic)

        raise ValueError(f"No topic found with id: {post_id}")

    def trigger_mycelium(self, topic: Dict[str, Any]) -> Dict[str, Any]:
       mission = build_mycelium_mission_message(topic, self.cfg)
       self.mycelium.post_room_message(
          room_name=self.cfg.mycelium_room,
          sender_handle=self.cfg.mycelium_sender_handle,
          content=mission,
      )

       facts = self.cfg.scenario_facts.strip()
       intents = {
            handle: (lens + (f" Fixed facts: {facts}" if facts else ""))
            for handle, lens in self.cfg.agent_lens_templates.items()
        }
       
       expected = tuple(self.cfg.agent_handles)
       missing = [h for h in expected if h not in intents]
       
       if missing:
          raise ValueError(f"No intent configured for agent handles: {missing}")

       joins: List[Dict[str, Any]] = []
       for handle in expected:
          joined = self.mycelium.join_session(
              room_name=self.cfg.mycelium_room,
              agent_handle=handle,
              intent=intents[handle],
          )
          joins.append(joined)
          time.sleep(3) #Stagger to give the LLM API 3 seconds to breathe

       session_ids = {
          item.get("coordination_session_id")
          for item in joins
          if item and item.get("coordination_session_id")
      }
       if len(session_ids) != 1:
          raise RuntimeError(
              "The four agents did not join one shared coordination session: "
              f"{sorted(session_ids)}"
          )

       coordination_session_id = session_ids.pop()
       self.current_coordination_session_id = coordination_session_id
       joined_times = [item.get("joined_at") for item in joins if item.get("joined_at")]
       self.current_session_created_at = min(joined_times) if joined_times else utc_now()

       return {
          "coordination_session_id": coordination_session_id,
          "participants": joins,
      }

    def poll_for_summary(self, topic: Dict[str, Any], post_id: str) -> Dict[str, Any]:
        """Wait for the coordination session."""
        started = time.time()
        coordination_session_id = getattr(self, "current_coordination_session_id", None)
        if not coordination_session_id:
            return {
                "topic": topic,
                "room": self.cfg.mycelium_room,
                "coordination_session_id": "none",
                "session": {},
                "plan": None,
                "messages": [],
                "expected_agents": list(self.cfg.agent_handles),
                "status": "session_failed_no_session_id",
            }

        consecutive_poll_errors = 0

        while True:
            elapsed = time.time() - started
            try:
                session = self.mycelium.get_session(coordination_session_id)
                consecutive_poll_errors = 0
            except Exception as poll_exc:
                consecutive_poll_errors += 1
                print(f"⚠️ Mycelium poll error (attempt {consecutive_poll_errors}/5): {poll_exc}")
                if consecutive_poll_errors >= 5:
                    return {
                        "topic": topic,
                        "room": self.cfg.mycelium_room,
                        "coordination_session_id": coordination_session_id,
                        "session": {},
                        "plan": None,
                        "messages": [],
                        "expected_agents": list(self.cfg.agent_handles),
                        "status": "session_failed_network_unreachable",
                    }
                time.sleep(self.cfg.poll_interval_seconds)
                continue
            
            state = str(
                session.get("state")
                or session.get("status")
                or session.get("phase")
                or ""
            ).lower()

            print(
                f"Polling session {coordination_session_id}: "
                f"state={state or 'unknown'}, elapsed={int(elapsed)}s"
            )

            if state in {"complete", "completed", "consensus", "done"}:
                all_messages = self.mycelium.get_messages(
                    self.cfg.mycelium_room, limit=200
                ).get("messages", [])
                allowed_senders = {
                    *self.cfg.agent_handles,
                    "CognitiveEngine",
                    "cognitiveengine",
                }
                started_at = str(getattr(self, "current_session_created_at", "") or "")
                messages = [
                    msg for msg in all_messages
                    if str(msg.get("created_at", "")) >= started_at
                    and str(msg.get("sender_handle", "")) in allowed_senders
                ]
                plan = self.mycelium.get_room_plan(self.cfg.mycelium_room)
                return {
                    "topic": topic,
                    "room": self.cfg.mycelium_room,
                    "coordination_session_id": coordination_session_id,
                    "session": session,
                    "plan": plan,
                    "messages": messages,
                    "expected_agents": list(self.cfg.agent_handles),
                    "status": "session_complete",
                }

            if state in {"failed", "failure", "timeout", "timed_out", "broken"}:
                return {
                    "topic": topic,
                    "room": self.cfg.mycelium_room,
                    "coordination_session_id": coordination_session_id,
                    "session": session,
                    "plan": None,
                    "messages": [],
                    "expected_agents": list(self.cfg.agent_handles),
                    "status": "session_failed_no_public_summary",
                }

            if elapsed > self.cfg.negotiation_timeout_seconds:
                return {
                    "topic": topic,
                    "room": self.cfg.mycelium_room,
                    "coordination_session_id": coordination_session_id,
                    "session": session,
                    "plan": None,
                    "messages": [],
                    "expected_agents": list(self.cfg.agent_handles),
                    "status": "bridge_timeout_no_public_summary",
                }

            print("Waiting for the four-agent session to complete...")
            time.sleep(self.cfg.poll_interval_seconds)

    def process_topic(self, topic: Dict[str, Any]) -> Dict[str, Any]:
        post_id = str(topic["id"])
        self.state.mark_topic(post_id, status="picked", topic=topic)
        session = self.trigger_mycelium(topic)
        self.state.mark_topic(
            post_id, status="triggered_mycelium", mycelium_session=session
        )
        self.moltbook.comment_on_post(post_id, kickoff_comment())
        self.state.mark_topic(post_id, status="kickoff_posted")

        payload = self.poll_for_summary(topic, post_id)
        if payload.get("status") != "session_complete":
            self.state.mark_topic(
                post_id,
                status=payload.get("status"),
                final_summary=None,
                session_result=payload,
            )
            print("Session did not complete; no final Moltbook summary will be published.")
            return self.state.get_topic(post_id)

        summary = self.publish_summarized_result(post_id, payload)
        self.state.mark_topic(
            post_id,
            status="final_posted",
            final_summary=summary,
            session_result=payload,
        )
        return self.state.get_topic(post_id)

    def get_topic_by_id(self, post_id: str, limit: int = 100):
        self.ensure_submolt()
        posts = self.moltbook.list_submolt_posts(self.cfg.submolt_name, limit=limit)
        for topic in posts:
            if str(topic.get("id")) == str(post_id):
                return topic
        raise ValueError(f"No topic found with id: {post_id}")

    def fetch_proxy_usage(self, api_key: str, user_id: str) -> Dict[str, Any]:
        """
        Fetch cumulative spend and token usage from LiteLLM proxy via /user/info using Decimal precision.
        """
        if not api_key or not user_id:
            return {"total_tokens": 0, "spend": Decimal("0.0")}
        
        base_litellm_url = os.getenv("LITELLM_URL", "https://litellm.prod.outshift.ai").replace("/v1/chat/completions", "")
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        try:
            url = f"{base_litellm_url}/user/info?user_id={user_id}"
            response = requests.get(url, headers=headers, timeout=10)
            if response.status_code == 200:
                data = response.json().get("user_info", {})
                spend_val = data.get("spend", 0.0)
                return {
                    "total_tokens": data.get("total_tokens", 0),
                    "spend": Decimal(str(spend_val))
                }
            else:
                print(f"Proxy query returned HTTP {response.status_code}: {response.text[:200]}")
        except Exception as e:
            print(f"Proxy query network erro: {e}")
            
        return {"total_tokens": 0, "spend": Decimal("0.0")}

    def get_proxy_snapshot(self) -> Dict[str, Dict[str, Any]]:
        """Captures total user account spend baselines across Key 1 (CE/Summariser) and Key 2 (Hermes Cluster)."""
        key1 = self.cfg.litellm_api_key
        user_id_1 = self.cfg.litellm_user_id1

        key2 = self.cfg.litellm_api_key2
        user_id_2 = self.cfg.litellm_user_id2

        snapshot = {}
        
        # Key 1 Baseline (Mycelium CE & Summariser workload)
        snapshot["key1_cluster"] = self.fetch_proxy_usage(key1, user_id=user_id_1)

        # Key 2 Baseline (Hermes 4-Agent Cluster workload)
        snapshot["hermes_cluster"] = self.fetch_proxy_usage(key2, user_id=user_id_2)

        # Safeguard null entries
        for k, v in snapshot.items():
            if not v or "spend" not in v:
                snapshot[k] = {"total_tokens": 0, "spend": Decimal("0.0")}
            
        return snapshot

    def print_token_audit(self, baseline_snapshot: Dict[str, Dict[str, Any]]):
        """Calculates and prints micro-cent spend deltas and reverse-engineered token counts."""
        # 1. Take final snapshot
        final_snapshot = self.get_proxy_snapshot()
        
        # 2. Calculate Key 1 Delta (CE / Sonnet workload)
        k1_final = final_snapshot.get("key1_cluster", {})
        k1_base = baseline_snapshot.get("key1_cluster", {})
        key1_cost = k1_final.get("spend", Decimal("0")) - k1_base.get("spend", Decimal("0"))
        key1_tokens = k1_final.get("total_tokens", 0) - k1_base.get("total_tokens", 0)
        
        if key1_tokens <= 0 and key1_cost > Decimal("0"):
            key1_tokens = estimate_tokens_from_spend(key1_cost, model_family="sonnet")

        # 3. Calculate Key 2 Delta (Hermes Gemini workload)
        hermes_final = final_snapshot.get("hermes_cluster", {})
        hermes_base = baseline_snapshot.get("hermes_cluster", {})
        hermes_cost = hermes_final.get("spend", Decimal("0")) - hermes_base.get("spend", Decimal("0"))
        hermes_tokens = hermes_final.get("total_tokens", 0) - hermes_base.get("total_tokens", 0)
        
        if hermes_tokens <= 0 and hermes_cost > Decimal("0"):
            hermes_tokens = estimate_tokens_from_spend(hermes_cost, model_family="gemini")
            
        grand_total_tokens = key1_tokens + hermes_tokens
        grand_total_cost = key1_cost + hermes_cost

        # Apportion Hermes Cluster Usage equally among the 4 agent handles for the UI report
        agent_handles = getattr(self.cfg, "agent_handles", ["financeguard", "careermax", "studentlife", "riskanalyst"])
        num_agents = Decimal(str(len(agent_handles))) if agent_handles else Decimal("4")
        
        per_agent_cost = hermes_cost / num_agents if hermes_cost > Decimal("0") else Decimal("0")
        per_agent_tokens = hermes_tokens // int(num_agents) if hermes_tokens > 0 else 0
        
        # 4. Print Report
        print("\n" + "="*80)
        print(" 📊 SESSION RUN TOKEN & COST BREAKDOWN (DELTA TRACKING)")
        print("="*80)
        print(f" [User Key 1] Mycelium CE & Summariser : ~{key1_tokens:,} tokens | ${key1_cost:.8f}")
        print("-" * 80)
        print(f" [User Key 2] 4 Hermes Agents (gemini)  : ~{hermes_tokens:,} tokens | ${hermes_cost:.8f}")
        
        for agent in agent_handles:
            print(f"   ├─ {agent:<20}                  : ~{per_agent_tokens:,} tokens | ${per_agent_cost:.8f}")
            
        print("="*80)
        print(f" GRAND TOTAL FOR THIS ORCHESTRATION LOOP")
        print(f" TOKENS CONSUMED: ~{grand_total_tokens:,}  |  TOTAL COST: ${grand_total_cost:.8f}")
        print("="*80 + "\n")

    def rerun_topic_by_id(
        self,
        post_id: str,
        post_kickoff_comment: bool = True,
        post_final_comment: bool = True,
        force: bool = True,
    ) -> Dict[str, Any]:
        topic = self.get_topic_by_id(post_id)
        existing = self.state.get_topic(post_id)
        run_count = int(existing.get("run_count", 0)) + 1
        if self.state.has_seen_topic(post_id) and not force:
            raise ValueError(f"Topic {post_id} was already processed; use force=True")
        baseline_snapshot = self.get_proxy_snapshot()
        self.state.mark_topic(
            post_id,
            status="rerun_picked",
            rerun=True,
            run_count=run_count,
            topic=topic,
        )
        session = self.trigger_mycelium(topic)
        self.state.mark_topic(
            post_id,
            status="rerun_triggered_mycelium",
            mycelium_session=session,
            last_rerun_at=utc_now(),
            run_count=run_count,
        )

        if post_kickoff_comment:
            self.moltbook.comment_on_post(
                post_id,
                sanitize_public_text(
                    "AI negotiation started. Four agents are reviewing this topic "
                    "through finance, career, student-experience, and risk lenses. "
                    "A public-safe summary will be posted only if the session reaches consensus."
                ),
            )
            self.state.mark_topic(
                post_id, status="rerun_kickoff_posted", run_count=run_count
            )

        payload = self.poll_for_summary(topic, post_id)
        if payload.get("status") == "session_complete":
             # Embed the baseline so the publisher can calculate the delta
             payload["baseline_snapshot"] = baseline_snapshot
        if payload.get("status") != "session_complete":
            self.state.mark_topic(
                post_id,
                status=payload.get("status"),
                final_summary=None,
                session_result=payload,
                run_count=run_count,
            )
            print("Session did not complete; no final Moltbook summary was published.")
            return self.state.get_topic(post_id)

        if post_final_comment:
            summary = self.publish_summarized_result(post_id, payload)
            final_status = "rerun_final_posted"
        else:
            summary = self.summarize_consensus_for_moltbook(payload)
            final_status = "rerun_complete_not_published"

        self.state.mark_topic(
            post_id,
            status=final_status,
            final_summary=summary,
            session_result=payload,
            run_count=run_count,
        )
        return self.state.get_topic(post_id)

    def summarize_consensus_for_moltbook(self, consensus_payload):
      return summarize_for_moltbook(
          consensus_payload=consensus_payload,
          api_key=self.cfg.litellm_api_key,
          model=self.cfg.litellm_model,
          timeout=300,
      )

    def publish_summarized_result(self, post_id: str, consensus_payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Extracts isolated agent outputs and posts them as nested sub-replies.
        Includes a console-print safeguard for demo purposes.
        """
        parent_comment_id: Optional[str] = None
        # 1. Create a clean payload copy for the LLM summarizer without proxy metadata
        llm_payload = {k: v for k, v in consensus_payload.items() if k != "baseline_snapshot"}

        # Digest raw telemetry via the LiteLLM extraction block
        summary = self.summarize_consensus_for_moltbook(llm_payload)
        consensus_text = summary.get("consensus_outcome", "").strip()
        agent_comments = summary.get("agent_comments", [])

        # =========================================================
        # 🚨PRINT CONSENSUS TO COLAB CONSOLE FIRST
        # =========================================================
        print("\n" + "="*70)
        print("🏆 FINAL NEGOTIATION CONSENSUS REACHED 🏆")
        print("="*70)
        print(f"\n{consensus_text}\n")
        print("-" * 70)
        print("INDIVIDUAL AGENT PERSPECTIVES:")
        for item in agent_comments:
            print(f"\n>> {item.get('agent').upper()}:")
            print(item.get('comment'))
        print("="*70 + "\n")

        # Publish Primary Core Summary under Root Client Identity
        root_comment_content = f"### Negotiation Consensus Summary\n\n{consensus_text}"

        try:
            print("Attempting to publish primary root consensus node to Moltbook...")
            root_response = self.moltbook.comment_on_post(post_id=post_id, content=root_comment_content)
            # 3. Pull out the assigned comment ID to anchor the threaded tree
            parent_comment_id = root_response.get("comment", {}).get("id")
            if not parent_comment_id:
                print("CRITICAL: Root comment node returned no valid ID. Defaulting to flat fallback layout.")
                return summary

            # --- INDEX SETTLE WINDOW ---
            print(f"Root comment created (ID: {parent_comment_id}). Waiting 12s for Moltbook search index to propagate...")
            time.sleep(12.0)

        except Exception as e:
            # Catch the 400 Incorrect Answer error so the Colab cell finishes gracefully!
            print(f"\n[DEMO WARNING] Moltbook Root comment posting failed (Verification/CAPTCHA error): {e}")
            print("The consensus was successfully generated and printed above.")

        # Post Agent Sub-replies even if root comment posting had an issue
        agent_token_map = {
            "financeguard": os.getenv("MOLTBOOK_API_KEY_FINANCEGUARD"),
            "careermax": os.getenv("MOLTBOOK_API_KEY_CAREERMAX"),
            "studentlife": os.getenv("MOLTBOOK_API_KEY_STUDENTLIFE"),
            "riskanalyst": os.getenv("MOLTBOOK_API_KEY_RISKANALYST")
        }

        for item in reversed(agent_comments):
            raw_handle = item.get("agent", "").lower().strip().replace("_agent", "")
            comment_body = item.get("comment", "").strip()

            if not raw_handle or not comment_body:
                continue

            specific_token = agent_token_map.get(raw_handle)
            if specific_token:
                identity_client = MoltbookClient(
                    api_key=specific_token,
                    base_url=self.cfg.moltbook_base_url,
                    verify_ssl=self.moltbook.verify_ssl
                )
            else:
                identity_client = self.moltbook

            try:
                print(f"Publishing child node perspective for agent identity: [{raw_handle}]...")
                identity_client.comment_on_post(
                    post_id=post_id,
                    content=comment_body,
                    parent_id=parent_comment_id
                )
                time.sleep(6.0)
            except Exception as e:
                print(f"Failed to post independent identity reply for {raw_handle}: {e}")

        # summarizer_token_count = summary.get("token_usage", {}).get("total_tokens", 0)
        # self.print_token_audit(summarizer_tokens=summarizer_token_count)

        # Trigger token and cost audit right before returning
        baseline_snapshot = consensus_payload.get("baseline_snapshot", {})
        self.print_token_audit(baseline_snapshot=baseline_snapshot)

        return summary


def make_config_from_env() -> BridgeConfig:
    defaults = BridgeConfig()

    def env_or(name: str, fallback):
        return os.getenv(name, fallback)

    return BridgeConfig(
        moltbook_base_url=env_or("MOLTBOOK_BASE_URL", defaults.moltbook_base_url),
        moltbook_api_key=os.getenv("MOLTBOOK_API_KEY", ""),         
        submolt_name=env_or("MOLTBOOK_SUBMOLT", defaults.submolt_name),
        submolt_display_name=env_or("MOLTBOOK_SUBMOLT_DISPLAY", defaults.submolt_display_name),
        submolt_description=env_or("MOLTBOOK_SUBMOLT_DESCRIPTION", defaults.submolt_description),
        mycelium_base_url=os.getenv("MYCELIUM_BASE_URL", ""),        
        mycelium_room=env_or("MYCELIUM_ROOM", defaults.mycelium_room),
        mycelium_sender_handle=env_or("MYCELIUM_SENDER", defaults.mycelium_sender_handle),
        state_path=env_or("BRIDGE_STATE_PATH", defaults.state_path),
        litellm_api_key=os.getenv("LITELLM_API_KEY", ""),
        litellm_user_id1=os.getenv("LITELLM_USER_ID1", ""),
        litellm_api_key2=os.getenv("LITELLM_API_KEY2", ""),
        litellm_user_id2=os.getenv("LITELLM_USER_ID2", ""),         
        litellm_model=env_or("LITELLM_MODEL", defaults.litellm_model),
        scenario_facts=env_or("SCENARIO_FACTS", defaults.scenario_facts),
    )