"""ChromaDB client service."""

import json
import chromadb
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any
from config import CHROMADB_HOST, CHROMADB_PORT, COLLECTIONS
from utils.logging_ import logger
from utils.degradation import get_manager as get_degradation_manager

_client: Optional[chromadb.HttpClient] = None


def get_client() -> chromadb.HttpClient:
    global _client
    if _client is None:
        _client = chromadb.HttpClient(host=CHROMADB_HOST, port=CHROMADB_PORT)
    return _client


def is_connected() -> bool:
    try:
        get_client().heartbeat()
        return True
    except Exception:
        return False


def ensure_collections() -> dict:
    client = get_client()
    result = {}
    for name, meta in COLLECTIONS.items():
        try:
            collection = client.get_or_create_collection(name=name, metadata={"description": meta["description"]})
            result[name] = collection
        except Exception as e:
            logger.error(f"Failed to ensure collection '{name}': {e}")
    return result


def get_collection_stats() -> dict:
    try:
        client = get_client()
        return {name: client.get_collection(name).count() for name in COLLECTIONS}
    except Exception:
        return {}


def add_document(collection_name: str, doc_id: str, content: str, metadata: Dict[str, Any] = None) -> bool:
    try:
        collection = get_client().get_collection(collection_name)
        meta = metadata or {}
        meta["created_at"] = datetime.now(timezone.utc).isoformat()
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
        collection.add(ids=[doc_id], documents=[content], metadatas=[clean_meta])
        return True
    except Exception as e:
        logger.error(f"Failed to add document to '{collection_name}': {e}")
        return False


def upsert_document(collection_name: str, doc_id: str, content: str, metadata: Dict[str, Any] = None) -> bool:
    try:
        collection = get_client().get_collection(collection_name)
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
        collection.upsert(ids=[doc_id], documents=[content], metadatas=[clean_meta])
        return True
    except Exception as e:
        logger.error(f"Failed to upsert in '{collection_name}': {e}")
        return False


def search_collection(collection_name: str, query: str, n_results: int = 5, where: Dict = None) -> List[Dict[str, Any]]:
    try:
        collection = get_client().get_collection(collection_name)
        kwargs = {"query_texts": [query], "n_results": min(n_results, collection.count() or 1)}
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
        return []


def take_snapshot(collection_name: str, doc_id: str) -> bool:
    try:
        source = get_client().get_collection(collection_name)
        existing = source.get(ids=[doc_id], include=["documents", "metadatas"])
        if not existing or not existing["ids"]:
            return False
        snapshot_id = f"{collection_name}:{doc_id}:{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
        snapshot_content = existing["documents"][0] if existing["documents"] else ""
        snapshot_meta = existing["metadatas"][0] if existing["metadatas"] else {}
        snapshot_meta["source_collection"] = collection_name
        snapshot_meta["source_id"] = doc_id
        snapshot_meta["snapshot_at"] = datetime.now(timezone.utc).isoformat()
        return add_document("snapshots", snapshot_id, snapshot_content, snapshot_meta)
    except Exception as e:
        logger.error(f"Snapshot failed: {e}")
        return False


def get_recent_sessions(n: int = 10) -> List[Dict[str, Any]]:
    try:
        collection = get_client().get_collection("sessions")
        count = collection.count()
        if count == 0:
            return []
        results = collection.get(include=["documents", "metadatas"], limit=min(n * 2, count))
        items = []
        if results and results["ids"]:
            for i, doc_id in enumerate(results["ids"]):
                items.append({"id": doc_id, "content": results["documents"][i] if results["documents"] else "", "metadata": results["metadatas"][i] if results["metadatas"] else {}})
        items.sort(key=lambda x: x.get("metadata", {}).get("created_at", ""), reverse=True)
        return items[:n]
    except Exception as e:
        logger.error(f"Failed to get recent sessions: {e}")
        return []


def get_chromadb(track_health: bool = True) -> chromadb.HttpClient:
    return get_client()
