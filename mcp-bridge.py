#!/usr/bin/env python3
"""ContextEngine MCP Bridge — stdio transport for Claude Desktop.

This script acts as an MCP server (stdio transport) that proxies
tool calls to the ContextEngine REST API.

Usage in Claude Desktop config:
{
  "mcpServers": {
    "context-engine": {
      "command": "python3",
      "args": ["/path/to/mcp-bridge.py"],
      "env": {
        "CONTEXT_ENGINE_URL": "http://localhost:9040"
      }
    }
  }
}
"""

import json
import sys
import os
import urllib.request
import urllib.error

CE_URL = os.environ.get("CONTEXT_ENGINE_URL", "http://localhost:9040")

# MCP Tool definitions matching ContextEngine endpoints
TOOLS = [
    {
        "name": "context_load",
        "description": "Load context for a new session. Returns master context, archive hits, nudges, and anomalies. Call at the start of every session.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "topic": {
                    "type": "string",
                    "description": "Optional topic to focus context retrieval on."
                }
            }
        }
    },
    {
        "name": "context_save",
        "description": "Save session context at end of conversation. Pass structured summary with decisions, failures, files changed, and next steps.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "Session ID from context_load"},
                "summary": {"type": "string", "description": "Structured session summary"},
                "decisions": {"type": "array", "items": {"type": "string"}, "description": "Key decisions made"},
                "failures": {"type": "array", "items": {"type": "string"}, "description": "What broke or didn't work"},
                "files_changed": {"type": "array", "items": {"type": "string"}, "description": "Files created or modified"},
                "next_steps": {"type": "array", "items": {"type": "string"}, "description": "Prioritized next steps"},
                "significance": {"type": "string", "enum": ["low", "medium", "high"], "description": "Session importance"},
                "tags": {"type": "array", "items": {"type": "string"}, "description": "Tags for categorization"},
                "transcript_text": {"type": "string", "description": "Optional raw transcript text"}
            },
            "required": ["session_id", "summary"]
        }
    },
    {
        "name": "context_checkpoint",
        "description": "Lightweight mid-session save. Pass session_id and a brief note.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "Session ID from context_load"},
                "note": {"type": "string", "description": "Brief note about what happened (1-3 sentences)"},
                "significance": {"type": "string", "enum": ["low", "medium", "high"], "default": "medium"},
                "transcript_text": {"type": "string", "description": "Optional raw transcript"}
            },
            "required": ["session_id", "note"]
        }
    },
    {
        "name": "context_search",
        "description": "Search the ContextEngine archive for historical context across sessions, decisions, failures, and entities.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Natural language search query"},
                "collections": {"type": "array", "items": {"type": "string"}, "description": "Specific collections to search"},
                "limit": {"type": "integer", "default": 5, "description": "Max results"}
            },
            "required": ["query"]
        }
    },
    {
        "name": "context_correct",
        "description": "Fix incorrect information in ContextEngine. Corrects data in master context and/or archive.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "item": {"type": "string", "description": "What is incorrect"},
                "correction": {"type": "string", "description": "What it should be"},
                "scope": {"type": "string", "enum": ["hot", "archive", "both"], "default": "both"}
            },
            "required": ["item", "correction"]
        }
    }
]

# Endpoint mapping
TOOL_ENDPOINTS = {
    "context_load": ("POST", "/api/load"),
    "context_save": ("POST", "/api/save"),
    "context_checkpoint": ("POST", "/api/checkpoint"),
    "context_search": ("POST", "/api/search"),
    "context_correct": ("POST", "/api/correct"),
}


def call_api(method, path, data=None):
    """Call ContextEngine REST API."""
    url = CE_URL + path
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}: {e.read().decode()[:200]}"}
    except Exception as e:
        return {"error": str(e)}


def handle_request(request):
    """Handle an MCP JSON-RPC request."""
    method = request.get("method")
    req_id = request.get("id")
    params = request.get("params", {})

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {
                    "name": "context-engine",
                    "version": "0.3.0"
                }
            }
        }

    if method == "notifications/initialized":
        return None  # No response for notifications

    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"tools": TOOLS}
        }

    if method == "tools/call":
        tool_name = params.get("name")
        arguments = params.get("arguments", {})

        if tool_name not in TOOL_ENDPOINTS:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": f"Unknown tool: {tool_name}"}],
                    "isError": True
                }
            }

        http_method, path = TOOL_ENDPOINTS[tool_name]
        result = call_api(http_method, path, arguments)

        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "content": [{"type": "text", "text": json.dumps(result, indent=2)}],
                "isError": "error" in result
            }
        }

    # Unknown method
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"Unknown method: {method}"}
    }


def main():
    """Run MCP stdio server."""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
            response = handle_request(request)
            if response is not None:
                sys.stdout.write(json.dumps(response) + "\n")
                sys.stdout.flush()
        except json.JSONDecodeError:
            sys.stderr.write(f"Invalid JSON: {line[:100]}\n")
        except Exception as e:
            sys.stderr.write(f"Error: {e}\n")


if __name__ == "__main__":
    main()
