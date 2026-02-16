"""
Retention policy for ChromaDB collections.

Prunes old documents based on configurable retention periods.
Runs automatically from the worker loop (e.g., daily).

Default retention:
- sessions: 6 months
- project_archive: 12 months
- decisions: 12 months
- failures: 12 months
- entities: no expiry (always relevant)
- patterns: 12 months
- snapshots: 30 days (compression safety net only)
- anomalies: 6 months
"""

import logging
import json
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger("context-engine")

# Default retention periods in days
DEFAULT_RETENTION = {
    "sessions": 180,          # 6 months
    "project_archive": 365,   # 1 year
    "decisions": 365,         # 1 year
    "failures": 365,          # 1 year
    "entities": 0,            # 0 = never prune
    "patterns": 365,          # 1 year
    "snapshots": 30,          # 1 month
    "anomalies": 180,         # 6 months
}


def prune_collection(
    client,
    collection_name: str,
    max_age_days: int,
    dry_run: bool = False,
) -> dict:
    """
    Remove documents older than max_age_days from a collection.

    Looks for 'created_at', 'timestamp', or 'updated_at' in metadata.

    Returns: {"collection": str, "checked": int, "pruned": int, "dry_run": bool}
    """
    if max_age_days <= 0:
        return {"collection": collection_name, "checked": 0, "pruned": 0, "skipped": True}

    try:
        col = client.get_collection(collection_name)
    except Exception:
        return {"collection": collection_name, "checked": 0, "pruned": 0, "error": "not found"}

    count = col.count()
    if count == 0:
        return {"collection": collection_name, "checked": 0, "pruned": 0}

    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    cutoff_str = cutoff.isoformat()

    # Fetch all documents (ChromaDB doesn't support date range queries natively)
    # Batch in chunks of 500
    to_delete = []
    batch_size = 500
    offset = 0

    while offset < count:
        try:
            results = col.get(
                limit=batch_size,
                offset=offset,
                include=["metadatas"],
            )
        except Exception as e:
            logger.warning(f"Retention: error reading {collection_name} at offset {offset}: {e}")
            break

        ids = results.get("ids", [])
        metadatas = results.get("metadatas", [])

        if not ids:
            break

        for doc_id, meta in zip(ids, metadatas):
            if not meta:
                continue

            # Find a timestamp field
            ts_str = meta.get("created_at") or meta.get("timestamp") or meta.get("updated_at")
            if not ts_str:
                continue

            try:
                # Handle various formats
                ts_str = str(ts_str)
                if ts_str < cutoff_str:
                    to_delete.append(doc_id)
            except Exception:
                continue

        offset += len(ids)

    result = {
        "collection": collection_name,
        "checked": count,
        "pruned": len(to_delete),
        "dry_run": dry_run,
    }

    if to_delete and not dry_run:
        # Delete in batches
        for i in range(0, len(to_delete), 100):
            batch = to_delete[i:i+100]
            try:
                col.delete(ids=batch)
            except Exception as e:
                logger.error(f"Retention: delete failed for {collection_name}: {e}")
                result["error"] = str(e)
                break

        logger.info(f"Retention: pruned {len(to_delete)}/{count} docs from {collection_name} (>{max_age_days} days)")

    return result


def run_retention(
    client,
    retention_overrides: Optional[dict] = None,
    dry_run: bool = False,
) -> list:
    """
    Run retention across all collections.

    Args:
        client: ChromaDB client
        retention_overrides: dict of {collection: days} to override defaults
        dry_run: if True, report what would be pruned without deleting

    Returns: list of per-collection results
    """
    retention = {**DEFAULT_RETENTION}
    if retention_overrides:
        retention.update(retention_overrides)

    results = []
    total_pruned = 0

    for col_name, max_days in retention.items():
        result = prune_collection(client, col_name, max_days, dry_run=dry_run)
        results.append(result)
        total_pruned += result.get("pruned", 0)

    action = "would prune" if dry_run else "pruned"
    logger.info(f"Retention: {action} {total_pruned} total documents across {len(results)} collections")

    return results
