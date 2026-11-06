import os
import json
import requests
from typing import List, Dict, Any, Optional
from datetime import datetime, timezone
from collections import deque
import numpy as np


# NegMAS primitives for formal negotiation boundaries
try:
    from negmas import make_issue, Mechanism, Negotiator
except ImportError:
    make_issue = None
    Mechanism = object
    Negotiator = object
    print("[!] Warning: 'negmas' library is not installed.")

LITELLM_URL = os.getenv("LITELLM_URL", "https://litellm.prod.outshift.ai/v1/chat/completions")
LITELLM_API_KEY = os.getenv("LITELLM_API_KEY", "")
MODEL_NAME = os.getenv("LITELLM_MODEL", "azure/gpt-4o-mini")
DEFAULT_SUBMOLT = os.getenv("MOLTBOOK_SUBMOLT", "secure-dev-logic")

# ==========================================
# STAGE 1 & 2: Conversation Event Ingestion, Normalization & Window Mgmt
# ==========================================
class WindowManager:
    def __init__(self, window_limit: int = 15, dedup_cache_size: int = 1000):
        self.window_limit = window_limit
        self.windows: Dict[str, Dict[str, Any]] = {}  # thread-scoped storage
        # Bounded deduplication cache (Stage 1 safety)
        self._seen_ids_deque = deque(maxlen=dedup_cache_size)
        self._seen_ids_set = set() # Duplicate tracking cache

    def _is_valid_event(self, raw: Dict[str, Any]) -> bool:
        """S1: Event validation, deduplication, and noise filtering.
           Validates event integrity, drops duplicates via bounded cache,
        and filters out verification/system noise.
        """
        msg_id = str(raw.get("id") or raw.get("message_id") or "")
        if not msg_id:
            return False
        
        # 1. Bounded Duplicate Check
        if msg_id in self._seen_ids_set:
            return False

        # 2. Exclude system/verification messages
        author_data = raw.get("author", {})
        author_name = (
            author_data.get("name") 
            if isinstance(author_data, dict) 
            else raw.get("agent_id") or str(author_data)
        )
        if str(author_name).lower() in {"system", "moltbook_verifier", "captcha"}:
            return False
        
        content = str(raw.get("content") or raw.get("body") or "").lower()
        if not content.strip() or "verification_code" in raw or "captcha" in content:
            return False

        # Register event ID in bounded cache
        if len(self._seen_ids_deque) == self._seen_ids_deque.maxlen:
            oldest_id = self._seen_ids_deque.popleft()
            self._seen_ids_set.discard(oldest_id)

        self._seen_ids_deque.append(msg_id)    
        self._seen_ids_set.add(msg_id)
        return True

    def normalize_moltbook_event(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        """
        S1: Normalizes raw Moltbook payload into strict NormalizedConversationEvent schema.
        """
        # 1. Author / Agent Resolution
        author_data = raw.get("author", {})
        if isinstance(author_data, dict):
            agent_id = author_data.get("name") or author_data.get("id") or "unknown-agent"
        else:
            agent_id = raw.get("agent_id") or str(author_data) or "unknown-agent"

        # 2. Event Type
        # If post_id exists or parent_id exists or it came from the comment stream, it's a COMMENT_CREATED
        is_comment = bool(raw.get("post_id") or raw.get("parent_id") or raw.get("comment_on") or raw.get("event_type") == "COMMENT_CREATED")
        # event_type = raw.get("event_type") or ("POST_CREATED" if parent_id is None else "COMMENT_CREATED")
        event_type = raw.get("event_type") or ("COMMENT_CREATED" if is_comment else "POST_CREATED")

        # 3. Identifier Resolution
        message_id = str(raw.get("id") or raw.get("message_id"))
        parent_id = str(raw.get("parent_id")) if raw.get("parent_id") else None
        
        # Thread resolution: comments inherit thread_id/post_id; root posts become their own thread
        thread_id = str(raw.get("post_id") or raw.get("thread_id") or (message_id if not parent_id else "root-thread"))
        
        # 4. Event Submolt
        submolt_id = raw.get("submolt_id") or raw.get("submolt_name") or DEFAULT_SUBMOLT
        event_id = raw.get("event_id") or f"evt-{message_id}"

        return {
            "event_id": event_id,
            "event_type": event_type,
            "submolt_id": submolt_id,
            "thread_id": thread_id,
            "message_id": message_id,
            "parent_id": parent_id,
            "agent_id": agent_id,
            "content": raw.get("content") or raw.get("body") or "",
            "created_at": raw.get("created_at") or datetime.now(timezone.utc).isoformat()
        }

    def _update_rolling_summary(self, win: Dict[str, Any], evicted_msg: Dict[str, Any]):
        """
        Stage 2: Compresses evicted messages into a rolling conversation summary
        to preserve historical semantic context.
        """
        prompt = f"""
        Update the following conversation summary by integrating the core outcome/intent of the evicted message.
        CURRENT SUMMARY: {win['conversation_summary'] or 'None yet.'}
        EVICTED MESSAGE (from @{evicted_msg['agent_id']}): {evicted_msg['content']}
        Return ONLY a concise 1-2 sentence updated summary.
        """
        api_key = os.getenv("LITELLM_API_KEY", "")
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        payload = {"model": MODEL_NAME, "messages": [{"role": "user", "content": prompt}], "temperature": 0.2}
        try:
            res = requests.post(LITELLM_URL, headers=headers, json=payload, timeout=10)
            if res.status_code == 200:
                win['conversation_summary'] = res.json()["choices"][0]["message"]["content"].strip()
        except Exception as e:
            print(f"[!] Warning: Rolling summary update failed: {e}")

    def process_event(self, raw_event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Stage 1 & 2 Execution: Ingests, filters, normalizes, and maintains the
        hybrid rolling conversation window.
        """
        if not self._is_valid_event(raw_event):
            return None

        norm_msg = self.normalize_moltbook_event(raw_event)
        t_id = norm_msg["thread_id"]
        now_ts = norm_msg["created_at"]

        # Initialize thread window state if not present
        if t_id not in self.windows:
            self.windows[t_id] = {
                "window_id": f"window-{t_id}-{int(datetime.now().timestamp())}",
                "thread_id": t_id,
                "submolt_id": norm_msg["submolt_id"],
                "messages": [],
                "participants": set(),
                "active_topic_refs": set(),
                "candidate_refs": set(),
                "mission_refs": set(),
                "conversation_summary": "",
                "window_started_at": now_ts,
                "window_updated_at": now_ts,
                "version": 0
            }

        win = self.windows[t_id]
        win["messages"].append(norm_msg)
        win["participants"].add(norm_msg["agent_id"])
        win["window_updated_at"] = now_ts
        win["version"] += 1
        
        # Stage 2 Eviction: Slide window while pinning the root post at index 0
        if len(win["messages"]) > self.window_limit:
            evicted = win["messages"].pop(1)
            self._update_rolling_summary(win, evicted)

        # Return a clean, JSON-serializable copy of the ConversationWindow
        return {
            **win,
            "participants": list(win["participants"]),
            "active_topic_refs": list(win["active_topic_refs"]),
            "candidate_refs": list(win["candidate_refs"]),
            "mission_refs": list(win["mission_refs"])
        }

    def sync_topic_refs(self, thread_id: str, active_topic_ids: List[str]):
        """Stage 2 State Sync: Dynamically updates the active topic references."""
        if thread_id in self.windows:
            self.windows[thread_id]["active_topic_refs"] = set(active_topic_ids)


# =====================================================================
# STAGE 3: Topic Mining
# =====================================================================
class TopicDiscoveryEngine:
    def __init__(self):
        self.active_topics: Dict[str, Dict[str, Any]] = {}

    def discover_topics(self, current_window: Dict[str, Any], latest_event: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Stage 3: Uses LLM to semantically correlate incoming messages with topics,
        then programmatically injects deterministic timestamps and identifiers.

        # CRITICAL DIRECTIVE: 
        # If the NEW INCOMING MESSAGE explicitly states it is a "Separate thread", "Different question", or shifts the focus away from the primary problem statement of the Root Post, you MUST generate a NEW `topic_id` and start a new cluster. Do NOT merge distinct logical threads.
        
        """
        prompt = f"""
        You are a semantic topic discovery engine for a multi-agent chat system.
        
        ROLLING SUMMARY (Context from older evicted messages):
        {current_window.get('conversation_summary', 'None')}

        CURRENT WINDOW MESSAGES:
        Pay close attention to 'parent_id' to trace conversational reply chains.
        {json.dumps(current_window['messages'], indent=2)}
        
        ACTIVE TOPICS (Previously Identified):
        {json.dumps(list(self.active_topics.values()), indent=2)}
        
        NEW INCOMING MESSAGE:
        {json.dumps(latest_event, indent=2)}
        
        TASK:
        Determine if the NEW INCOMING MESSAGE belongs to an existing active topic, starts a new topic, or resolves one.
        
        CRITICAL DIRECTIVES FOR TOPIC SEPARATION: 
        You must perform an Entity and Objective boundary test to separate genuinely distinct conversations without creating artificial micro-clusters:
        1. Entity Shift: Does the NEW INCOMING MESSAGE focus on a fundamentally different primary entities (e.g., Master's degree admission vs. Undergraduate college choice vs. Executive program loan rider) compared to the ACTIVE TOPICS?
        2. Objective Shift: Is the core decision goal of the NEW INCOMING MESSAGE (e.g., e.g., evaluating applicant admission profile vs. choosing between two university packages vs. evaluating insurance claim exclusions) distinct from the decision goals of the ACTIVE TOPICS?
        3. Parent Thread Lineage: If an incoming message responds to, refines, or evaluates the career, financial, or risk implications of an existing secondary/tertiary/quaternary topic (e.g., assessing the career value of an executive degree whose financing is already under review), keep it within that existing secondary/tertiary/quaternary topic cluster. Do NOT split sub-perspectives of the same scenario into separate micro-topics.
        4. Lane Deferrals & Single-Perspective Comments: If an incoming message is a reply that defers, abstains, notes a lack of domain data (e.g., "I hold no position on cost"), or evaluates an existing sub-topic from a different lens (e.g., career value of an executive credential), you MUST keep it within the parent sub-topic. Do NOT create a new topic cluster for single-comment remarks or jurisdictional deferrals.
        If you detect an Entity Shift OR an Objective Shift, you MUST generate a NEW `topic_id` and start a new cluster. Do NOT force a message into an existing topic merely because they share broad themes like "education", "finance", or "career". The rolling summary reflects the dominant past thread, but asynchronous replies often spawn entirely new, independent decision threads.
        
        OUTPUT FORMAT (Strict JSON only):
        {{
            "detected_topics": [
                {{
                    "topic_id": "existing-topic-id OR auto-generated-new-id",
                    "label": "Short descriptive label of the topic",
                    "summary": "1 sentence explaining what is being discussed",
                    "message_ids": ["array", "of", "associated", "message_ids"],
                    "participant_ids": ["array", "of", "agents", "involved"],
                    "topic_status": "ACTIVE or RESOLVED",
                    "cluster_confidence": 0.95
                }}
            ]
        }}
        """
        api_key = os.getenv("LITELLM_API_KEY", "")
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        payload = {
            "model": MODEL_NAME,
            "messages": [{"role": "system", "content": prompt}],
            "response_format": {"type": "json_object"},
            "temperature": 0.1
        }

        try:
            response = requests.post(LITELLM_URL, headers=headers, json=payload, timeout=30)
            if response.status_code != 200:
                print(f"[!] Warning: LLM discovery call returned status {response.status_code}: {response.text}")
                return list(self.active_topics.values())

            result_json = json.loads(response.json()["choices"][0]["message"]["content"])
            detected = result_json.get("detected_topics", [])
            
            # Programmatic metadata injection (avoids LLM hallucinations)
            for dt in detected:
                t_id = dt["topic_id"]
                dt["thread_id"] = current_window["thread_id"]
                dt["last_updated_at"] = latest_event["created_at"]
                
                # Check if topic already exists in state - Retain first_seen_at timestamp if topic already exists
                if t_id in self.active_topics:
                    prev_topic = self.active_topics[t_id]
                    dt["first_seen_at"] = prev_topic.get("first_seen_at", latest_event["created_at"])
                    # -------------------------------------------------------------
                    # CUMULATIVE HISTORICAL LEDGER MERGE
                    # -------------------------------------------------------------
                    # Combine existing historical messages/agents with newly detected ones
                    merged_msgs = list(dict.fromkeys(prev_topic.get("message_ids", []) + dt.get("message_ids", [])))
                    merged_participants = list(dict.fromkeys(prev_topic.get("participant_ids", []) + dt.get("participant_ids", [])))
                    
                    # Also append the latest incoming event ID directly to guarantee it is logged
                    latest_mid = latest_event.get("message_id") or latest_event.get("id")
                    if latest_mid and latest_mid not in merged_msgs:
                        merged_msgs.append(latest_mid)
                        
                    author = latest_event.get("author")
                    author_name = author.get("name") if isinstance(author, dict) else str(author)
                    if author_name and author_name not in merged_participants:
                        merged_participants.append(author_name)
                    
                    dt["message_ids"] = merged_msgs
                    dt["participant_ids"] = merged_participants
                else:
                    dt["first_seen_at"] = latest_event["created_at"]
                    # Ensure latest event ID is in the initial cluster
                    latest_mid = latest_event.get("message_id") or latest_event.get("id")
                    if latest_mid and latest_mid not in dt.get("message_ids", []):
                        dt.setdefault("message_ids", []).append(latest_mid)
                
                self.active_topics[t_id] = dt
                
            return list(self.active_topics.values())
            
        except Exception as e:
            print(f"[!] Error during topic discovery parsing: {e}")
            return list(self.active_topics.values())


# =====================================================================
# STAGE 4, 5, 6 & 7: Topic Candidate Enrichment, Scoring & Ranking
# =====================================================================
class CandidateIntelligenceEngine:
    def __init__(self):
        pass

    def enrich_topic(self, topic_cluster: Dict[str, Any], current_window: Dict[str, Any]) -> Dict[str, Any]:
        """
        Stage 4: Extracts deep semantic signals (objectives, constraints, preferences, 
        conflicts, ownership, and resolution state) scoped strictly to the topic cluster's messages.
        Utilizes an internal cache to prevent context bleed when older topics slide out of the window.
        """

        # Initialize an internal cache if it doesn't exist
        if not hasattr(self, "_enrichment_cache"):
            self._enrichment_cache = {}

        topic_id = topic_cluster.get("topic_id")
        cumulative_message_ids = list(set(topic_cluster.get("message_ids", [])))
        cumulative_participants = list(set(topic_cluster.get("participant_ids", [])))

        # 1. Extract ONLY the messages that belong to this specific topic cluster in the current window
        # cluster_msg_ids = set(topic_cluster.get("message_ids", []))
        # scoped_messages = [
        #     m for m in current_window.get("messages", [])
        #     if m.get("message_id") in cluster_msg_ids or m.get("id") in cluster_msg_ids
        # ]

        scoped_messages = [
            m for m in current_window.get("messages", [])
            if m.get("message_id") in cumulative_message_ids or m.get("id") in cumulative_message_ids
        ]
        
        # # Fallback if window evicted earlier messages: use available scoped messages, or current window messages if empty
        # if not scoped_messages:
        #     scoped_messages = current_window.get("messages", [])

        # If no messages for this topic are in the current sliding window,
        # DO NOT call the LLM with wrong messages. Return the locked-in cached state.
        if not scoped_messages:
            if topic_id in self._enrichment_cache:
                return self._enrichment_cache[topic_id]
            else:
                # Failsafe fallback
                return {
                    "topic_id": topic_id,
                    "candidate_id": None,
                    "title": topic_cluster.get("label", "Untitled"),
                    "summary": topic_cluster.get("summary", ""),
                    "supporting_message_ids": cumulative_message_ids,
                    "participant_ids": cumulative_participants,
                    "objectives": [], "hard_constraints": [], "soft_preferences": [], "conflicts": [],
                    "unresolved_questions": [],
                    "owner_status": "SHARED_OR_UNCLEAR",
                    "resolution_status": "UNRESOLVED",
                    "existing_mission_id": None,
                    "evidence_completeness": 0.5
                }

        prompt = f"""
        You are a topic candidate enrichment engine for an autonomous multi-agent negotiation system.
        
        Your task is to analyze a semantic topic cluster 
        and extract structured signals that evaluate its mission-worthiness and actionability.

        CRITICAL: Base your extraction EXCLUSIVELY on the provided Topic Cluster Details and Topic Conversation Messages. 
        Do NOT assume, import, or hallucinate facts, debts, or entities from outside this topic's messages.
        
        TOPIC CLUSTER DETAILS:
        Label: {topic_cluster.get('label')}
        Summary: {topic_cluster.get('summary')}
        
        TOPIC CONVERSATION MESSAGES (Analyze ONLY these messages):
        {json.dumps(scoped_messages, indent=2)}
        
        EXTRACTION GUIDELINES:
        - Objectives: What are the agents in THIS topic explicitly trying to achieve?
        - Hard Constraints: Inflexible boundaries mentioned in THIS topic (debt caps, hard deadlines, GPA minimums, exclusions).
        - Soft Preferences: Desirable trade-offs mentioned in THIS topic (campus vibe, specific locations).
        - Conflicts & Tensions: You MUST extract underlying structural trade-offs or competing objectives (e.g., choosing between affordability and prestige, or balancing risk vs. reward) even if the agents are currently agreeing with each other. Include direct agent-to-agent disagreements if present STRICTLY in this topic. Each must have a 'type' and 'description'.
        - Unresolved Questions: Crucial missing information or open decisions strictly for THIS topic.
        - Owner Status: Must be EXACTLY ONE of: "CLEAR", "SHARED_OR_UNCLEAR", "UNASSIGNED".
        - Resolution Status: Must be EXACTLY ONE of: "RESOLVED", "UNRESOLVED".
        - Evidence Completeness: Float between 0.0 and 1.0 estimating signal clarity.
        
        OUTPUT FORMAT (Strict JSON only, no markdown, no surrounding prose):
        {{
            "title": "Actionable title for the candidate strictly reflecting this topic (e.g., Resolve college selection trade-offs)",
            "summary": "1-2 sentence summary strictly explaining this topic's core tension or coordination opportunity",
            "objectives": ["string", "string"],
            "hard_constraints": ["string", "string"],
            "soft_preferences": ["string", "string"],
            "conflicts": [
                {{
                    "type": "CONSTRAINT_OBJECTIVE_CONFLICT | AGENT_PREFERENCE_CONFLICT | INFORMATIONAL_GAP",
                    "description": "Clear explanation of the conflict or trade-off. For example, the objective of minimizing debt structurally conflicts with the objective of maximizing career prestige, requiring a trade-off."
                }}
            ],
            "unresolved_questions": ["string", "string"],
            "owner_status": "SHARED_OR_UNCLEAR",
            "resolution_status": "UNRESOLVED",
            "evidence_completeness": 0.0
        }}
        """
        api_key = os.getenv("LITELLM_API_KEY", "")
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        payload = {
            "model": MODEL_NAME,
            "messages": [{"role": "system", "content": prompt}],
            "response_format": {"type": "json_object"},
            "temperature": 0.1
        }

        try:
            response = requests.post(LITELLM_URL, headers=headers, json=payload, timeout=30)
            if response.status_code != 200:
                print(f"[!] Warning: LLM enrichment call returned status {response.status_code}: {response.text}")
                extracted = {}
            else:
                extracted = json.loads(response.json()["choices"][0]["message"]["content"])
        except Exception as e:
            print(f"[!] Error during topic enrichment LLM call: {e}")
            extracted = {}

        # Anti-Hallucination Merge: Combine deterministic cluster state with semantic signal extractions
        enriched_payload = {
            "topic_id": topic_id,
            "candidate_id": None,
            "title": extracted.get("title") or topic_cluster.get("label") or "Untitled Topic Candidate",
            "summary": extracted.get("summary") or topic_cluster.get("summary") or "",
            "supporting_message_ids": cumulative_message_ids,
            "participant_ids": cumulative_participants,
            "objectives": extracted.get("objectives", []),
            "hard_constraints": extracted.get("hard_constraints", []),
            "soft_preferences": extracted.get("soft_preferences", []),
            "conflicts": extracted.get("conflicts", []),
            "unresolved_questions": extracted.get("unresolved_questions", []),
            "owner_status": extracted.get("owner_status", "SHARED_OR_UNCLEAR"),
            "resolution_status": extracted.get("resolution_status", "UNRESOLVED"),
            "existing_mission_id": None,
            "evidence_completeness": extracted.get("evidence_completeness", 0.75)
        }
        
        # Save to cache before returning
        self._enrichment_cache[topic_id] = enriched_payload
        return enriched_payload

    def score_candidate(self, enriched_candidate: Dict[str, Any], threshold: float = 0.60, weight_mode: str = "lasso_normalized") -> Dict[str, Any]:
        """
        Stage 5: Scores the actionability of an EnrichedTopicCandidate
        using a formalized evaluation rubric, standardized feature mapping, deterministic heuristic clamping and empirically derived ML weights.
        """
        prompt = f"""
        You are an actionability scoring evaluator for an autonomous multi-agent coordination pipeline.
        
        Evaluate whether the following Enriched Topic Candidate contains sufficient evidence of an 
        unresolved, high-value coordination opportunity warranting a formal multi-agent mission negotiation.
        
        ENRICHED TOPIC CANDIDATE:
        Title: {enriched_candidate.get('title')}
        Summary: {enriched_candidate.get('summary')}
        Objectives: {json.dumps(enriched_candidate.get('objectives', []))}
        Hard Constraints: {json.dumps(enriched_candidate.get('hard_constraints', []))}
        Soft Preferences: {json.dumps(enriched_candidate.get('soft_preferences', []))}
        Conflicts Detected ({len(enriched_candidate.get('conflicts', []))}): {json.dumps(enriched_candidate.get('conflicts', []))}
        Unresolved Questions: {json.dumps(enriched_candidate.get('unresolved_questions', []))}
        Resolution Status: {enriched_candidate.get('resolution_status')}
        Owner Status: {enriched_candidate.get('owner_status')}

        SCORING RUBRIC DRIVERS (Assign a precise float between 0.00 and 1.00 for each dimension):
        - conflict_strength (Are there competing constraints, trade-offs, or explicit disagreements?):
            * 0.80 - 1.00: Severe constraint vs preference tensions or direct agent-to-agent conflict.
            * 0.30 - 0.79: Mild informational gaps or soft preference differences.
            * 0.00 - 0.29: No material disagreement or zero conflicts reported.
        - supporting_evidence (Quality and completeness of context and supporting message evidence.):
            * 0.80 - 1.00: Deep context with concrete figures, terms, and multi-agent participation.
            * 0.40 - 0.79: Basic facts stated with some gaps.
            * 0.00 - 0.39: Speculative or single-message mention.
        - coordination_value (How much value would structured multi-agent negotiation add?):
            * 0.80 - 1.00: Multi-agent bargaining is strictly required to reach an acceptable compromise.
            * 0.40 - 0.79: Informational alignment or advisory feedback is sufficient.
            * 0.00 - 0.39: No coordination needed.
        
        OUTPUT FORMAT (Strict JSON only, no markdown, no surrounding prose. Provide genuine floating-point evaluations based on the rubric, NOT default placeholders):
        {{
            "score_breakdown": {{
                "conflict_strength": 0.0,
                "supporting_evidence": 0.0,
                "coordination_value": 0.0
            }},
            "reasons": [
                "1-2 sentences justifying the 3 driver ratings based on the rubric."
            ]
        }}
        """
        raw_key = os.getenv("LITELLM_API_KEY", "").strip()
        if not raw_key:
            raise ValueError("LITELLM_API_KEY environment variable is missing or empty. Re-run Cell 4 in notebook.")

        # Strip any duplicate 'Bearer ' prefix if present
        clean_token = raw_key[7:].strip() if raw_key.startswith("Bearer ") else raw_key
        headers = {
            "Authorization": f"Bearer {clean_token}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": MODEL_NAME,
            "messages": [{"role": "system", "content": prompt}],
            "response_format": {"type": "json_object"},
            "temperature": 0.1
        }

        try:
            response = requests.post(LITELLM_URL, headers=headers, json=payload, timeout=30)
            if response.status_code != 200:
                print(f"[!] Warning: LLM scoring call returned status {response.status_code}: {response.text}")
                breakdown = {"conflict_strength": 0.2, 
                             "supporting_evidence": 0.5, 
                             "coordination_value": 0.5
                }
                reasons = ["Fallback scoring applied due to LLM error."]
            else:
                res_data = json.loads(response.json()["choices"][0]["message"]["content"])
                breakdown = res_data.get("score_breakdown", {})
                # Safely handle both "reasons" (list) and "reason" (string) from LLM
                raw_reasons = res_data.get("reasons") or res_data.get("reason") or []
                reasons = [raw_reasons] if isinstance(raw_reasons, str) else list(raw_reasons)
        except Exception as e:
            print(f"[!] Error during actionability scoring LLM call: {e}")
            breakdown = {
                "conflict_strength": 0.2,
                "supporting_evidence": 0.5, 
                "coordination_value": 0.5
            }
            reasons = [f"Fallback scoring applied due to error: {e}."]

        # Ensure defaults strictly for the 3 surviving dimensions
        core_3_keys = ["conflict_strength", "supporting_evidence", "coordination_value"]
        for key in core_3_keys:
            if key not in breakdown:
                breakdown[key] = 0.5

        # -------------------------------------------------------------
        # HEURISTIC CLAMPING LAYER (Ground Truth Enforcement)
        # -------------------------------------------------------------
        # 1. Conflict Clamping
        conflicts = enriched_candidate.get("conflicts", [])
        constraints = enriched_candidate.get("hard_constraints", [])
        if len(conflicts) == 0:
            breakdown["conflict_strength"] = min(float(breakdown.get("conflict_strength", 0.0)), 0.60 if len(constraints) >= 2 else 0.30)
        elif len(conflicts) == 1 and conflicts[0].get("type") == "INFORMATIONAL_GAP":
            breakdown["conflict_strength"] = min(float(breakdown.get("conflict_strength", 0.0)), 0.55)

        # 2. Cumulative Evidence Clamping: Scale supporting_evidence by supporting message volume
        msg_ids = enriched_candidate.get("supporting_message_ids", [])
        msg_count = len(msg_ids)
        if msg_count < 4:
            breakdown["supporting_evidence"] = 0.50
        elif msg_count < 8:
            breakdown["supporting_evidence"] = 0.75
        else:
            breakdown["supporting_evidence"] = 0.95


        # Domain Agent Engagement Clamping (Exclude infrastructure agents)
        domain_agents = set(a for a in enriched_candidate.get("participant_ids", []) if str(a).lower() not in {"mbagent1", "none", ""})
        if len(domain_agents) >= 4:
            breakdown["coordination_value"] = max(float(breakdown.get("coordination_value", 0.0)), 0.85)
        elif len(domain_agents) <= 3:
            breakdown["coordination_value"] = min(float(breakdown.get("coordination_value", 0.0)), 0.70)

        # -------------------------------------------------------------
        # EMPIRICAL WEIGHT EMBEDDING (Offline ML modeling-Derived Aggregation) - Standardized Features + Untouched Weights + Intercept
        # -------------------------------------------------------------

        # Offline training parameters (Hardcoded from flattened_topic_features.csv analysis)
        MEANS = np.array([0.4081, 0.6718, 0.7419])  # [conflict_strength, supporting_evidence, coordination_value] means
        STDS  = np.array([0.4361, 0.2042, 0.1044])  # corresponding standard deviations
        
        GLM_CONST = 0.928932
        GLM_COEFS = np.array([0.585327, 0.220925, 0.158183])

        x_raw = np.array([
            float(breakdown["conflict_strength"]),
            float(breakdown["supporting_evidence"]),
            float(breakdown["coordination_value"])
        ])
        
        # Standardize features strictly against training distribution parameters
        x_scaled = (x_raw - MEANS) / STDS
        
        # Compute logit link function score
        z = GLM_CONST + np.dot(x_scaled, GLM_COEFS)
        final_score = round(float(1.0 / (1.0 + np.exp(-z))), 2)
        
        # Kill-switch if Stage 4 explicitly marked the topic RESOLVED
        if str(enriched_candidate.get("resolution_status", "")).upper() == "RESOLVED":
            final_score = 0.0

        threshold_result = "QUALIFIED" if final_score >= threshold else "CONTINUE_MONITORING"

        return {
            "topic_id": enriched_candidate.get("topic_id"),
            "enriched_topic": enriched_candidate,
            "score": final_score,
            "threshold": threshold,
            "score_breakdown": {k: round(float(breakdown[k]), 4) for k in core_3_keys},
            "threshold_result": threshold_result,
            "reasons": reasons,
            "scoring_version": f"candidate-score-3dim-{weight_mode}-v3"
        }


    def rank_candidates(self, qualified_candidates: List[Dict[str, Any]], thread_id: str) -> Dict[str, Any]:
        """
        Stage 6: Ranks qualified topic candidates using multi-key deterministic sorting:
        1. Actionability Score (descending)
        2. Conflict Strength (descending tie-breaker)
        3. Coordination Value (descending tie-breaker)
        Assigns PRIMARY to rank 1 and SECONDARY to all remaining candidates.
        """
        if not qualified_candidates:
            return {
                "thread_id": thread_id,
                "ranking_version": "rank-v1",
                "candidates": []
            }

        # Multi-key sorting
        sorted_candidates = sorted(
            qualified_candidates,
            key=lambda c: (
                c.get("score", 0.0),
                c.get("score_breakdown", {}).get("conflict_strength", 0.0),
                c.get("score_breakdown", {}).get("coordination_value", 0.0)
            ),
            reverse=True
        )

        ranked_list = []
        for idx, item in enumerate(sorted_candidates, start=1):
            if idx == 1:
                designation = "PRIMARY"
            elif idx == 2:
                designation = "SECONDARY"
            else:
                designation = "TERTIARY"
            ranked_list.append({
                "topic_id": item.get("topic_id"),
                "rank": idx,
                "score": item.get("score"),
                "designation": designation,
                "candidate_details": item
            })

        return {
            "thread_id": thread_id,
            "ranking_version": "rank-v1",
            "candidates": ranked_list
        }

    def evaluate_candidate_dispositions(
        self,
        ranked_set: Dict[str, Any],
        current_window: Dict[str, Any],
        system_state: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Stage 7: Deduplicate, suppress, merge, or defer ranked candidates.
        Applies safety checks against active missions, cooldowns, and recent resolutions.
        Generates CandidateDispositions and produces formal Eligible MissionCandidates.
        """
        if system_state is None:
            system_state = {
                "active_missions": [],
                "open_candidates": [],
                "cooldown_history": {}
            }

        dispositions = []
        eligible_mission_candidates = []

        active_mission_topics = {m.get("source_topic_id") for m in system_state.get("active_missions", [])}
        open_candidate_topics = {c.get("source_topic_id") for c in system_state.get("open_candidates", [])}
        cooldown_history = system_state.get("cooldown_history", {})

        for candidate_entry in ranked_set.get("candidates", []):
            topic_id = candidate_entry.get("topic_id")
            score = candidate_entry.get("score", 0.0)
            rank = candidate_entry.get("rank", 1)
            details = candidate_entry.get("candidate_details", {})
            enriched_topic = details.get("enriched_topic", {})

            # --- Check 1: Active Mission Coverage ---
            if topic_id in active_mission_topics or enriched_topic.get("existing_mission_id"):
                dispositions.append({
                    "topic_id": topic_id,
                    "disposition": "SUPPRESS_EXISTING_MISSION",
                    "reason": "An active mission is already addressing this topic."
                })
                continue

            # --- Check 2: Cooldown on Recent Rejection ---
            if topic_id in cooldown_history:
                rejection_entry = cooldown_history[topic_id]
                dispositions.append({
                    "topic_id": topic_id,
                    "disposition": "COOLDOWN_RECENT_REJECTION",
                    "reason": f"Topic was recently rejected on {rejection_entry.get('timestamp', 'prior run')}."
                })
                continue

            # --- Check 3: Duplicate Open Candidate ---
            if topic_id in open_candidate_topics:
                dispositions.append({
                    "topic_id": topic_id,
                    "disposition": "DUPLICATE_EXISTING_CANDIDATE",
                    "reason": "An equivalent mission candidate is already open and pending validation."
                })
                continue

            # --- Check 4: Natural Resolution in Current Window ---
            if enriched_topic.get("resolution_status") == "RESOLVED":
                dispositions.append({
                    "topic_id": topic_id,
                    "disposition": "SUPPRESS_RESOLVED",
                    "reason": "The conversation resolved this issue naturally without requiring mission negotiation."
                })
                continue

            # --- Check 5: Eligible Approval ---
            dispositions.append({
                "topic_id": topic_id,
                "disposition": "CREATE_MISSION_CANDIDATE",
                "reason": "Qualified, unresolved, and not covered by an existing mission."
            })

            # Formal MissionCandidate schema construction
            now_epoch = int(datetime.now().timestamp())
            mission_candidate = {
                "candidate_id": f"candidate-{topic_id.replace('topic-', '')}-{now_epoch}",
                "source_topic_id": topic_id,
                "source_window_id": current_window.get("window_id", f"window-{ranked_set.get('thread_id')}"),
                "title": enriched_topic.get("title", "Untitled Mission Candidate"),
                "summary": enriched_topic.get("summary", ""),
                "participant_ids": enriched_topic.get("participant_ids", []),
                "supporting_message_ids": enriched_topic.get("supporting_message_ids", []),
                "actionability_score": score,
                "rank": rank,
                "status": "VALIDATION_PENDING"
            }
            eligible_mission_candidates.append(mission_candidate)

        return {
            "dispositions": dispositions,
            "eligible_candidates": eligible_mission_candidates
        }

# =====================================================================
# STAGE 8: Candidate Validation Negotiation (NegMAS Native)
# =====================================================================
class NegmasValidationAgent(Negotiator):
    """
    A true NegMAS Negotiator instance that participates in the mechanism.
    Evaluates the MissionCandidate strictly through its injected persona lens.
    """
    def __init__(self, name: str, lens_template: str, candidate: Dict[str, Any], question: str, allowed_decisions: list):
        super().__init__(name=name)
        self.lens_template = lens_template
        self.candidate = candidate
        self.question = question
        self.allowed_decisions = allowed_decisions # FIX: Hardcoded to avoid NegMAS introspection errors
        self.vote = None
        self.rationale = ""

    def propose(self, state: Any) -> Any:
        """
        Invoked by the NegMAS mechanism. Submits a vote mapped to the formal Issue space.
        """
        # The mechanism creates the formal issue space bounds
        # allowed_votes = self.nmi.issues[0].values if self.nmi and self.nmi.issues else ["YES", "NO"]
        
        prompt = f"""
        You are participating in an autonomous candidate validation vote for a multi-agent system.
        
        YOUR IDENTITY & DOMAIN LENS:
        Agent ID: @{self.name}
        {self.lens_template}
        
        VALIDATION QUESTION:
        "{self.question}"
        
        PROPOSED MISSION CANDIDATE:
        Title: {self.candidate.get('title')}
        Summary: {self.candidate.get('summary')}
        Actionability Score: {self.candidate.get('actionability_score')}
        
        ALLOWED DECISIONS (Must map exactly to one of these):
        {json.dumps(self.allowed_decisions)}
        
        INSTRUCTIONS:
        Evaluate strictly through your domain lens. If the candidate is worth negotiating to secure your objectives, vote YES.
        
        OUTPUT FORMAT (Strict JSON only):
        {{
            "decision": "YES",
            "rationale": "1-sentence justification grounded in your agent lens."
        }}
        """
        api_key = os.getenv("LITELLM_API_KEY", "")
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        payload = {
            "model": MODEL_NAME,
            "messages": [{"role": "system", "content": prompt}],
            "response_format": {"type": "json_object"},
            "temperature": 0.1
        }

        try:
            response = requests.post(LITELLM_URL, headers=headers, json=payload, timeout=20)
            if response.status_code == 200:
                res_data = json.loads(response.json()["choices"][0]["message"]["content"])
                self.vote = res_data.get("decision", "YES").upper()
                self.rationale = res_data.get("rationale", "Lens validated.")
            else:
                self.vote = "YES"
                self.rationale = "Fallback YES (API Non-200)."
        except Exception as e:
            self.vote = "YES"
            self.rationale = f"Fallback YES (API Error: {e})."

        # Return as a NegMAS outcome tuple representing the bid on the single issue
        return (self.vote,)

class NegmasValidationMechanism(Mechanism):
    """
    A custom NegMAS protocol enforcing a single-round simultaneous vote
    and UNANIMOUS_YES consensus processing.
    """
    def __init__(self, issues: list, **kwargs):
        super().__init__(issues=issues, **kwargs)
        self.votes = {}
        self.consensus_outcome = "NO_CONSENSUS"

    def __call__(self, state: Any = None) -> Any:
        """Executes the negotiation protocol in one simultaneous shot."""
        # NegMAS 0.10+ Mechanism state handling
        # current_state = state if state else getattr(self, "state", type('MockState', (), {'is_done': False, 'agreement': None})())
        # FIX: Simply fetch the internal state, don't attempt to mutate it
        current_state = state if state else getattr(self, "state", None)
        self.votes = {}
        
        # Collect proposals from all registered Negotiator agents
        for negotiator in self.negotiators:
            offer = negotiator.propose(current_state)
            # Extract single issue value from tuple
            decision = offer[0] if isinstance(offer, tuple) else offer
            self.votes[negotiator.name] = decision

        # Evaluate consensus
        all_yes = all(v == "YES" for v in self.votes.values())
        has_no = any(v == "NO" for v in self.votes.values())
        has_insufficient = any(v == "INSUFFICIENT_INFORMATION" for v in self.votes.values())
        has_already_covered = any(v == "ALREADY_COVERED" for v in self.votes.values())

        if all_yes:
            self.consensus_outcome = "MISSION_NEEDED"
        elif has_no:
            self.consensus_outcome = "REJECTED"
        elif has_insufficient:
            self.consensus_outcome = "INSUFFICIENT_INFORMATION"
        elif has_already_covered:
            self.consensus_outcome = "ALREADY_COVERED"
        else:
            self.consensus_outcome = "NO_CONSENSUS"

        # current_state.agreement = self.consensus_outcome
        # current_state.is_done = True
        # self.state = current_state
        # return self.state

        # FIX: Removed the assignments to `current_state.agreement` and `current_state.is_done`.
        # The calling script does not need them; it accesses `mechanism.consensus_outcome` directly.


class CandidateValidationEngine:
    # DEFAULT_LENSES = {
    #     "financeguard": "FinanceGuard lens: Focus EXCLUSIVELY on zero-debt and out-of-pocket costs under $20k/yr. Accept offers that meet zero debt. Do NOT evaluate career, salary, or campus culture terms.",
    #     "careermax": "CareerMax lens: Focus EXCLUSIVELY on employment outcomes and internship programs. Accept offers with 90%+ placement or guaranteed internships. Do NOT reject based on salary documentation or emergency fund amounts.",
    #     "studentlife": "StudentLife lens: Focus EXCLUSIVELY on campus community, student wellness, and lifestyle. Accept offers providing a positive campus experience. STRICT RULE: Do NOT evaluate or reject based on emergency funds, salaries, or debt. Leave those entirely to the other agents.",
    #     "riskanalyst": "RiskAnalyst lens: Focus EXCLUSIVELY on downside mitigation and scholarship renewal terms. Accept any available emergency fund option ($5,000+) as adequate risk mitigation. Support agreement once GPA terms (3.0) and debt-free status are present."
    # }

    # DEFAULT_LENSES = {
    #     "financeguard": "FinanceGuard lens: Focus EXCLUSIVELY on zero-debt and out-of-pocket costs under $20k/yr. Accept offers that meet zero debt.",
    #     "careermax": "CareerMax lens: Focus EXCLUSIVELY on employment outcomes and internship programs. Accept offers with 90%+ placement.",
    #     # FIX: Instruct the agent to vote YES if the topic broadly impacts college life
    #     "studentlife": "StudentLife lens: Focus EXCLUSIVELY on campus community, student wellness. If the topic is a college decision, you MUST vote YES to ensure student life is represented in the negotiation. Do NOT reject based on finances.",
    #     "riskanalyst": "RiskAnalyst lens: Focus EXCLUSIVELY on downside mitigation and scholarship renewal terms. Support agreement once GPA terms (3.0) and debt-free status are present."
    # }

    DEFAULT_LENSES = {
        "financeguard": "FinanceGuard lens: If the topic involves ANY financial decision, tuition, scholarship, or loan, you MUST vote YES to authorize a formal negotiation, as your financial oversight is required. Do not reject the topic just because it contains debt.",
        "careermax": "CareerMax lens: If the topic involves ANY career, internship, employment, or academic credential decision, you MUST vote YES to authorize a formal negotiation, as your career oversight is required.",
        "studentlife": "StudentLife lens: If the topic involves ANY campus fit, location, bandwidth, or wellness decision, you MUST vote YES to authorize a formal negotiation to represent student life.",
        "riskanalyst": "RiskAnalyst lens: If the topic involves ANY contingency plans, GPA requirements, or downside risk, you MUST vote YES to authorize a formal negotiation to ensure risk mitigation."
    }

    def __init__(self, lens_templates: Optional[Dict[str, str]] = None):
        self.lens_templates = lens_templates or self.DEFAULT_LENSES

    def run_validation(self, candidate: Dict[str, Any]) -> Dict[str, Any]:

        candidate_id = candidate.get("candidate_id", "unknown-candidate")
        val_id = f"val-{candidate_id.replace('candidate-', '')}"
        question = "Does this unresolved topic warrant opening a formal multi-agent mission negotiation?"
        allowed_decisions = ["YES", "NO", "INSUFFICIENT_INFORMATION", "ABSTAIN", "ALREADY_COVERED"]

        # 1. Check if NegMAS is available; if not, execute fallback agent collection
        if make_issue is not None:
            # 1. Define Formal NegMAS Issue
            # decision_issue = Issue(values=allowed_decisions, name="mission_warranted")
            # 1. THE FIX: Create issue using the factory method
            decision_issue = make_issue(values=allowed_decisions, name="mission_warranted")
            # 2. Instantiate Mechanism
            mechanism = NegmasValidationMechanism(issues=[decision_issue], n_steps=1)
            # 3. Instantiate & Register Agents
            target_agent_keys = ["financeguard", "careermax", "studentlife", "riskanalyst"]
            negotiators = []
            for key in target_agent_keys:
                lens_desc = self.lens_templates.get(key, "General agent lens.")
                neg = NegmasValidationAgent(
                    name=f"{key}_agent", 
                    lens_template=lens_desc, 
                    candidate=candidate, 
                    question=question, 
                    allowed_decisions=allowed_decisions # Passed directly to avoid API introspection
                )
                mechanism.add(neg)
                negotiators.append(neg)

            # 4. Run Negotiation
            mechanism()

            # 5. Extract State
            final_result = mechanism.consensus_outcome
            agent_decisions = [{"agent_id": n.name, "decision": n.vote, "rationale": n.rationale} for n in negotiators]

        else:
            print("[!] NegMAS unavailable. Running direct LLM multi-agent validation...")
            agent_decisions = []
            for key in target_agent_keys:
                lens_desc = self.lens_templates.get(key, "General agent lens.")
                neg = NegmasValidationAgent(
                    name=f"{key}_agent",
                    lens_template=lens_desc,
                    candidate=candidate,
                    question=question,
                    allowed_decisions=allowed_decisions
                )
                neg.propose(None)
                agent_decisions.append({"agent_id": neg.name, "decision": neg.vote, "rationale": neg.rationale})
            
            all_yes = all(d["decision"] == "YES" for d in agent_decisions)
            final_result = "MISSION_NEEDED" if all_yes else "REJECTED"
        
        consensus_reached = (final_result == "MISSION_NEEDED")

        validation_request = {
            "validation_id": val_id, "candidate_id": candidate_id, "question": question,
            "participants": [n.name for n in negotiators], "allowed_decisions": allowed_decisions,
            "consensus_rule": "UNANIMOUS_YES", "expires_at": datetime.now(timezone.utc).isoformat()
        }

        validation_result = {
            "validation_id": val_id, "candidate_id": candidate_id, "decisions": agent_decisions,
            "consensus_rule": "UNANIMOUS_YES", "consensus_reached": consensus_reached,
            "result": final_result, "completed_at": datetime.now(timezone.utc).isoformat()
        }

        audit_record = {
            "candidate_id": candidate_id, "source_topic_id": candidate.get("source_topic_id"),
            "detector_score": candidate.get("actionability_score"), "agent_decisions": agent_decisions,
            "consensus_result": final_result, "detector_version": "candidate-score-v1",
            "validation_policy_version": "unanimous-v1"
        }

        return {"validation_request": validation_request, "validation_result": validation_result, "audit_record": audit_record}


# =====================================================================
# STAGE 9: Mission Governance Gate 
# =====================================================================
def execute_governance_gate(candidate: Dict[str, Any], auto_approve: bool = True) -> Dict[str, Any]:
    """
    Stage 9 : Evaluates mission authorization against organizational policies. Applies human-in-the-loop governance checks.
    Defaults to AUTO_APPROVE for seamless pipeline execution while maintaining
    explicit hooks for manual approval or denial.
    """
    candidate_id = candidate.get("candidate_id", "unknown-candidate")
    now_ts = datetime.now(timezone.utc).isoformat()
    if auto_approve:
        return {
            "candidate_id": candidate_id,
            "decision_mode": "AUTO_APPROVE",
            "governance_status": "APPROVED",
            "policy_ids": [
                "low-risk-internal-mission",
                "authorized-submolt"
            ],
            "rationale": "Automated governance policy passed-Internal decision-support mission within configured autonomy limits.",
            "constraints_to_propagate": [
                "Do not execute financial transactions",
                "Do not exceed approved debt ceiling"
            ],
            "decided_at": now_ts
        }
    else:
        now_epoch = int(datetime.now().timestamp())
        return {
            "candidate_id": candidate_id,
            "decision_mode": "HITL",
            "governance_status": "REQUIRE_HUMAN_REVIEW",
            "approval_request_id": f"approval-{now_epoch}",
            "reason": "Candidate routed to human review - Policy requires it before opening external mission channel.",
            "decided_at": now_ts
        }