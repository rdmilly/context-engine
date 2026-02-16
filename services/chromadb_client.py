"""ChromaDB client service.

Phase 1: Connection management + health check.
Phase 2: Collection operations, writes, search, snapshots.
"""

import json
import chromadb
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any

from config import CHROMADB_HOST, CHROMADB_PORT, COLLECTIONS
from utils.logging_ import logger
from utils.degradation import get_manager as get_degradation_manager


_client: Optional[chromadb.HttpClient] = None


def get_client() -> chromadb.HttpClient:
    """Get or create ChromaDB HTTP client."""
    global _client
    if _client is None:
        _client = chromadb.HttpClient(
            host=CHROMADB_HOST,
            port=CHROMADB_PORT,
        )
        logger.info(f"ChromaDB client connected: {CHROMADB_HOST}:{CHROMADB_PORT}")
    return _client


def is_connected() -> bool:
    """Check if ChromaDB is reachable."""
    try:
        client = get_client()
        client.heartbeat()
        return True
    except Exception as e:
        logger.warning(f"ChromaDB not reachable: {e}")
        return False


def ensure_collections() -> dict:
    """Ensure all required collections exist."""
    client = get_client()
    result = {}
    dm = get_degradation_manager()
    for name, meta in COLLECTIONS.items():
        try:
            collection = client.get_or_create_collection(
                name=name,
                metadata={"description": meta["description"]},
            )
            result[name] = collection
            logger.info(f"Collection '{name}': {collection.count()} documents")
        except Exception as e:
            logger.error(f"Failed to ensure collection '{name}': {e}")
    return result


def get_collection_stats() -> dict:
    """Get stats for all collections."""
    try:
        client = get_client()
        stats = {}
        for name in COLLECTIONS:
            try:
                coll = client.get_collection(name)
                stats[name] = coll.count()
            except Exception:
                stats[name] = -1
        return stats
    except Exception as e:
        logger.error(f"Failed to get collection stats: {e}")
        return {}


# ─── Phase 2: Write Operations ──────────────────────────────

def add_document(
    collection_name: str,
    doc_id: str,
    content: str,
    metadata: Dict[str, Any] = None,
) -> bool:
    """Add a document to a collection.

    Args:
        collection_name: Target collection (must be in COLLECTIONS)
        doc_id: Unique document ID
        content: Text content (used for embedding)
        metadata: Additional metadata dict
    """
    try:
        client = get_client()
        collection = client.get_collection(collection_name)

        meta = metadata or {}
        meta["created_at"] = datetime.now(timezone.utc).isoformat()

        # ChromaDB metadata values must be str, int, float, or bool
        clean_meta = {}
        for k, v in meta.items():
            if isinstance(v, (str, int, float, bool)):
                clean_meta[k] = v
            elif isinstance(v, list):
                clean_meta[k] = json.dumps(v)
            elif v is None:
                clean_meta[k] = ""
            else:
                clean_meta[k] = str(v)

        collection.add(
            ids=[doc_id],
            documents=[content],
            metadatas=[clean_meta],
        )
        logger.info(f"Added doc '{doc_id}' to '{collection_name}' ({len(content)} chars)")
        return True
    except Exception as e:
        logger.error(f"Failed to add document to '{collection_name}': {e}")
        return False


def upsert_document(
    collection_name: str,
    doc_id: str,
    content: str,
    metadata: Dict[str, Any] = None,
) -> bool:
    """Add or update a document in a collection."""
    try:
        client = get_client()
        collection = client.get_collection(collection_name)

        meta = metadata or {}
        meta["updated_at"] = datetime.now(timezone.utc).isoformat()

        clean_meta = {}
        for k, v in meta.items():
            if isinstance(v, (str, int, float, bool)):
                clean_meta[k] = v
            elif isinstance(v, list):
                clean_meta[k] = json.dumps(v)
            elif v is None:
                clean_meta[k] = ""
            else:
                clean_meta[k] = str(v)

        collection.upsert(
            ids=[doc_id],
            documents=[content],
            metadatas=[clean_meta],
        )
        logger.info(f"Upserted doc '{doc_id}' in '{collection_name}'")
        return True
    except Exception as e:
        logger.error(f"Failed to upsert document in '{collection_name}': {e}")
        return False


def search_collection(
    collection_name: str,
    query: str,
    n_results: int = 5,
    where: Dict = None,
) -> List[Dict[str, Any]]:
    """Search a collection by semantic similarity.

    Returns list of {id, content, metadata, distance}.
    """
    try:
        client = get_client()
        collection = client.get_collection(collection_name)

        kwargs = {
            "query_texts": [query],
            "n_results": min(n_results, collection.count() or 1),
        }
        if where:
            kwargs["where"] = where

        results = collection.query(**kwargs)

        hits = []
        if results and results["ids"] and results["ids"][0]:
            for i, doc_id in enumerate(results["ids"][0]):
                hits.append({
                    "id": doc_id,
                    "content": results["documents"][0][i] if results["documents"] else "",
                    "metadata": results["metadatas"][0][i] if results["metadatas"] else {},
                    "distance": results["distances"][0][i] if results["distances"] else None,
                })
        get_degradation_manager().mark_healthy("chromadb")
        return hits
    except Exception as e:
        get_degradation_manager().mark_unhealthy("chromadb", str(e))
        logger.error(f"Search failed in '{collection_name}': {e}")
        return []


def take_snapshot(collection_name: str, doc_id: str) -> bool:
    """Save a pre-write snapshot for rollback safety.

    Copies the current state of a document to the snapshots collection.
    """
    try:
        client = get_client()
        source = client.get_collection(collection_name)

        # Try to get existing document
        existing = source.get(ids=[doc_id], include=["documents", "metadatas"])
        if not existing or not existing["ids"]:
            return False  # Nothing to snapshot

        snapshot_id = f"{collection_name}:{doc_id}:{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
        snapshot_content = existing["documents"][0] if existing["documents"] else ""
        snapshot_meta = existing["metadatas"][0] if existing["metadatas"] else {}
        snapshot_meta["source_collection"] = collection_name
        snapshot_meta["source_id"] = doc_id
        snapshot_meta["snapshot_at"] = datetime.now(timezone.utc).isoformat()

        return add_document("snapshots", snapshot_id, snapshot_content, snapshot_meta)
    except Exception as e:
        logger.error(f"Snapshot failed for {collection_name}:{doc_id}: {e}")
        return False


def get_recent_sessions(n: int = 10) -> List[Dict[str, Any]]:
    """Get the N most recent session summaries from ChromaDB.

    Used for promotion detection.
    """
    try:
        client = get_client()
        collection = client.get_collection("sessions")
        count = collection.count()
        if count == 0:
            return []

        # Get all and sort by created_at (ChromaDB doesn't support ordering)
        results = collection.get(
            include=["documents", "metadatas"],
            limit=min(n * 2, count),  # Get extra to sort
        )

        items = []
        if results and results["ids"]:
            for i, doc_id in enumerate(results["ids"]):
                items.append({
                    "id": doc_id,
                    "content": results["documents"][i] if results["documents"] else "",
                    "metadata": results["metadatas"][i] if results["metadatas"] else {},
                })

        # Sort by created_at descending
        items.sort(
            key=lambda x: x.get("metadata", {}).get("created_at", ""),
            reverse=True,
        )
        return items[:n]
    except Exception as e:
        logger.error(f"Failed to get recent sessions: {e}")
        return []


# Alias for worker compatibility
def get_chromadb(track_health: bool = True) -> chromadb.HttpClient:
    """Alias for get_client, used by worker processor."""
    return get_client()

