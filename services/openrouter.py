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
        "description": "Flag anomalies between session data and established context",
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
    def __init__(self):
        self.backend = LLM_BACKEND
        self.api_key = OPENROUTER_API_KEY
        self.base_url = OPENROUTER_BASE_URL
        self.ollama_url = OLLAMA_URL
        self.client = httpx.Client(timeout=60.0)
        self._call_count = 0
        self._total_cost = 0.0

    def _get_model(self, task: str) -> str:
        return TASK_MODELS.get(task, "meta-llama/llama-3.3-70b-instruct:free")

    def _call(self, model, messages, tools=None, tool_choice=None):
        dm = get_degradation_manager()
        if not dm.can_call("openrouter"):
            dm.mark_unhealthy("openrouter", "circuit breaker open")
            return {}
        if self.backend == "ollama":
            return self._call_ollama(model, messages, tools, tool_choice)
        if not self.api_key or self.api_key.startswith("placeholder"):
            raise RuntimeError("OpenRouter API key not configured")
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json",
                   "HTTP-Referer": "https://millyweb.com", "X-Title": "ContextEngine"}
        payload = {"model": model, "messages": messages, "max_tokens": 4096}
        if tools: payload["tools"] = tools
        if tool_choice: payload["tool_choice"] = tool_choice
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
            logger.error(f"OpenRouter HTTP error: {e.response.status_code}")
            dm.mark_unhealthy("openrouter", f"HTTP {e.response.status_code}")
            raise
        except Exception as e:
            logger.error(f"OpenRouter error: {e}")
            raise

    def _call_ollama(self, model, messages, tools=None, tool_choice=None):
        dm = get_degradation_manager()
        payload = {"model": model, "messages": messages, "stream": False}
        if tools: payload["tools"] = tools
        if tool_choice: payload["tool_choice"] = tool_choice
        try:
            response = self.client.post(f"{self.ollama_url}/v1/chat/completions", json=payload)
            response.raise_for_status()
            data = response.json()
            self._call_count += 1
            dm.mark_healthy("openrouter")
            return data
        except Exception as e:
            dm.mark_unhealthy("openrouter", str(e))
            raise

    def _extract_tool_call(self, response):
        choices = response.get("choices", [])
        if not choices: return None
        message = choices[0].get("message", {})
        tool_calls = message.get("tool_calls", [])
        if not tool_calls:
            content = message.get("content", "")
            if content:
                try: return json.loads(content)
                except json.JSONDecodeError: return None
            return None
        args_str = tool_calls[0].get("function", {}).get("arguments", "{}")
        try: return json.loads(args_str)
        except json.JSONDecodeError: return None

    def _needs_escalation(self, result):
        if result is None: return True
        if isinstance(result, dict):
            for key, val in result.items():
                if isinstance(val, str) and any(h in val.lower() for h in ["i'm not sure", "unclear", "cannot determine"]):
                    return True
                if isinstance(val, list) and len(val) == 0 and key in ["items", "master_context_updates"]:
                    return True
        return False

    def call_with_escalation(self, task, messages, tools=None, tool_choice=None):
        model = self._get_model(task)
        logger.info(f"Worker LLM call: task={task}, model={model}")
        try:
            response = self._call(model, messages, tools, tool_choice)
            result = self._extract_tool_call(response)
            if self._needs_escalation(result) and model in ESCALATION_MAP:
                escalated = ESCALATION_MAP[model]
                logger.info(f"Escalating {task}: {model} -> {escalated}")
                esc_messages = messages.copy()
                if result:
                    esc_messages.append({"role": "assistant", "content": f"Previous attempt: {json.dumps(result)}"})
                    esc_messages.append({"role": "user", "content": "Previous attempt was incomplete. Please provide a thorough response."})
                response = self._call(escalated, esc_messages, tools, tool_choice)
                result = self._extract_tool_call(response)
            return result
        except Exception as e:
            logger.error(f"LLM call failed for {task}: {e}")
            return None

    def extract_entities(self, session_data):
        messages = [{"role": "user", "content": f"Extract named entities from this session.\n\nSession data:\n{json.dumps(session_data, indent=2, default=str)[:3000]}\n\nUse the extracted_entities tool."}]
        return self.call_with_escalation("entity_extraction", messages, tools=[ENTITY_TOOL], tool_choice={"type": "function", "function": {"name": "extracted_entities"}})

    def detect_patterns(self, recent_sessions):
        briefs = [{"id": s.get("id", ""), "content": s.get("content", "")[:300], "metadata": {k: v for k, v in s.get("metadata", {}).items() if k in ["tags", "timestamp", "significance"]}} for s in recent_sessions[:10]]
        messages = [{"role": "user", "content": f"Analyze recent sessions for behavioral patterns (3+ occurrences).\n\n{json.dumps(briefs, indent=2, default=str)}\n\nUse detected_patterns tool."}]
        return self.call_with_escalation("pattern_analysis", messages, tools=[PATTERN_TOOL], tool_choice={"type": "function", "function": {"name": "detected_patterns"}})

    def extract_session_fields(self, note):
        messages = [{"role": "user", "content": f"Extract structured session info from this note:\n\n{note}\n\nUse extracted_fields tool."}]
        return self.call_with_escalation("session_summary", messages, tools=[EXTRACT_TOOL], tool_choice={"type": "function", "function": {"name": "extracted_fields"}})

    def summarize_session(self, session_data):
        messages = [{"role": "user", "content": f"Compress this session for archival:\n\n{json.dumps(session_data, indent=2, default=str)}\n\nUse session_summary tool."}]
        return self.call_with_escalation("session_summary", messages, tools=[SUMMARY_TOOL], tool_choice={"type": "function", "function": {"name": "session_summary"}})

    def triage_session(self, session_data, current_master):
        messages = [{"role": "user", "content": f"Triage session content (keep/archive/merge/discard).\n\nCurrent master:\n{current_master}\n\nNew session:\n{json.dumps(session_data, indent=2, default=str)}\n\nUse triage_result tool."}]
        return self.call_with_escalation("triage", messages, tools=[TRIAGE_TOOL], tool_choice={"type": "function", "function": {"name": "triage_result"}})

    def compress_master_context(self, current_master, triage_result, session_data):
        messages = [{"role": "user", "content": f"Update master context based on triage.\n\nCurrent:\n{current_master}\n\nTriage:\n{json.dumps(triage_result, indent=2, default=str)}\n\nSession:\n{json.dumps(session_data, indent=2, default=str)}\n\nUse compressed_master_context tool."}]
        return self.call_with_escalation("master_compression", messages, tools=[MASTER_COMPRESS_TOOL], tool_choice={"type": "function", "function": {"name": "compressed_master_context"}})

    def extract_from_transcript(self, transcript, note):
        from config import MAX_TRANSCRIPT_CHARS
        if len(transcript) > MAX_TRANSCRIPT_CHARS:
            half = MAX_TRANSCRIPT_CHARS // 2
            transcript = transcript[:half] + "\n\n[...TRUNCATED...]\n\n" + transcript[-half:]
        messages = [{"role": "user", "content": f"Extract structured info from this transcript.\n\nUser note: {note}\n\nTranscript:\n---\n{transcript}\n---\n\nUse extracted_fields tool."}]
        return self.call_with_escalation("session_summary", messages, tools=[EXTRACT_TOOL], tool_choice={"type": "function", "function": {"name": "extracted_fields"}})

    def generate_nudges(self, master_context, recent_sessions, patterns=None, failures=None):
        briefs = [{'content': s.get('content', '')[:300], 'metadata': {k: v for k, v in s.get('metadata', {}).items() if k in ['tags', 'timestamp', 'significance', 'session_id']}} for s in recent_sessions[:10]]
        parts = ['Generate proactive nudges based on current state and history.', f'Master context:\n{master_context[:4000]}', f'Recent sessions:\n{json.dumps(briefs, indent=2, default=str)}']
        if patterns: parts.append(f'Patterns:\n{json.dumps(patterns[:5], indent=2, default=str)}')
        if failures: parts.append(f'Failures:\n{json.dumps(failures[:5], indent=2, default=str)}')
        parts.append('Generate 0-5 useful nudges. Use generated_nudges tool.')
        messages = [{'role': 'user', 'content': chr(10).join(parts)}]
        result = self.call_with_escalation('nudge_generation', messages, tools=[NUDGE_TOOL], tool_choice={'type': 'function', 'function': {'name': 'generated_nudges'}})
        return result.get('nudges', []) if result else []

    def detect_anomalies(self, session_data, master_context, recent_decisions=None, recent_failures=None):
        session_brief = {"summary": session_data.get("summary", ""), "decisions": session_data.get("decisions", []), "failures": session_data.get("failures", []), "tags": session_data.get("tags", [])}
        parts = [f"Compare session against master context for anomalies.\n\nMASTER:\n{master_context[:4000]}\n\nSESSION:\n{json.dumps(session_brief, indent=2, default=str)}"]
        if recent_decisions: parts.append(f"Recent decisions:\n{json.dumps(recent_decisions[:10], indent=2, default=str)}")
        if recent_failures: parts.append(f"Known failures:\n{json.dumps(recent_failures[:10], indent=2, default=str)}")
        parts.append("Flag genuine anomalies only. Use detected_anomalies tool.")
        messages = [{"role": "user", "content": chr(10).join(parts)}]
        result = self.call_with_escalation("anomaly_detection", messages, tools=[ANOMALY_TOOL], tool_choice={"type": "function", "function": {"name": "detected_anomalies"}})
        return result.get("anomalies", []) if result else []

    @property
    def stats(self):
        return {"calls": self._call_count, "estimated_cost": self._total_cost, "backend": self.backend}


_client: Optional[OpenRouterClient] = None

def get_client() -> OpenRouterClient:
    global _client
    if _client is None:
        _client = OpenRouterClient()
    return _client

get_openrouter = get_client
