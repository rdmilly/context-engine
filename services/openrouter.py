"""OpenRouter LLM client with task-based model routing.

Routes different tasks to different models for cost optimization.
Uses tool_use for structured output.
"""

import httpx
import json
import time
from typing import Optional, Any

from config import OPENROUTER_API_KEY, OPENROUTER_BASE_URL, TASK_MODELS, LLM_BACKEND, OLLAMA_URL, OLLAMA_TASK_MODELS
from utils.logging_ import logger
from utils.degradation import get_manager as get_degradation_manager


ESCALATION_MAP = {
    "anthropic/claude-haiku-4.5": "anthropic/claude-sonnet-4.5",
    "anthropic/claude-sonnet-4.5": "anthropic/claude-opus-4",
    "meta-llama/llama-3.3-70b-instruct:free": "anthropic/claude-haiku-4.5",
    "google/gemini-2.0-flash-exp:free": "anthropic/claude-haiku-4.5",
}

TRIAGE_TOOL = {
    "type": "function",
    "function": {
        "name": "triage_result",
        "description": "Return triage decisions for session context items",
        "parameters": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "content": {"type": "string"},
                            "action": {"type": "string", "enum": ["keep", "archive", "merge", "discard"]},
                            "reason": {"type": "string"},
                            "merge_target": {"type": "string"},
                            "collection": {"type": "string"},
                        },
                        "required": ["content", "action", "reason"]
                    }
                },
                "master_context_updates": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "section": {"type": "string"},
                            "action": {"type": "string", "enum": ["update", "add", "remove"]},
                            "content": {"type": "string"},
                        },
                        "required": ["section", "action", "content"]
                    }
                }
            },
            "required": ["items", "master_context_updates"]
        }
    }
}

SUMMARY_TOOL = {
    "type": "function",
    "function": {
        "name": "session_summary",
        "description": "Return a compressed session summary for archival",
        "parameters": {
            "type": "object",
            "properties": {
                "compressed_summary": {"type": "string"},
                "key_topics": {"type": "array", "items": {"type": "string"}},
                "significance_confirmed": {"type": "string", "enum": ["low", "medium", "high"]},
                "projects_mentioned": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["compressed_summary", "key_topics", "significance_confirmed"]
        }
    }
}

MASTER_COMPRESS_TOOL = {
    "type": "function",
    "function": {
        "name": "compressed_master_context",
        "description": "Return the updated master context document",
        "parameters": {
            "type": "object",
            "properties": {
                "master_context_markdown": {"type": "string"},
                "changes_made": {"type": "array", "items": {"type": "string"}},
                "items_archived": {"type": "integer"},
                "items_kept": {"type": "integer"},
            },
            "required": ["master_context_markdown", "changes_made"]
        }
    }
}

EXTRACT_TOOL = {
    "type": "function",
    "function": {
        "name": "extracted_fields",
        "description": "Extract structured session fields from a brief note",
        "parameters": {
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "decisions": {"type": "array", "items": {"type": "string"}},
                "failures": {"type": "array", "items": {"type": "string"}},
                "files_changed": {"type": "array", "items": {"type": "string"}},
                "next_steps": {"type": "array", "items": {"type": "string"}},
                "tags": {"type": "array", "items": {"type": "string"}},
                "significance": {"type": "string", "enum": ["low", "medium", "high"]}
            },
            "required": ["summary", "tags", "significance"]
        }
    }
}

ENTITY_TOOL = {
    "type": "function",
    "function": {
        "name": "extracted_entities",
        "description": "Extract named entities from session data",
        "parameters": {
            "type": "object",
            "properties": {
                "entities": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "type": {"type": "string", "enum": ["person", "project", "service", "tool", "server", "domain", "other"]},
                            "context": {"type": "string"},
                            "relationships": {"type": "array", "items": {"type": "string"}}
                        },
                        "required": ["name", "type", "context"]
                    }
                }
            },
            "required": ["entities"]
        }
    }
}

PATTERN_TOOL = {
    "type": "function",
    "function": {
        "name": "detected_patterns",
        "description": "Detect behavioral patterns across recent sessions",
        "parameters": {
            "type": "object",
            "properties": {
                "patterns": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "pattern": {"type": "string"},
                            "frequency": {"type": "integer"},
                            "type": {"type": "string", "enum": ["recurring_topic", "work_habit", "tech_preference", "risk_pattern", "other"]},
                            "suggestion": {"type": "string"}
                        },
                        "required": ["pattern", "frequency", "type"]
                    }
                }
            },
            "required": ["patterns"]
        }
    }
}

