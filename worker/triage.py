"""Triage logic for session processing.

Uses LLM (Haiku with Sonnet escalation) to decide what happens
to each piece of session data:
- KEEP: Add/update in master context (hot)
- ARCHIVE: Store in ChromaDB for semantic search
- MERGE: Update existing entry in master context or archive
- DISCARD: Drop (in learning mode, archive instead of discard)
"""

import json
from typing import Optional, Dict, Any, List
from datetime import datetime, timezone

from config import LEARNING_MODE, resolve_collection_name
from services.openrouter import get_client as get_llm
from services.chromadb_client import (
    add_document,
    upsert_document,
    search_collection,
    take_snapshot,
)
from services.kb_gateway import read_master_context, write_master_context
from utils.logging_ import logger


def _archive_item(item: dict, session_id: str) -> bool:
    """Archive a single triaged item to the appropriate ChromaDB collection."""
    raw_collection = item.get("collection", "project_archive")
    collection = resolve_collection_name(raw_collection)
    if collection != raw_collection:
        logger.info(f"Resolved collection name: '{raw_collection}' -> '{collection}'")

    content = item.get("content", "")
    action = item.get("action", "archive")
    reason = item.get("reason", "")

    if not content:
        return False

    # Generate a document ID
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    doc_id = f"{session_id}:{collection}:{timestamp}"

    metadata = {
        "session_id": session_id,
        "action": action,
        "reason": reason,
        "source": "triage",
    }

    if action == "merge":
        merge_target = item.get("merge_target", "")
        if merge_target:
            metadata["merge_target"] = merge_target
            hits = search_collection(collection, merge_target, n_results=1)
            if hits:
                existing = hits[0]
                take_snapshot(collection, existing["id"])
                merged_content = f"{existing['content']}\n\n[Updated {timestamp}]\n{content}"
                return upsert_document(collection, existing["id"], merged_content, metadata)

    return add_document(collection, doc_id, content, metadata)


def _archive_session_summary(session_data: dict, summary_result: dict) -> bool:
    """Archive the compressed session summary to the sessions collection."""
    session_id = session_data.get("session_id", "unknown")
    compressed = summary_result.get("compressed_summary", "")
    if not compressed:
        return False

    metadata = {
        "session_id": session_id,
        "significance": summary_result.get("significance_confirmed", session_data.get("significance", "medium")),
        "key_topics": json.dumps(summary_result.get("key_topics", [])),
        "projects": json.dumps(summary_result.get("projects_mentioned", [])),
        "tags": json.dumps(session_data.get("tags", [])),
    }

    return add_document("sessions", session_id, compressed, metadata)


def _update_master_context(triage_result: dict, session_data: dict) -> bool:
    """Update master context based on triage decisions."""
    current_master = read_master_context()
    if not current_master:
        logger.error("Cannot read master context for update")
        return False

    llm = get_llm()
    compress_result = llm.compress_master_context(
        current_master, triage_result, session_data
    )

    if not compress_result:
        logger.error("LLM failed to compress master context")
        return False

    new_master = compress_result.get("master_context_markdown", "")
    if not new_master or len(new_master) < 100:
        logger.error(f"Compressed master context too short ({len(new_master)} chars), skipping")
        return False

    changes = compress_result.get("changes_made", [])
    logger.info(f"Master context update: {len(changes)} changes, {len(new_master)} chars")

    success = write_master_context(new_master)
    if success:
        logger.info(f"Master context updated. Changes: {changes}")
    return success


