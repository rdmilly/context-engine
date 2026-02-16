"""context_load endpoint."""

from fastapi import APIRouter
from models import LoadRequest, LoadResponse
from services import kb_gateway, chromadb_client
from utils.session import generate_session_id
from config import MAX_LOAD_RESPONSE_CHARS, LEARNING_MODE
from utils.nudges import get_active_nudges
from utils.logging_ import logger

router = APIRouter()


def _search_archive(topic, limit=5):
    results = []
    for col in ["project_archive", "decisions", "sessions"]:
        try:
            for hit in chromadb_client.search_collection(col, topic, n_results=limit):
                if hit.get("distance", 999) < 1.5:
                    results.append({"collection": col, "content": hit["content"][:500],
                                    "metadata": hit.get("metadata", {}),
                                    "relevance": round(1.0 - (hit.get("distance", 1.0) / 2.0), 3)})
        except Exception as e:
            logger.warning(f"Archive search failed for {col}: {e}")
    results.sort(key=lambda x: x.get("relevance", 0), reverse=True)
    return results[:limit]


def _get_failure_warnings(topic, limit=3):
    warnings = []
    try:
        for hit in chromadb_client.search_collection("failures", topic, n_results=limit):
            if hit.get("distance", 999) < 1.2:
                warnings.append(f"[{hit.get('metadata', {}).get('session_id', '?')}] {hit.get('content', '')[:200]}")
    except Exception:
        pass
    return warnings


def _detect_promotions(limit=3):
    nudges = []
    try:
        recent = chromadb_client.get_recent_sessions(n=10)
        if len(recent) < 3: return []
        topic_counts = {}
        for s in recent:
            for t in s.get("metadata", {}).get("topics", "").split(","):
                t = t.strip().lower()
                if t: topic_counts[t] = topic_counts.get(t, 0) + 1
        hot = (kb_gateway.read_master_context() or "").lower()
        for topic, count in sorted(topic_counts.items(), key=lambda x: -x[1]):
            if count >= 3 and topic not in hot:
                nudges.append(f"Topic '{topic}' in {count}/10 recent sessions but not in master context.")
                if len(nudges) >= limit: break
    except Exception:
        pass
    return nudges


@router.post("/api/load", response_model=LoadResponse)
async def context_load(request: LoadRequest = None):
    if request is None: request = LoadRequest()
    session_id = generate_session_id()
    hot_context = kb_gateway.read_master_context()
    degraded = hot_context is None
    degraded_reason = "KB Gateway not accessible" if degraded else None
    if degraded: hot_context = "[Context unavailable]"
    archive_hits, failure_warnings, nudges, conflicts = [], [], [], []
    if chromadb_client.is_connected():
        if request.topic:
            archive_hits = _search_archive(request.topic)
            failure_warnings = _get_failure_warnings(request.topic)
        nudges = _detect_promotions()
        if not LEARNING_MODE:
            nudges.extend(get_active_nudges(limit=5, topic=request.topic))
    total = len(hot_context) + sum(len(h.get("content", "")) for h in archive_hits)
    if total > MAX_LOAD_RESPONSE_CHARS:
        budget = MAX_LOAD_RESPONSE_CHARS - len(hot_context)
        trimmed = []
        for h in archive_hits:
            if budget > 200:
                if len(h["content"]) > budget: h["content"] = h["content"][:budget] + "..."
                trimmed.append(h)
                budget -= len(h["content"])
        archive_hits = trimmed
    return LoadResponse(session_id=session_id, hot_context=hot_context, archive_hits=archive_hits,
                        failure_warnings=failure_warnings, nudges=nudges, conflicts=conflicts,
                        degraded=degraded, degraded_reason=degraded_reason)
