"""context_load endpoint."""

from fastapi import APIRouter
from models import LoadRequest, LoadResponse
from services import kb_gateway, chromadb_client
from utils.session import generate_session_id
from config import MAX_LOAD_RESPONSE_CHARS, LEARNING_MODE
from utils.nudges import get_active_nudges
from utils.logging_ import logger

router = APIRouter()


def _search_archive(topic: str, limit: int = 5) -> list:
    results = []
    for col_name in ["project_archive", "decisions", "sessions"]:
        try:
            hits = chromadb_client.search_collection(col_name, topic, n_results=limit)
            for hit in hits:
                if hit.get("distance", 999) < 1.5:
                    results.append({"collection": col_name, "content": hit["content"][:500], "metadata": hit.get("metadata", {}), "relevance": round(1.0 - (hit.get("distance", 1.0) / 2.0), 3)})
        except Exception as e:
            logger.warning(f"Archive search failed for {col_name}: {e}")
    results.sort(key=lambda x: x.get("relevance", 0), reverse=True)
    return results[:limit]


def _get_failure_warnings(topic: str = None, limit: int = 3) -> list:
    warnings = []
    try:
        if topic:
            hits = chromadb_client.search_collection("failures", topic, n_results=limit)
            for hit in hits:
                if hit.get("distance", 999) < 1.2:
                    warnings.append(f"[{hit.get('metadata', {}).get('session_id', 'unknown')}] {hit.get('content', '')[:200]}")
    except Exception as e:
        logger.warning(f"Failure warning lookup failed: {e}")
    return warnings


def _detect_promotions(limit: int = 3) -> list:
    nudges = []
    try:
        recent = chromadb_client.get_recent_sessions(n=10)
        if len(recent) < 3:
            return []
        topic_counts = {}
        for session in recent:
            topics_str = session.get("metadata", {}).get("topics", "")
            if topics_str:
                for topic in topics_str.split(","):
                    topic = topic.strip().lower()
                    if topic:
                        topic_counts[topic] = topic_counts.get(topic, 0) + 1
        hot_context = kb_gateway.read_master_context() or ""
        hot_lower = hot_context.lower()
        for topic, count in sorted(topic_counts.items(), key=lambda x: -x[1]):
            if count >= 3 and topic not in hot_lower:
                nudges.append(f"Topic '{topic}' appeared in {count}/10 recent sessions but isn't in master context. Consider promoting.")
                if len(nudges) >= limit:
                    break
    except Exception as e:
        logger.warning(f"Promotion detection failed: {e}")
    return nudges


@router.post("/api/load", response_model=LoadResponse)
async def context_load(request: LoadRequest = None):
    if request is None:
        request = LoadRequest()
    session_id = generate_session_id()
    logger.info(f"context_load: session={session_id}, topic={request.topic}")
    hot_context = kb_gateway.read_master_context()
    degraded = False
    degraded_reason = None
    if hot_context is None:
        degraded = True
        degraded_reason = "KB Gateway not accessible."
        hot_context = "[Context unavailable]"
    archive_hits, failure_warnings, nudges, conflicts = [], [], [], []
    if chromadb_client.is_connected():
        if request.topic:
            archive_hits = _search_archive(request.topic)
            failure_warnings = _get_failure_warnings(request.topic)
        nudges = _detect_promotions()
        if not LEARNING_MODE:
            nudges.extend(get_active_nudges(limit=5, topic=request.topic))
    total_chars = len(hot_context) + sum(len(h.get("content", "")) for h in archive_hits)
    if total_chars > MAX_LOAD_RESPONSE_CHARS:
        budget = MAX_LOAD_RESPONSE_CHARS - len(hot_context)
        trimmed = []
        for h in archive_hits:
            if budget > 200:
                if len(h.get("content", "")) > budget:
                    h["content"] = h["content"][:budget] + "..."
                trimmed.append(h)
                budget -= len(h["content"])
            else:
                break
        archive_hits = trimmed
    return LoadResponse(session_id=session_id, hot_context=hot_context, archive_hits=archive_hits, failure_warnings=failure_warnings, nudges=nudges, conflicts=conflicts, degraded=degraded, degraded_reason=degraded_reason)