def _check_promotions(session_data: dict) -> List[Dict[str, Any]]:
    """Check if any archived topics should be promoted to hot context.

    If a topic appears in 3+ of the last 10 sessions, consider promotion.
    """
    from services.chromadb_client import get_recent_sessions

    recent = get_recent_sessions(10)
    if len(recent) < 3:
        return []

    topic_counts = {}
    for sess in recent:
        topics_str = sess.get("metadata", {}).get("key_topics", "[]")
        try:
            topics = json.loads(topics_str) if isinstance(topics_str, str) else topics_str
        except json.JSONDecodeError:
            topics = []
        for topic in topics:
            topic_lower = topic.lower().strip()
            topic_counts[topic_lower] = topic_counts.get(topic_lower, 0) + 1

    promotions = []
    for topic, count in topic_counts.items():
        if count >= 3:
            master = read_master_context() or ""
            if topic.lower() not in master.lower():
                promotions.append({
                    "topic": topic,
                    "appearances": count,
                    "action": "promote_to_hot",
                })
                logger.info(f"Promotion candidate: '{topic}' appeared in {count}/10 recent sessions")

    return promotions


def process_session(session_data: dict) -> Dict[str, Any]:
    """Full triage pipeline for a session.

    1. Summarize session (Haiku)
    2. Triage items (Haiku, escalate to Sonnet if needed)
    3. Archive items to ChromaDB
    4. Update master context (Sonnet)
    5. Check for promotions

    Returns processing report.
    """
    session_id = session_data.get("session_id", "unknown")
    significance = session_data.get("significance", "medium")
    logger.info(f"Processing session {session_id} (significance: {significance})")

    report = {
        "session_id": session_id,
        "steps_completed": [],
        "steps_failed": [],
        "items_archived": 0,
        "master_updated": False,
        "promotions": [],
    }

    llm = get_llm()

    # Step 1: Summarize session
    try:
        summary_result = llm.summarize_session(session_data)
        if summary_result:
            archived = _archive_session_summary(session_data, summary_result)
            if archived:
                report["steps_completed"].append("session_summary")
                report["items_archived"] += 1
            else:
                report["steps_failed"].append("session_summary_archive")
        else:
            report["steps_failed"].append("session_summary_llm")
    except Exception as e:
        logger.error(f"Summary step failed: {e}")
        report["steps_failed"].append(f"session_summary: {e}")

    # Step 2: Triage (only for medium/high significance)
    if significance in ("medium", "high"):
        try:
            current_master = read_master_context() or ""
            triage_result = llm.triage_session(session_data, current_master)

            if triage_result:
                items = triage_result.get("items", [])
                archived_count = 0

                for item in items:
                    action = item.get("action", "discard")

                    # In learning mode, never discard — archive instead
                    if LEARNING_MODE and action == "discard":
                        action = "archive"
                        item["action"] = "archive"
                        item["reason"] = f"[learning mode] {item.get('reason', '')}"

                    if action in ("archive", "merge"):
                        if _archive_item(item, session_id):
                            archived_count += 1

                report["items_archived"] += archived_count
                report["steps_completed"].append(f"triage ({len(items)} items, {archived_count} archived)")

                # Step 3: Update master context
                try:
                    if triage_result.get("master_context_updates"):
                        updated = _update_master_context(triage_result, session_data)
                        report["master_updated"] = updated
                        if updated:
                            report["steps_completed"].append("master_context_update")
                        else:
                            report["steps_failed"].append("master_context_update")
                    else:
                        report["steps_completed"].append("master_context_update (no changes needed)")
                except Exception as e:
                    logger.error(f"Master context update failed: {e}")
                    report["steps_failed"].append(f"master_context_update: {e}")

            else:
                report["steps_failed"].append("triage_llm")

        except Exception as e:
            logger.error(f"Triage step failed: {e}")
            report["steps_failed"].append(f"triage: {e}")
    else:
        report["steps_completed"].append("triage (skipped, low significance)")

    # Step 4: Check promotions
    try:
        promotions = _check_promotions(session_data)
        report["promotions"] = promotions
        if promotions:
            report["steps_completed"].append(f"promotions ({len(promotions)} candidates)")
    except Exception as e:
        logger.error(f"Promotion check failed: {e}")
        report["steps_failed"].append(f"promotions: {e}")

    logger.info(
        f"Session {session_id} processed: "
        f"{len(report['steps_completed'])} completed, "
        f"{len(report['steps_failed'])} failed, "
        f"{report['items_archived']} archived, "
        f"master_updated={report['master_updated']}"
    )
    return report
