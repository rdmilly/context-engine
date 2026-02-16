"""context_search endpoint.

Semantic search across all ChromaDB collections.
"""

from fastapi import APIRouter

from models import SearchRequest, SearchResponse
from config import COLLECTIONS, resolve_collection_name
from services import chromadb_client
from utils.logging_ import logger

router = APIRouter()


@router.post("/api/search", response_model=SearchResponse)
async def context_search(request: SearchRequest):
    """Search archive collections for relevant context."""
    logger.info(f"context_search: query='{request.query}', collections={request.collections}")

    # Determine which collections to search
    if request.collections:
        target_collections = [resolve_collection_name(c) for c in request.collections]
        # Deduplicate
        target_collections = list(dict.fromkeys(target_collections))
    else:
        # Search all content collections (skip snapshots and patterns)
        target_collections = ["project_archive", "decisions", "failures", "entities", "sessions"]

    all_results = []

    if not chromadb_client.is_connected():
        return SearchResponse(
            query=request.query,
            results=[],
            total_results=0,
            collections_searched=[],
        )

    for col_name in target_collections:
        try:
            hits = chromadb_client.search_collection(
                col_name,
                request.query,
                n_results=request.limit,
            )
            for hit in hits:
                # Apply distance threshold
                if hit.get("distance", 999) < 1.8:
                    result = {
                        "collection": col_name,
                        "id": hit["id"],
                        "content": hit["content"],
                        "metadata": hit.get("metadata", {}),
                        "distance": hit.get("distance"),
                        "relevance": round(1.0 - (hit.get("distance", 1.0) / 2.0), 3),
                    }

                    # Apply date filters if provided
                    timestamp = hit.get("metadata", {}).get("timestamp", "")
                    if request.date_after and timestamp and timestamp < request.date_after:
                        continue
                    if request.date_before and timestamp and timestamp > request.date_before:
                        continue

                    # Apply tag filter if provided
                    if request.tags:
                        item_tags = hit.get("metadata", {}).get("tags", "")
                        if isinstance(item_tags, str):
                            item_tags = [t.strip() for t in item_tags.split(",") if t.strip()]
                        if not any(t in item_tags for t in request.tags):
                            continue

                    all_results.append(result)
        except Exception as e:
            logger.warning(f"Search failed for collection '{col_name}': {e}")

    # Sort by relevance, limit total results
    all_results.sort(key=lambda x: x.get("relevance", 0), reverse=True)
    all_results = all_results[:request.limit]

    logger.info(f"context_search: {len(all_results)} results across {len(target_collections)} collections")

    return SearchResponse(
        query=request.query,
        results=all_results,
        total_results=len(all_results),
        collections_searched=target_collections,
    )


@router.get("/api/search")
async def context_search_get(query: str, collections: str = None, limit: int = 10):
    """GET wrapper for search — used by dashboard."""
    from models import SearchRequest
    cols = [c.strip() for c in collections.split(",")] if collections else None
    return await context_search(SearchRequest(query=query, collections=cols, limit=limit))
