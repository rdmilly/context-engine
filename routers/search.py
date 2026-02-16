"""context_search endpoint."""

from fastapi import APIRouter
from models import SearchRequest, SearchResponse
from config import COLLECTIONS, resolve_collection_name
from services import chromadb_client
from utils.logging_ import logger

router = APIRouter()


@router.post("/api/search", response_model=SearchResponse)
async def context_search(request: SearchRequest):
    if request.collections:
        target_collections = list(dict.fromkeys([resolve_collection_name(c) for c in request.collections]))
    else:
        target_collections = ["project_archive", "decisions", "failures", "entities", "sessions"]

    all_results = []
    if not chromadb_client.is_connected():
        return SearchResponse(query=request.query, results=[], total_results=0, collections_searched=[])

    for col_name in target_collections:
        try:
            hits = chromadb_client.search_collection(col_name, request.query, n_results=request.limit)
            for hit in hits:
                if hit.get("distance", 999) < 1.8:
                    result = {"collection": col_name, "id": hit["id"], "content": hit["content"],
                              "metadata": hit.get("metadata", {}), "distance": hit.get("distance"),
                              "relevance": round(1.0 - (hit.get("distance", 1.0) / 2.0), 3)}
                    timestamp = hit.get("metadata", {}).get("timestamp", "")
                    if request.date_after and timestamp and timestamp < request.date_after: continue
                    if request.date_before and timestamp and timestamp > request.date_before: continue
                    if request.tags:
                        item_tags = hit.get("metadata", {}).get("tags", "")
                        if isinstance(item_tags, str): item_tags = [t.strip() for t in item_tags.split(",") if t.strip()]
                        if not any(t in item_tags for t in request.tags): continue
                    all_results.append(result)
        except Exception as e:
            logger.warning(f"Search failed for '{col_name}': {e}")

    all_results.sort(key=lambda x: x.get("relevance", 0), reverse=True)
    return SearchResponse(query=request.query, results=all_results[:request.limit],
                          total_results=len(all_results[:request.limit]), collections_searched=target_collections)


@router.get("/api/search")
async def context_search_get(query: str, collections: str = None, limit: int = 10):
    cols = [c.strip() for c in collections.split(",")] if collections else None
    return await context_search(SearchRequest(query=query, collections=cols, limit=limit))
