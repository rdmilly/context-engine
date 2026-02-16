"""Internal endpoints: health, summary, stats.

Not exposed as MCP tools — used by tool-filter and monitoring.
"""

import time
from pathlib import Path

from fastapi import APIRouter

from models import HealthResponse
from config import SESSIONS_DIR, LEARNING_MODE
from services import kb_gateway, chromadb_client
from worker.processor import get_processor
from utils.logging_ import logger
from utils.nudges import get_active_nudges, dismiss_nudge, get_nudge_stats
from utils.anomalies import get_active_anomalies, dismiss_anomaly, get_anomaly_stats
from utils.degradation import get_manager as get_degradation_manager

router = APIRouter()

_start_time = time.time()


@router.get("/api/health", response_model=HealthResponse)
async def health():
    """Health check endpoint."""
    sessions_count = 0
    try:
        sessions_count = len(list(SESSIONS_DIR.glob("*.json")))
    except Exception:
        pass

    dm = get_degradation_manager()
    return HealthResponse(
        status="healthy" if dm.level.value in ("full", "partial") else "degraded",
        version="0.3.0",
        chromadb_connected=chromadb_client.is_connected(),
        kb_accessible=kb_gateway.kb_accessible(),
        sessions_count=sessions_count,
        uptime_seconds=round(time.time() - _start_time, 1),
        learning_mode=LEARNING_MODE,
        degradation_level=dm.level.value,
    )


@router.get("/api/summary")
async def get_summary():
    """Return hot context summary for Layer 2 auto-injection."""
    dm = get_degradation_manager()
    content = kb_gateway.read_master_context()

    if content is None:
        # Try cache fallback
        cached = dm.get_cached_context()
        if cached:
            content = cached
        else:
            return {
                "summary": "ContextEngine active but master context not yet created.",
                "tokens_estimate": 10,
                "degraded": True,
                "degradation_level": dm.level.value,
            }

    max_chars = 2000
    if len(content) > max_chars:
        summary = content[:max_chars] + "\n\n[... truncated for token budget ...]"
    else:
        summary = content

    return {
        "summary": summary,
        "tokens_estimate": len(summary.split()),
        "degraded": dm.level.value != "full",
        "degradation_level": dm.level.value,
    }


@router.get("/api/stats")
async def get_stats():
    """Collection sizes, session counts, worker stats."""
    sessions_count = 0
    processed_count = 0
    unprocessed_count = 0
    try:
        import json
        for f in SESSIONS_DIR.glob("*.json"):
            sessions_count += 1
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                if data.get("_processed"):
                    processed_count += 1
                else:
                    unprocessed_count += 1
            except Exception:
                unprocessed_count += 1
    except Exception:
        pass

    chromadb_stats = {}
    if chromadb_client.is_connected():
        chromadb_stats = chromadb_client.get_collection_stats()

    processor = get_processor()

    # Gather recent sessions for dashboard
    recent_sessions = []
    try:
        import json as _json
        session_files = sorted(SESSIONS_DIR.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)[:20]
        for sf in session_files:
            try:
                sd = _json.loads(sf.read_text(encoding="utf-8"))
                recent_sessions.append({
                    "session_id": sd.get("session_id", sf.stem),
                    "significance": sd.get("significance", "unknown"),
                    "saved_at": sd.get("saved_at"),
                    "processed": bool(sd.get("_processed")),
                    "summary_preview": (sd.get("summary") or "")[:120],
                })
            except Exception:
                pass
    except Exception:
        pass

    # LLM stats
    llm_stats = {}
    try:
        from services.openrouter import get_client as _get_llm
        llm_stats = _get_llm().stats
    except Exception:
        llm_stats = {"calls": 0, "backend": "unknown"}

    return {
        "sessions": {"total": sessions_count, "processed": processed_count, "unprocessed": unprocessed_count},
        "sessions_total": sessions_count,
        "sessions_processed": processed_count,
        "sessions_unprocessed": unprocessed_count,
        "recent_sessions": recent_sessions,
        "chromadb_collections": chromadb_stats,
        "kb_accessible": kb_gateway.kb_accessible(),
        "learning_mode": LEARNING_MODE,
        "worker": processor.status,
        "llm": llm_stats,
    }


@router.get("/api/worker")
async def worker_status():
    """Detailed worker status."""
    processor = get_processor()
    result = processor.status

    try:
        from services.openrouter import get_client as get_llm
        llm = get_llm()
        result["llm"] = llm.stats
    except Exception:
        result["llm"] = {"error": "not initialized"}

    return result



@router.get("/api/nudges")
async def list_nudges():
    """Get active nudges and stats."""
    return {
        "nudges": get_active_nudges(limit=10),
        "stats": get_nudge_stats(),
    }


@router.post("/api/nudges/dismiss")
async def dismiss_nudge_endpoint(request: dict):
    """Dismiss a nudge by partial message match."""
    msg = request.get("message", "")
    if not msg:
        return {"error": "message field required"}
    found = dismiss_nudge(msg)
    return {"dismissed": found, "query": msg}


@router.get("/api/anomalies")
async def list_anomalies():
    """Get active anomalies and stats."""
    return {
        "anomalies": get_active_anomalies(),
        "stats": get_anomaly_stats(),
    }


@router.post("/api/anomalies/dismiss")
async def dismiss_anomaly_endpoint(request: dict):
    """Dismiss an anomaly by partial description match."""
    desc = request.get("description", "")
    if not desc:
        return {"error": "description field required"}
    found = dismiss_anomaly(desc)
    return {"dismissed": found, "query": desc}


@router.get("/api/degradation")
async def get_degradation_status():
    """Get current degradation status and dependency health."""
    dm = get_degradation_manager()
    return dm.status



@router.get("/api/setup/claude-desktop")
async def claude_desktop_config():
    """Generate Claude Desktop MCP config for this ContextEngine instance."""
    import socket
    hostname = socket.gethostname()

    return {
        "instruction": "Add this to your Claude Desktop config file",
        "config_path": {
            "macos": "~/Library/Application Support/Claude/claude_desktop_config.json",
            "windows": "%APPDATA%/Claude/claude_desktop_config.json",
            "linux": "~/.config/Claude/claude_desktop_config.json",
        },
        "config": {
            "mcpServers": {
                "context-engine": {
                    "command": "python3",
                    "args": ["<path-to>/mcp-bridge.py"],
                    "env": {
                        "CONTEXT_ENGINE_URL": "http://localhost:9040"
                    }
                }
            }
        },
        "notes": [
            "Replace <path-to> with the actual path to mcp-bridge.py",
            "If ContextEngine runs on a different port, update CONTEXT_ENGINE_URL",
            "mcp-bridge.py has zero dependencies — just Python 3.8+",
        ]
    }
