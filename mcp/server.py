"""Memory MCP Server - Standalone StreamableHTTP transport.

Exposes Memory tools via MCP protocol on port 8000.
Proxies to the Memory REST API.
"""

import os
import json
import httpx
from mcp.server.fastmcp import FastMCP

API_BASE = os.environ.get("MEMORY_API_URL", "http://memory:9040")

mcp = FastMCP("Memory", instructions="Persistent memory system for LLM conversations.", host="0.0.0.0", port=8000)

_http = httpx.Client(base_url=API_BASE, timeout=30.0)


def _api(method, path, data=None):
    try:
        if method == "GET":
            r = _http.get(path)
        else:
            r = _http.post(path, json=data or {})
        r.raise_for_status()
        return r.json()
    except httpx.HTTPStatusError as e:
        return {"error": f"HTTP {e.response.status_code}: {e.response.text[:300]}"}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def memory_load(topic: str = "") -> str:
    """Load context for a new session. Returns master context, archive hits, nudges, anomalies. Call at session start."""
    payload = {}
    if topic:
        payload["topic"] = topic
    return json.dumps(_api("POST", "/api/load", payload), indent=2)


@mcp.tool()
def memory_save(
    session_id: str,
    summary: str,
    decisions: list[str] | None = None,
    failures: list[str] | None = None,
    files_changed: list[str] | None = None,
    next_steps: list[str] | None = None,
    significance: str = "medium",
    tags: list[str] | None = None,
    transcript_text: str = "",
) -> str:
    """Save session context at end of conversation. Requires session_id from memory_load."""
    payload = {"session_id": session_id, "summary": summary, "significance": significance}
    if decisions: payload["decisions"] = decisions
    if failures: payload["failures"] = failures
    if files_changed: payload["files_changed"] = files_changed
    if next_steps: payload["next_steps"] = next_steps
    if tags: payload["tags"] = tags
    if transcript_text: payload["transcript_text"] = transcript_text
    return json.dumps(_api("POST", "/api/save", payload), indent=2)


@mcp.tool()
def memory_checkpoint(session_id: str, note: str, significance: str = "medium", transcript_text: str = "") -> str:
    """Lightweight mid-session save. Haiku auto-extracts structured fields."""
    payload = {"session_id": session_id, "note": note, "significance": significance}
    if transcript_text: payload["transcript_text"] = transcript_text
    return json.dumps(_api("POST", "/api/checkpoint", payload), indent=2)


@mcp.tool()
def memory_search(query: str, collections: list[str] | None = None, limit: int = 5, tags: list[str] | None = None) -> str:
    """Search archive for historical context across sessions, decisions, failures, entities."""
    payload = {"query": query, "limit": limit}
    if collections: payload["collections"] = collections
    if tags: payload["tags"] = tags
    return json.dumps(_api("POST", "/api/search", payload), indent=2)


@mcp.tool()
def memory_correct(item: str, correction: str, scope: str = "both") -> str:
    """Correct wrong information in Memory. Fixes master context and/or ChromaDB archive."""
    return json.dumps(_api("POST", "/api/correct", {"item": item, "correction": correction, "scope": scope}), indent=2)


@mcp.tool()
def memory_context() -> str:
    """Get current master context document (read-only) without starting a session."""
    return json.dumps(_api("GET", "/api/internal/master-context"), indent=2)


@mcp.tool()
def memory_stats() -> str:
    """Get Memory system health and statistics."""
    return json.dumps(_api("GET", "/api/health"), indent=2)


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
