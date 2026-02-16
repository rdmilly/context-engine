"""context_load endpoint.

Reads master context from KB Gateway.
Searches ChromaDB for topic-relevant archive hits, failure warnings, and promotions.
"""

from fastapi import APIRouter

from models import LoadRequest, LoadResponse
from services import kb_gateway, chromadb_client
from utils.session import generate_session_id
from config import MAX_LOAD_RESPONSE_CHARS, LEARNING_MODE
from utils.nudges import get_active_nudges
from utils.logging_ import logger

router = APIRouter()


def _search_archive(topic: str, limit: int = 5) -> list:
    """Search multiple ChromaDB collections for topic-relevant context."""
    results = []
    collections_to_search = ["project_archive", "decisions", "sessions"]

    for col_name in collections_to_search:
        try:
            hits = chromadb_client.search_collection(col_name, topic, n_results=limit)
            for hit in hits:
                # Only include reasonably relevant results (lower distance = more relevant)
                if hit.get("distance", 999) < 1.5:
                    results.append({
                        "collection": col_name,
                        "content": hit["content"][:500],  # Truncate for token budget
                        "metadata": hit.get("metadata", {}),
                        "relevance": round(1.0 - (hit.get("distance", 1.0) / 2.0), 3),
                    })
        except Exception as e:
            logger.warning(f"Archive search failed for {col_name}: {e}")

    # Sort by relevance descending, limit total
    results.sort(key=lambda x: x.get("relevance", 0), reverse=True)
    return results[:limit]


def _get_failure_warnings(topic: str = None, limit: int = 3) -> list:
    """Get relevant failure warnings from the failures collection."""
    warnings = []
    try:
        if topic:
            hits = chromadb_client.search_collection("failures", topic, n_results=limit)
        else:
            # Get most recent failures
            hits = chromadb_client.get_recent_sessions(n=limit)  # Will adapt if needed
            hits = []  # Fallback: no topic = no failure warnings

        for hit in hits:
            if hit.get("distance", 999) < 1.2:  # Only very relevant failures
                content = hit.get("content", "")
                session_id = hit.get("metadata", {}).get("session_id", "unknown")
                warnings.append(f"[{session_id}] {content[:200]}")
    except Exception as e:
        logger.warning(f"Failure warning lookup failed: {e}")

    return warnings


def _detect_promotions(limit: int = 3) -> list:
    """Detect archived topics that keep recurring and should be promoted to hot.

    Looks at recent sessions for repeated topics that appear in archive
    but not in master context.
    """
    nudges = []
    try:
        recent = chromadb_client.get_recent_sessions(n=10)
        if len(recent) < 3:
            return []  # Not enough data

        # Count topic frequency across recent sessions
        topic_counts = {}
        for session in recent:
            content = session.get("content", "")
            metadata = session.get("metadata", {})
            topics_str = metadata.get("topics", "")
            if topics_str:
                for topic in topics_str.split(","):
                    topic = topic.strip().lower()
                    if topic:
                        topic_counts[topic] = topic_counts.get(topic, 0) + 1

        # Topics appearing in 3+ of last 10 sessions might need promotion
        hot_context = kb_gateway.read_master_context() or ""
        hot_lower = hot_context.lower()

        for topic, count in sorted(topic_counts.items(), key=lambda x: -x[1]):
            if count >= 3 and topic not in hot_lower:
                nudges.append(
                    f"Topic '{topic}' appeared in {count}/10 recent sessions "
                    f"but isn't in master context. Consider promoting."
                )
                if len(nudges) >= limit:
                    break

    except Exception as e:
        logger.warning(f"Promotion detection failed: {e}")

    return nudges


@router.post("/api/load", response_model=LoadResponse)
async def context_load(request: LoadRequest = None):
    """Load context for a new session.

    Returns hot context from KB + archive hits + failure warnings + promotion nudges.
    Generates a session_id for this conversation.
    """
    if request is None:
        request = LoadRequest()

    session_id = generate_session_id()
    logger.info(f"context_load: session={session_id}, topic={request.topic}")

    # Tier 1: Read hot context from KB
    hot_context = kb_gateway.read_master_context()
    degraded = False
    degraded_reason = None

    if hot_context is None:
        degraded = True
        degraded_reason = "KB Gateway not accessible. Hot context unavailable."
        hot_context = "[Context unavailable — KB Gateway unreachable. Proceeding without historical context.]"
        logger.warning("context_load: KB degraded mode")

    # Tier 2: Archive search (if topic provided)
    archive_hits = []
    failure_warnings = []
    nudges = []
    conflicts = []

    if chromadb_client.is_connected():
        # Search archive for topic-relevant context
        if request.topic:
            archive_hits = _search_archive(request.topic)
            failure_warnings = _get_failure_warnings(request.topic)
            logger.info(f"context_load: {len(archive_hits)} archive hits, {len(failure_warnings)} warnings for '{request.topic}'")

        # Check for promotion candidates (rule-based)
        nudges = _detect_promotions()
        
        # Add LLM-generated nudges (only when learning mode is off)
        if not LEARNING_MODE:
            llm_nudges = get_active_nudges(limit=5, topic=request.topic)
            nudges.extend(llm_nudges)
        
        if nudges:
            logger.info(f"context_load: {len(nudges)} total nudges")

    # Token budget enforcement
    total_chars = len(hot_context)
    if archive_hits:
        for h in archive_hits:
            total_chars += len(h.get("content", ""))
    
    if total_chars > MAX_LOAD_RESPONSE_CHARS:
        # Trim archive hits to fit budget
        budget_remaining = MAX_LOAD_RESPONSE_CHARS - len(hot_context)
        trimmed_hits = []
        for h in archive_hits:
            content_len = len(h.get("content", ""))
            if budget_remaining > 200:  # Keep at least 200 chars per hit
                if content_len > budget_remaining:
                    h["content"] = h["content"][:budget_remaining] + "..."
                trimmed_hits.append(h)
                budget_remaining -= len(h["content"])
            else:
                break
        archive_hits = trimmed_hits
        logger.info(f"context_load: trimmed archive hits to fit {MAX_LOAD_RESPONSE_CHARS} char budget")

    return LoadResponse(
        session_id=session_id,
        hot_context=hot_context,
        archive_hits=archive_hits,
        failure_warnings=failure_warnings,
        nudges=nudges,
        conflicts=conflicts,
        degraded=degraded,
        degraded_reason=degraded_reason,
    )
