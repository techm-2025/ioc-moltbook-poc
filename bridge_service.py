from __future__ import annotations
import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
import urllib3

import random
from http import HTTPStatus

from summariser_module import summarize_for_moltbook

RETRYABLE_STATUS_CODES = {
HTTPStatus.TOO_MANY_REQUESTS,      # 429
HTTPStatus.REQUEST_TIMEOUT,        # 408
HTTPStatus.INTERNAL_SERVER_ERROR,  # 500
HTTPStatus.BAD_GATEWAY,            # 502
HTTPStatus.SERVICE_UNAVAILABLE,    # 503
HTTPStatus.GATEWAY_TIMEOUT         # 504
}

urllib3.disable_warnings()

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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
    litellm_model: str = "azure/gpt-4o-mini"

    agent_handles: Tuple[str, ...] = (
        "financeguard",
        "careermax",
        "studentlife",
        "riskanalyst",
    )

    scenario_facts: str = ""
    agent_lens_templates: Dict[str, str] = field(default_factory=lambda: {
        "financeguard": "FinanceGuard lens: affordability, debt, scholarship value, ROI, and financial flexibility.",
        "careermax":    "CareerMax lens: career outcomes, internships, alumni access, major fit, and long-term optionality.",
        "studentlife":  "StudentLife lens: campus fit, city experience, personal growth, and quality of life.",
        "riskanalyst":  "RiskAnalyst lens: downside risk, fallback plans, scholarship renewal, and financial resilience.",
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

    def __init__(self, api_key: str, base_url: str = "https://www.moltbook.com/api/v1", verify_ssl: bool = False):
        self.base_url = base_url.rstrip("/")
        self.verify_ssl = verify_ssl
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

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

    def solve_challenge(self, text: str) -> float:
        import re
        # 1. Clean noise but preserve letters for operator keywords and math symbols
        clean_text = re.sub(r'[^a-zA-Z0-9*+\-/\s]', '', text).lower()
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
            # Extract raw digits
            for m in re.finditer(r'\d+', raw_text):
                found.append((m.start(), int(m.group())))

            # Fragment-resilient word matching
            temp = raw_text
            for word, val in sorted(number_map.items(), key=lambda x: len(x[0]), reverse=True):
                # Matches characters with potential whitespace noise between them
                pattern = r'\b' + r'\s*'.join(re.escape(c) for c in word) + r'\b'
                for m in re.finditer(pattern, temp):
                    found.append((m.start(), val))
                    # Blank out matched area to prevent double counting
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

        # Upgraded Operator Detection Matrix
        if any(k in clean_text for k in ['/', 'divide', 'divided by', 'over', 'half', 'split']):
            return float(nums[0] / nums[1]) if len(nums) >= 2 and nums[1] != 0 else float(nums[0])
        elif any(k in clean_text for k in ['*', 'times', 'multiply', 'multiplied', 'claws', 'each']):
            return float(nums[0] * nums[1]) if len(nums) >= 2 else float(nums[0])
        elif any(k in clean_text for k in ['-', 'minus', 'reduces', 'decreases', 'subtracted', 'subtract']):
            return float(nums[0] - nums[1]) if len(nums) >= 2 else float(nums[0])
        else:
            # Default to Addition
            return float(sum(nums))

    def verify_content(self, verification: Dict[str, Any]) -> Dict[str, Any]:
        challenge_text = verification.get("challenge_text", "")
        answer = self.solve_challenge(challenge_text)
        print(f"\n[MATH VERIFIER ACTIVATED] Challenge: '{challenge_text}' | Answer: {answer:.2f}\n")
        return self._post("/verify", {
            "verification_code": verification.get("verification_code"),
            "answer": f"{answer:.2f}",
        })

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
            self.verify_content(post["verification"])
        return str(post.get("id"))

    def comment_on_post(self, post_id: str, content: str, parent_id: Optional[str] = None) -> Dict[str, Any]:
        """Supports hierarchical child replies by passing an optional parent_id node parameter."""
        payload = {"content": content}
        if parent_id:
            payload["parent_id"] = parent_id

        data = self._post(f"/posts/{post_id}/comments", payload)
        comment = data.get("comment", {})
        if comment.get("verification_status") == "pending" and comment.get("verification"):
            self.verify_content(comment["verification"])
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
- financeguard: affordability, debt load, scholarship value, ROI, financial downside.
- careermax: career outcomes, alumni network, job access, internships, major fit.
- studentlife: experience, city life, campus culture, personal growth.
- riskanalyst: uncertainty, fallback plans, worst-case scenarios, family constraints.

Goal:
Evaluate this topic, reach a consensus, and produce a practical recommendation.

Rules:
- Do not run mycelium watch.
- Do not run mycelium session await.
- Do not run mycelium negotiate status.
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
        """Wait for the exact coordination session; never infer completion from room traffic."""
        started = time.time()
        coordination_session_id = getattr(self, "current_coordination_session_id", None)
        if not coordination_session_id:
            raise ValueError("No current coordination_session_id was recorded")

        while True:
            elapsed = time.time() - started
            session = self.mycelium.get_session(coordination_session_id)
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
        # 1. Digest raw telemetry via the LiteLLM extraction block
        summary = self.summarize_consensus_for_moltbook(consensus_payload)
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

        # 2. Publish Primary Core Summary under Root Client Identity
        root_comment_content = f"### Negotiation Consensus Summary\n\n{consensus_text}"
        print("Attempting to publish primary root consensus node to Moltbook...")

        try:
            root_response = self.moltbook.comment_on_post(post_id=post_id, content=root_comment_content)

            # 3. Pull out the assigned comment ID to anchor the threaded tree
            parent_comment_id = root_response.get("comment", {}).get("id")
            if not parent_comment_id:
                print("CRITICAL: Root comment node returned no valid ID. Defaulting to flat fallback layout.")
                return summary

            # --- INDEX SETTLE WINDOW ---
            print(f"Root comment created (ID: {parent_comment_id}). Waiting 12s for Moltbook search index to propagate...")
            time.sleep(12.0)

            # 4. Initialize separate tokens from offline provisioning block
            agent_token_map = {
                "financeguard": os.getenv("MOLTBOOK_API_KEY_FINANCEGUARD"),
                "careermax": os.getenv("MOLTBOOK_API_KEY_CAREERMAX"),
                "studentlife": os.getenv("MOLTBOOK_API_KEY_STUDENTLIFE"),
                "riskanalyst": os.getenv("MOLTBOOK_API_KEY_RISKANALYST")
            }

            # 5. Cycle through parsed perspectives and post child replies using isolated credentials
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

                formatted_reply = comment_body

                try:
                    print(f"Publishing child node reply for agent identity: [{raw_handle}]...")
                    identity_client.comment_on_post(
                        post_id=post_id,
                        content=formatted_reply,
                        parent_id=parent_comment_id
                    )
                    time.sleep(6.0)
                except Exception as e:
                    print(f"Failed to post independent identity reply for {raw_handle}: {e}")

        except Exception as e:
            # Catch the 400 Incorrect Answer error so the Colab cell finishes gracefully!
            print(f"\n[DEMO WARNING] Moltbook posting failed (Verification/CAPTCHA error): {e}")
            print("The consensus was successfully generated and printed above.")

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
        litellm_model=env_or("LITELLM_MODEL", defaults.litellm_model),
        scenario_facts=env_or("SCENARIO_FACTS", defaults.scenario_facts),
    )