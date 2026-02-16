"""Internal endpoints: health, summary, stats."""

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
    sessions_count = len(list(SESSIONS_DIR.glob("*.json"))) if SESSIONS_DIR.exists() else 0
    dm = get_degradation_manager()
    return HealthResponse(status="healthy" if dm.level.value in ("full", "partial") else "degraded", version="0.3.0", chromadb_connected=chromadb_client.is_connected(), kb_accessible=kb_gateway.kb_accessible(), sessions_count=sessions_count, uptime_seconds=round(time.time() - _start_time, 1), learning_mode=LEARNING_MODE, degradation_level=dm.level.value)


@router.get("/api/summary")
async def get_summary():
    dm = get_degradation_manager()
    content = kb_gateway.read_master_context()
    if content is None:
        cached = dm.get_cached_context()
        content = cached if cached else None
    if content is None:
        return {"summary": "ContextEngine active but master context not yet created.", "tokens_estimate": 10, "degraded": True, "degradation_level": dm.level.value}
    summary = content[:2000] + "\n\n[... truncated ...]" if len(content) > 2000 else content
    return {"summary": summary, "tokens_estimate": len(summary.split()), "degraded": dm.level.value != "full", "degradation_level": dm.level.value}


@router.get("/api/stats")
async def get_stats():
    import json
    sessions_count = processed_count = unprocessed_count = 0
    recent_sessions = []
    try:
        for f in SESSIONS_DIR.glob("*.json"):
            sessions_count += 1
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                if data.get("_processed"): processed_count += 1
                else: unprocessed_count += 1
            except: unprocessed_count += 1
        for sf in sorted(SESSIONS_DIR.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)[:20]:
            try:
                sd = json.loads(sf.read_text(encoding="utf-8"))
                recent_sessions.append({"session_id": sd.get("session_id", sf.stem), "significance": sd.get("significance", "unknown"), "processed": bool(sd.get("_processed")), "summary_preview": (sd.get("summary") or "")[:120]})
            except: pass
    except: pass
    chromadb_stats = chromadb_client.get_collection_stats() if chromadb_client.is_connected() else {}
    processor = get_processor()
    llm_stats = {}
    try:
        from services.openrouter import get_client as _get_llm
        llm_stats = _get_llm().stats
    except: llm_stats = {"calls": 0, "backend": "unknown"}
    return {"sessions": {"total": sessions_count, "processed": processed_count, "unprocessed": unprocessed_count}, "sessions_total": sessions_count, "sessions_processed": processed_count, "sessions_unprocessed": unprocessed_count, "recent_sessions": recent_sessions, "chromadb_collections": chromadb_stats, "kb_accessible": kb_gateway.kb_accessible(), "learning_mode": LEARNING_MODE, "worker": processor.status, "llm": llm_stats}


@router.get("/api/worker")
async def worker_status():
    processor = get_processor()
    result = processor.status
    try:
        from services.openrouter import get_client as get_llm
        result["llm"] = get_llm().stats
    except: result["llm"] = {"error": "not initialized"}
    return result


@router.get("/api/nudges")
async def list_nudges():
    return {"nudges": get_active_nudges(limit=10), "stats": get_nudge_stats()}


@router.post("/api/nudges/dismiss")
async def dismiss_nudge_endpoint(request: dict):
    msg = request.get("message", "")
    if not msg: return {"error": "message field required"}
    return {"dismissed": dismiss_nudge(msg), "query": msg}


@router.get("/api/anomalies")
async def list_anomalies():
    return {"anomalies": get_active_anomalies(), "stats": get_anomaly_stats()}


@router.post("/api/anomalies/dismiss")
async def dismiss_anomaly_endpoint(request: dict):
    desc = request.get("description", "")
    if not desc: return {"error": "description field required"}
    return {"dismissed": dismiss_anomaly(desc), "query": desc}


@router.get("/api/degradation")
async def get_degradation_status():
    return get_degradation_manager().status


@router.get("/api/setup/claude-desktop")
async def claude_desktop_config():
    return {"config": {"mcpServers": {"context-engine": {"command": "python3", "args": ["<path-to>/mcp-bridge.py"], "env": {"CONTEXT_ENGINE_URL": "http://localhost:9040"}}}}}