NUDGE_TOOL = {
    "type": "function",
    "function": {
        "name": "generated_nudges",
        "description": "Generate proactive nudges based on session history",
        "parameters": {
            "type": "object",
            "properties": {
                "nudges": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "message": {"type": "string"},
                            "type": {"type": "string", "enum": ["followup", "contradiction", "stale", "risk", "opportunity", "reminder"]},
                            "priority": {"type": "string", "enum": ["low", "medium", "high"]},
                            "related_session": {"type": "string"},
                            "expires_after_days": {"type": "integer"}
                        },
                        "required": ["message", "type", "priority"]
                    }
                }
            },
            "required": ["nudges"]
        }
    }
}

ANOMALY_TOOL = {
    "type": "function",
    "function": {
        "name": "detected_anomalies",
        "description": "Flag anomalies between new session data and established context",
        "parameters": {
            "type": "object",
            "properties": {
                "anomalies": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "description": {"type": "string"},
                            "type": {"type": "string", "enum": ["contradiction", "regression", "drift", "inconsistency", "escalation"]},
                            "severity": {"type": "string", "enum": ["low", "medium", "high", "critical"]},
                            "evidence": {"type": "string"},
                            "expires_after_days": {"type": "integer"},
                        },
                        "required": ["description", "type", "severity", "evidence"]
                    }
                }
            },
            "required": ["anomalies"]
        }
    }
}


