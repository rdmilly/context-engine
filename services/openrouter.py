"""OpenRouter LLM client with task-based model routing.

Routes different tasks to different models for cost optimization.
Uses tool_use for structured output — no JSON parsing needed.
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

# Tool definitions for structured output
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
                            "content": {"type": "string", "description": "The content being triaged"},
                            "action": {"type": "string", "enum": ["keep", "archive", "merge", "discard"]},
                            "reason": {"type": "string", "description": "Brief explanation"},
                            "merge_target": {"type": "string", "description": "If merge, what to merge with"},
                            "collection": {"type": "string", "description": "Target ChromaDB collection for archive"},
                        },
                        "required": ["content", "action", "reason"]
                    }
                },
                "master_context_updates": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "section": {"type": "string", "description": "Which section to update"},
                            "action": {"type": "string", "enum": ["update", "add", "remove"]},
                            "content": {"type": "string", "description": "New content for this section"},
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
                "compressed_summary": {"type": "string", "description": "2-4 sentence compressed summary"},
                "key_topics": {"type": "array", "items": {"type": "string"}, "description": "Main topics covered"},
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
                "master_context_markdown": {"type": "string", "description": "Complete updated master-context.md content in markdown"},
                "changes_made": {"type": "array", "items": {"type": "string"}, "description": "List of changes made"},
                "items_archived": {"type": "integer", "description": "Number of items moved to archive"},
                "items_kept": {"type": "integer", "description": "Number of items kept in hot"},
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
                "summary": {"type": "string", "description": "Expanded 2-4 sentence summary of the session"},
                "decisions": {"type": "array", "items": {"type": "string"}, "description": "Key decisions made"},
                "failures": {"type": "array", "items": {"type": "string"}, "description": "Things that broke or did not work"},
                "files_changed": {"type": "array", "items": {"type": "string"}, "description": "Files created or modified"},
                "next_steps": {"type": "array", "items": {"type": "string"}, "description": "What to do next"},
                "tags": {"type": "array", "items": {"type": "string"}, "description": "Short tags for categorization"},
                "significance": {"type": "string", "enum": ["low", "medium", "high"], "description": "Session significance"}
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
                            "name": {"type": "string", "description": "Entity name"},
                            "type": {"type": "string", "enum": ["person", "project", "service", "tool", "server", "domain", "other"]},
                            "context": {"type": "string", "description": "Brief context about this entity"},
                            "relationships": {"type": "array", "items": {"type": "string"}, "description": "Related entities"}
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
                            "pattern": {"type": "string", "description": "Description of the pattern"},
                            "frequency": {"type": "integer", "description": "How many sessions show this pattern"},
                            "type": {"type": "string", "enum": ["recurring_topic", "work_habit", "tech_preference", "risk_pattern", "other"]},
                            "suggestion": {"type": "string", "description": "Actionable suggestion based on pattern"}
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
        "description": "Generate proactive nudges based on session history and current state",
        "parameters": {
            "type": "object",
            "properties": {
                "nudges": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "message": {"type": "string", "description": "The nudge message to show the user"},
                            "type": {"type": "string", "enum": ["followup", "contradiction", "stale", "risk", "opportunity", "reminder"]},
                            "priority": {"type": "string", "enum": ["low", "medium", "high"]},
                            "related_session": {"type": "string", "description": "Session ID that triggered this nudge, if any"},
                            "expires_after_days": {"type": "integer", "description": "Days until this nudge is no longer relevant"}
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
                            "description": {"type": "string", "description": "Clear description of the anomaly"},
                            "type": {"type": "string", "enum": ["contradiction", "regression", "drift", "inconsistency", "escalation"]},
                            "severity": {"type": "string", "enum": ["low", "medium", "high", "critical"]},
                            "evidence": {"type": "string", "description": "Specific evidence from session vs master context"},
                            "expires_after_days": {"type": "integer", "description": "Days until anomaly expires (default 14)"},
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
        """Get the model for a given task."""
        return TASK_MODELS.get(task, "meta-llama/llama-3.3-70b-instruct:free")

    def _call(self, model: str, messages: list, tools: list = None, tool_choice: dict = None) -> dict:
        """Make a raw API call to OpenRouter."""
        dm = get_degradation_manager()
        if not dm.can_call("openrouter"):
            logger.warning("LLM circuit breaker OPEN — skipping call")
            dm.mark_unhealthy("openrouter", "circuit breaker open")
            return {}

        # Ollama backend — different URL, no auth
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

        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": 4096,
        }
        if tools:
            payload["tools"] = tools
        if tool_choice:
            payload["tool_choice"] = tool_choice

        try:
            response = self.client.post(
                f"{self.base_url}/chat/completions",
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
            self._call_count += 1
            dm.mark_healthy("openrouter")

            # Track cost if available
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
        """Make an API call to Ollama's OpenAI-compatible endpoint."""
        dm = get_degradation_manager()

        payload = {
            "model": model,
            "messages": messages,
            "stream": False,
        }
        if tools:
            payload["tools"] = tools
        if tool_choice:
            payload["tool_choice"] = tool_choice

        try:
            response = self.client.post(
                f"{self.ollama_url}/v1/chat/completions",
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
            self._call_count += 1
            dm.mark_healthy("openrouter")

            logger.info(f"Ollama [{model}]: {data.get('usage', {}).get('prompt_tokens', '?')}in/{data.get('usage', {}).get('completion_tokens', '?')}out")
            return data
        except httpx.HTTPStatusError as e:
            logger.error(f"Ollama HTTP error: {e.response.status_code}")
            dm.mark_unhealthy("openrouter", f"Ollama HTTP {e.response.status_code}")
            raise
        except httpx.ConnectError as e:
            logger.error(f"Ollama connection failed: {e}")
            dm.mark_unhealthy("openrouter", "Ollama unreachable")
            raise
        except Exception as e:
            logger.error(f"Ollama call failed: {e}")
            dm.mark_unhealthy("openrouter", str(e))
            raise

    def _extract_tool_call(self, response: dict) -> Optional[dict]:
        """Extract tool call arguments from response."""
        choices = response.get("choices", [])
        if not choices:
            return None

        message = choices[0].get("message", {})
        tool_calls = message.get("tool_calls", [])
        if not tool_calls:
            # Fallback: try to parse content as JSON
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
        """Check if result quality needs escalation to a better model."""
        if result is None:
            return True
        # Check for hedging/empty fields
        if isinstance(result, dict):
            for key, val in result.items():
                if isinstance(val, str) and any(h in val.lower() for h in ["i'm not sure", "unclear", "cannot determine", "n/a"]):
                    return True
                if isinstance(val, list) and len(val) == 0 and key in ["items", "master_context_updates"]:
                    return True
        return False

    def call_with_escalation(
        self, task: str, messages: list, tools: list = None, tool_choice: dict = None
    ) -> Optional[dict]:
        """Call OpenRouter with automatic escalation on low-quality results."""
        model = self._get_model(task)
        logger.info(f"Worker LLM call: task={task}, model={model}")

        try:
            response = self._call(model, messages, tools, tool_choice)
            result = self._extract_tool_call(response)

            if self._needs_escalation(result) and model in ESCALATION_MAP:
                escalated_model = ESCALATION_MAP[model]
                logger.info(f"Escalating {task}: {model} -> {escalated_model}")

                # Include previous attempt in escalation prompt
                escalation_messages = messages.copy()
                if result:
                    escalation_messages.append({
                        "role": "assistant",
                        "content": f"Previous attempt (needs improvement): {json.dumps(result)}"
                    })
                    escalation_messages.append({
                        "role": "user",
                        "content": "The previous attempt was incomplete or uncertain. Please provide a more thorough and confident response."
                    })

                response = self._call(escalated_model, escalation_messages, tools, tool_choice)
                result = self._extract_tool_call(response)

            return result

        except Exception as e:
            logger.error(f"LLM call failed for task {task}: {e}")
            return None



    def extract_entities(self, session_data: dict) -> Optional[dict]:
        """Extract named entities from session data using Haiku."""
        messages = [{
            "role": "user",
            "content": f"""Extract named entities from this session. Include people, projects, services, tools, servers, and domains.
Only extract entities that are specifically mentioned, not generic concepts.

Session data:
{json.dumps(session_data, indent=2, default=str)[:3000]}

Use the extracted_entities tool to return your result."""
        }]
        return self.call_with_escalation(
            "entity_extraction", messages,
            tools=[ENTITY_TOOL],
            tool_choice={"type": "function", "function": {"name": "extracted_entities"}}
        )

    def detect_patterns(self, recent_sessions: list) -> Optional[dict]:
        """Detect behavioral patterns across recent sessions."""
        session_summaries = []
        for s in recent_sessions[:10]:
            session_summaries.append({
                "id": s.get("id", ""),
                "content": s.get("content", "")[:300],
                "metadata": {k: v for k, v in s.get("metadata", {}).items() if k in ["tags", "timestamp", "significance"]}
            })

        messages = [{
            "role": "user",
            "content": f"""Analyze these recent sessions for behavioral patterns. Look for:
- Recurring topics or technologies
- Work habits (time patterns, session types)
- Technology preferences
- Risk patterns (repeated failures, recurring issues)

Recent sessions:
{json.dumps(session_summaries, indent=2, default=str)}

Only report patterns that appear in 3+ sessions. Use the detected_patterns tool."""
        }]
        return self.call_with_escalation(
            "pattern_analysis", messages,
            tools=[PATTERN_TOOL],
            tool_choice={"type": "function", "function": {"name": "detected_patterns"}}
        )

    def extract_session_fields(self, note: str) -> Optional[dict]:
        """Extract structured fields from a brief session note using Haiku."""
        messages = [{
            "role": "user",
            "content": f"""Extract structured session information from this brief note.
Expand the note into a proper summary and extract any decisions, failures, files changed, next steps, and tags.
If information is not mentioned, return empty arrays.

Note: {note}

Use the extracted_fields tool to return your result."""
        }]
        return self.call_with_escalation(
            "session_summary", messages,
            tools=[EXTRACT_TOOL],
            tool_choice={"type": "function", "function": {"name": "extracted_fields"}}
        )

    def summarize_session(self, session_data: dict) -> Optional[dict]:
        """Compress a session into a summary for archival."""
        messages = [{
            "role": "user",
            "content": f"""Compress this session into a concise archival summary.

Session data:
{json.dumps(session_data, indent=2, default=str)}

Use the session_summary tool to return your result."""
        }]
        return self.call_with_escalation(
            "session_summary", messages,
            tools=[SUMMARY_TOOL],
            tool_choice={"type": "function", "function": {"name": "session_summary"}}
        )

    def triage_session(self, session_data: dict, current_master: str) -> Optional[dict]:
        """Triage session content: keep/archive/merge/discard."""
        messages = [{
            "role": "user",
            "content": f"""You are a context management system. Analyze this session and decide what happens to each piece of information.

Current master context (hot):
{current_master}

New session data:
{json.dumps(session_data, indent=2, default=str)}

Rules:
- KEEP: Information that should stay in or be added to the hot master context (active projects, current state, recent decisions)
- ARCHIVE: Historical info worth preserving but not needed in every session (completed work, past decisions)
- MERGE: Info that updates/replaces something already in master context
- DISCARD: Trivial, redundant, or ephemeral info (only in learning mode, never discard — use archive instead)

Also specify what sections of master context should be updated.
Use the triage_result tool to return your decisions."""
        }]
        return self.call_with_escalation(
            "triage", messages,
            tools=[TRIAGE_TOOL],
            tool_choice={"type": "function", "function": {"name": "triage_result"}}
        )

    def compress_master_context(self, current_master: str, triage_result: dict, session_data: dict) -> Optional[dict]:
        """Rewrite master context incorporating triage decisions."""
        messages = [{
            "role": "user",
            "content": f"""You are a context management system. Update the master context document based on the triage decisions and new session data.

Current master context:
{current_master}

Triage decisions:
{json.dumps(triage_result, indent=2, default=str)}

New session data:
{json.dumps(session_data, indent=2, default=str)}

Rules:
- Keep the document concise and actionable
- Update project states, decisions, and next steps
- Remove completed items that have been archived
- Add any new active projects or blockers
- Maintain the existing section structure
- The document should fit in ~500 tokens when summarized

Use the compressed_master_context tool to return the updated document."""
        }]
        return self.call_with_escalation(
            "master_compression", messages,
            tools=[MASTER_COMPRESS_TOOL],
            tool_choice={"type": "function", "function": {"name": "compressed_master_context"}}
        )


    def extract_from_transcript(self, transcript: str, note: str) -> Optional[dict]:
        """Extract structured fields from a conversation transcript using Haiku.
        
        Richer version of extract_session_fields — Haiku gets the full conversation
        and produces accurate summary, decisions, files, etc.
        
        Cost: ~$0.04 per call (40K input + 1K output at Haiku rates)
        """
        from config import MAX_TRANSCRIPT_CHARS
        if len(transcript) > MAX_TRANSCRIPT_CHARS:
            half = MAX_TRANSCRIPT_CHARS // 2
            transcript = transcript[:half] + "\n\n[...TRUNCATED...]\n\n" + transcript[-half:]
        
        messages = [{
            "role": "user",
            "content": f"""You are a session summarizer. Extract structured information from this conversation transcript.

User's note about the session: {note}

Full conversation transcript:
---
{transcript}
---

Extract:
- A concise 2-4 sentence summary of what was accomplished
- Key decisions made (with rationale if clear)
- Any failures or things that broke
- Files created or modified (full paths)
- Next steps or TODO items mentioned
- Short categorization tags
- Overall significance (low=quick chat, medium=standard work, high=major decisions/infrastructure changes)

Use the extracted_fields tool to return your result."""
        }]
        return self.call_with_escalation(
            "session_summary", messages,
            tools=[EXTRACT_TOOL],
            tool_choice={"type": "function", "function": {"name": "extracted_fields"}}
        )

    def generate_nudges(self, master_context, recent_sessions, patterns=None, failures=None):
        """Generate proactive nudges based on current state and history."""
        session_briefs = []
        for s in recent_sessions[:10]:
            session_briefs.append({
                'content': s.get('content', '')[:300],
                'metadata': {k: v for k, v in s.get('metadata', {}).items() 
                             if k in ['tags', 'timestamp', 'significance', 'session_id']}
            })

        parts = []
        parts.append('You are a proactive context assistant. Generate useful nudges based on the user state and recent work history.')
        parts.append('Current master context:')
        parts.append(master_context[:4000])
        parts.append('Recent sessions (last 10):')
        parts.append(json.dumps(session_briefs, indent=2, default=str))

        if patterns:
            parts.append('Detected patterns:')
            parts.append(json.dumps(patterns[:5], indent=2, default=str))

        if failures:
            parts.append('Recent failures:')
            parts.append(json.dumps(failures[:5], indent=2, default=str))

        parts.append("""Generate nudges for things like:
- FOLLOWUP: Next steps mentioned in recent sessions that have not been addressed
- CONTRADICTION: Decisions that conflict with each other
- STALE: Projects or tasks in master context with no recent activity
- RISK: Recurring failures or unresolved blockers
- OPPORTUNITY: Patterns suggesting potential improvements
- REMINDER: Important deadlines or commitments mentioned

Only generate nudges that are genuinely useful. Quality over quantity - 0-5 nudges max.
If everything looks good, return an empty nudges array.
Use the generated_nudges tool to return your result.""")

        messages = [{'role': 'user', 'content': chr(10).join(parts)}]

        result = self.call_with_escalation(
            'nudge_generation', messages,
            tools=[NUDGE_TOOL],
            tool_choice={'type': 'function', 'function': {'name': 'generated_nudges'}}
        )

        if result and result.get('nudges'):
            return result['nudges']
        return []


    def detect_anomalies(self, session_data: dict, master_context: str, recent_decisions: list = None, recent_failures: list = None):
        """Detect anomalies between session data and established context."""
        parts = []
        parts.append("You are a context integrity checker. Compare the new session data against the established master context and flag any anomalies.")
        parts.append("")
        parts.append("MASTER CONTEXT (established truth):")
        parts.append(master_context[:4000])
        parts.append("")
        parts.append("NEW SESSION DATA:")
        session_brief = {
            "summary": session_data.get("summary", ""),
            "decisions": session_data.get("decisions", []),
            "failures": session_data.get("failures", []),
            "tags": session_data.get("tags", []),
            "files_changed": session_data.get("files_changed", []),
        }
        parts.append(json.dumps(session_brief, indent=2, default=str))

        if recent_decisions:
            parts.append("")
            parts.append("RECENT DECISIONS (last 10):")
            parts.append(json.dumps(recent_decisions[:10], indent=2, default=str))

        if recent_failures:
            parts.append("")
            parts.append("KNOWN RESOLVED FAILURES:")
            parts.append(json.dumps(recent_failures[:10], indent=2, default=str))

        parts.append("")
        parts.append("""Flag anomalies of these types:
- CONTRADICTION: Session claims that conflict with master context or recent decisions
- REGRESSION: A previously resolved failure recurring
- DRIFT: Project scope or direction changing without an explicit decision
- INCONSISTENCY: Entity or fact mentioned differently than established
- ESCALATION: Issue severity increasing across sessions

Be conservative. Only flag genuine anomalies, not minor updates or natural evolution.
If no anomalies detected, return an empty array.
Use the detected_anomalies tool.""")

        messages = [{"role": "user", "content": chr(10).join(parts)}]

        result = self.call_with_escalation(
            "anomaly_detection", messages,
            tools=[ANOMALY_TOOL],
            tool_choice={"type": "function", "function": {"name": "detected_anomalies"}}
        )

        if result and result.get("anomalies"):
            return result["anomalies"]
        return []

    @property
    def stats(self) -> dict:
        return {"calls": self._call_count, "estimated_cost": self._total_cost, "backend": self.backend}


# Singleton
_client: Optional[OpenRouterClient] = None

def get_client() -> OpenRouterClient:
    global _client
    if _client is None:
        _client = OpenRouterClient()
    return _client

get_openrouter = get_client  # Alias for save.py