class OpenRouterClient:
    """OpenRouter API client with model routing and escalation."""

    def __init__(self):
        self.backend = LLM_BACKEND
        self.api_key = OPENROUTER_API_KEY
        self.base_url = OPENROUTER_BASE_URL
        self.ollama_url = OLLAMA_URL
        self.client = httpx.Client(timeout=60.0)
        self._call_count = 0
        self._total_cost = 0.0
        logger.info(f"OpenRouterClient: backend={self.backend}")

    def _get_model(self, task: str) -> str:
        return TASK_MODELS.get(task, "meta-llama/llama-3.3-70b-instruct:free")

    def _call(self, model: str, messages: list, tools: list = None, tool_choice: dict = None) -> dict:
        dm = get_degradation_manager()
        if not dm.can_call("openrouter"):
            logger.warning("LLM circuit breaker OPEN")
            dm.mark_unhealthy("openrouter", "circuit breaker open")
            return {}

        if self.backend == "ollama":
            return self._call_ollama(model, messages, tools, tool_choice)

        if not self.api_key or self.api_key.startswith("placeholder"):
            raise RuntimeError("OpenRouter API key not configured")

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://millyweb.com",
            "X-Title": "ContextEngine",
        }
        payload = {"model": model, "messages": messages, "max_tokens": 4096}
        if tools:
            payload["tools"] = tools
        if tool_choice:
            payload["tool_choice"] = tool_choice

        try:
            response = self.client.post(f"{self.base_url}/chat/completions", headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()
            self._call_count += 1
            dm.mark_healthy("openrouter")
            usage = data.get("usage", {})
            if usage:
                logger.info(f"OpenRouter [{model}]: {usage.get('prompt_tokens', 0)}in/{usage.get('completion_tokens', 0)}out")
            return data
        except httpx.HTTPStatusError as e:
            logger.error(f"OpenRouter HTTP error: {e.response.status_code} {e.response.text[:200]}")
            dm.mark_unhealthy("openrouter", f"HTTP {e.response.status_code}")
            raise
        except Exception as e:
            logger.error(f"OpenRouter error: {e}")
            raise

    def _call_ollama(self, model: str, messages: list, tools: list = None, tool_choice: dict = None) -> dict:
        dm = get_degradation_manager()
        payload = {"model": model, "messages": messages, "stream": False}
        if tools:
            payload["tools"] = tools
        if tool_choice:
            payload["tool_choice"] = tool_choice
        try:
            response = self.client.post(f"{self.ollama_url}/v1/chat/completions", json=payload)
            response.raise_for_status()
            data = response.json()
            self._call_count += 1
            dm.mark_healthy("openrouter")
            return data
        except httpx.HTTPStatusError as e:
            dm.mark_unhealthy("openrouter", f"Ollama HTTP {e.response.status_code}")
            raise
        except httpx.ConnectError as e:
            dm.mark_unhealthy("openrouter", "Ollama unreachable")
            raise
        except Exception as e:
            dm.mark_unhealthy("openrouter", str(e))
            raise

    def _extract_tool_call(self, response: dict) -> Optional[dict]:
        choices = response.get("choices", [])
        if not choices:
            return None
        message = choices[0].get("message", {})
        tool_calls = message.get("tool_calls", [])
        if not tool_calls:
            content = message.get("content", "")
            if content:
                try:
                    return json.loads(content)
                except json.JSONDecodeError:
                    return None
            return None
        args_str = tool_calls[0].get("function", {}).get("arguments", "{}")
        try:
            return json.loads(args_str)
        except json.JSONDecodeError:
            logger.error(f"Failed to parse tool call args: {args_str[:200]}")
            return None

    def _needs_escalation(self, result: Optional[dict]) -> bool:
        if result is None:
            return True
        if isinstance(result, dict):
            for key, val in result.items():
                if isinstance(val, str) and any(h in val.lower() for h in ["i'm not sure", "unclear", "cannot determine", "n/a"]):
                    return True
                if isinstance(val, list) and len(val) == 0 and key in ["items", "master_context_updates"]:
                    return True
        return False

    def call_with_escalation(self, task: str, messages: list, tools: list = None, tool_choice: dict = None) -> Optional[dict]:
        model = self._get_model(task)
        logger.info(f"Worker LLM call: task={task}, model={model}")
        try:
            response = self._call(model, messages, tools, tool_choice)
            result = self._extract_tool_call(response)
            if self._needs_escalation(result) and model in ESCALATION_MAP:
                escalated_model = ESCALATION_MAP[model]
                logger.info(f"Escalating {task}: {model} -> {escalated_model}")
                escalation_messages = messages.copy()
                if result:
                    escalation_messages.append({"role": "assistant", "content": f"Previous attempt: {json.dumps(result)}"})
                    escalation_messages.append({"role": "user", "content": "The previous attempt was incomplete. Please provide a thorough response."})
                response = self._call(escalated_model, escalation_messages, tools, tool_choice)
                result = self._extract_tool_call(response)
            return result
        except Exception as e:
            logger.error(f"LLM call failed for task {task}: {e}")
            return None

    def extract_entities(self, session_data: dict) -> Optional[dict]:
        messages = [{"role": "user", "content": f"Extract named entities from this session. Include people, projects, services, tools, servers, and domains.\n\nSession data:\n{json.dumps(session_data, indent=2, default=str)[:3000]}\n\nUse the extracted_entities tool."}]
        return self.call_with_escalation("entity_extraction", messages, tools=[ENTITY_TOOL], tool_choice={"type": "function", "function": {"name": "extracted_entities"}})

    def detect_patterns(self, recent_sessions: list) -> Optional[dict]:
        session_summaries = [{"id": s.get("id", ""), "content": s.get("content", "")[:300], "metadata": {k: v for k, v in s.get("metadata", {}).items() if k in ["tags", "timestamp", "significance"]}} for s in recent_sessions[:10]]
        messages = [{"role": "user", "content": f"Analyze these recent sessions for behavioral patterns (recurring topics, work habits, tech preferences, risk patterns). Only report patterns in 3+ sessions.\n\n{json.dumps(session_summaries, indent=2, default=str)}\n\nUse the detected_patterns tool."}]
        return self.call_with_escalation("pattern_analysis", messages, tools=[PATTERN_TOOL], tool_choice={"type": "function", "function": {"name": "detected_patterns"}})

    def extract_session_fields(self, note: str) -> Optional[dict]:
        messages = [{"role": "user", "content": f"Extract structured session information from this note. Expand into a proper summary and extract decisions, failures, files, next steps, tags.\n\nNote: {note}\n\nUse the extracted_fields tool."}]
        return self.call_with_escalation("session_summary", messages, tools=[EXTRACT_TOOL], tool_choice={"type": "function", "function": {"name": "extracted_fields"}})

    def summarize_session(self, session_data: dict) -> Optional[dict]:
        messages = [{"role": "user", "content": f"Compress this session into a concise archival summary.\n\n{json.dumps(session_data, indent=2, default=str)}\n\nUse the session_summary tool."}]
        return self.call_with_escalation("session_summary", messages, tools=[SUMMARY_TOOL], tool_choice={"type": "function", "function": {"name": "session_summary"}})

    def triage_session(self, session_data: dict, current_master: str) -> Optional[dict]:
        messages = [{"role": "user", "content": f"Analyze this session and decide what to keep/archive/merge/discard.\n\nCurrent master context:\n{current_master}\n\nNew session:\n{json.dumps(session_data, indent=2, default=str)}\n\nRules: KEEP=hot context, ARCHIVE=historical, MERGE=updates existing, DISCARD=trivial (prefer archive over discard).\nUse the triage_result tool."}]
        return self.call_with_escalation("triage", messages, tools=[TRIAGE_TOOL], tool_choice={"type": "function", "function": {"name": "triage_result"}})

    def compress_master_context(self, current_master: str, triage_result: dict, session_data: dict) -> Optional[dict]:
        messages = [{"role": "user", "content": f"Update the master context based on triage decisions.\n\nCurrent:\n{current_master}\n\nTriage:\n{json.dumps(triage_result, indent=2, default=str)}\n\nSession:\n{json.dumps(session_data, indent=2, default=str)}\n\nKeep concise and actionable. Use compressed_master_context tool."}]
        return self.call_with_escalation("master_compression", messages, tools=[MASTER_COMPRESS_TOOL], tool_choice={"type": "function", "function": {"name": "compressed_master_context"}})

    def extract_from_transcript(self, transcript: str, note: str) -> Optional[dict]:
        from config import MAX_TRANSCRIPT_CHARS
        if len(transcript) > MAX_TRANSCRIPT_CHARS:
            half = MAX_TRANSCRIPT_CHARS // 2
            transcript = transcript[:half] + "\n\n[...TRUNCATED...]\n\n" + transcript[-half:]
        messages = [{"role": "user", "content": f"Extract structured information from this conversation transcript.\n\nUser note: {note}\n\nTranscript:\n---\n{transcript}\n---\n\nExtract summary, decisions, failures, files_changed, next_steps, tags, significance. Use the extracted_fields tool."}]
        return self.call_with_escalation("session_summary", messages, tools=[EXTRACT_TOOL], tool_choice={"type": "function", "function": {"name": "extracted_fields"}})

    def generate_nudges(self, master_context, recent_sessions, patterns=None, failures=None):
        session_briefs = [{"content": s.get("content", "")[:300], "metadata": {k: v for k, v in s.get("metadata", {}).items() if k in ["tags", "timestamp", "significance", "session_id"]}} for s in recent_sessions[:10]]
        parts = ["Generate proactive nudges based on user state and recent history.", f"Master context:\n{master_context[:4000]}", f"Recent sessions:\n{json.dumps(session_briefs, indent=2, default=str)}"]
        if patterns:
            parts.append(f"Patterns:\n{json.dumps(patterns[:5], indent=2, default=str)}")
        if failures:
            parts.append(f"Failures:\n{json.dumps(failures[:5], indent=2, default=str)}")
        parts.append("Generate 0-5 nudges: followup, contradiction, stale, risk, opportunity, reminder. Quality over quantity. Use generated_nudges tool.")
        messages = [{"role": "user", "content": "\n\n".join(parts)}]
        result = self.call_with_escalation("nudge_generation", messages, tools=[NUDGE_TOOL], tool_choice={"type": "function", "function": {"name": "generated_nudges"}})
        return result.get("nudges", []) if result else []

    def detect_anomalies(self, session_data: dict, master_context: str, recent_decisions: list = None, recent_failures: list = None):
        session_brief = {"summary": session_data.get("summary", ""), "decisions": session_data.get("decisions", []), "failures": session_data.get("failures", []), "tags": session_data.get("tags", []), "files_changed": session_data.get("files_changed", [])}
        parts = ["Compare new session against master context and flag anomalies.", f"MASTER CONTEXT:\n{master_context[:4000]}", f"NEW SESSION:\n{json.dumps(session_brief, indent=2, default=str)}"]
        if recent_decisions:
            parts.append(f"RECENT DECISIONS:\n{json.dumps(recent_decisions[:10], indent=2, default=str)}")
        if recent_failures:
            parts.append(f"KNOWN FAILURES:\n{json.dumps(recent_failures[:10], indent=2, default=str)}")
        parts.append("Flag: contradiction, regression, drift, inconsistency, escalation. Be conservative. Use detected_anomalies tool.")
        messages = [{"role": "user", "content": "\n\n".join(parts)}]
        result = self.call_with_escalation("anomaly_detection", messages, tools=[ANOMALY_TOOL], tool_choice={"type": "function", "function": {"name": "detected_anomalies"}})
        return result.get("anomalies", []) if result else []

    @property
    def stats(self) -> dict:
        return {"calls": self._call_count, "estimated_cost": self._total_cost, "backend": self.backend}


_client: Optional[OpenRouterClient] = None

def get_client() -> OpenRouterClient:
    global _client
    if _client is None:
        _client = OpenRouterClient()
    return _client

get_openrouter = get_client
