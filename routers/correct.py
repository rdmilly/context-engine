"""context_correct endpoint.

Fix incorrect information in hot context (KB) and/or archive (ChromaDB).
Takes a snapshot before any modifications.
"""

from fastapi import APIRouter

from models import CorrectRequest, CorrectResponse, CorrectionScope
from services import kb_gateway, chromadb_client
from utils.logging_ import logger

router = APIRouter()


def _correct_hot_context(item: str, correction: str) -> bool:
    """Find and replace incorrect info in master-context.md."""
    try:
        content = kb_gateway.read_master_context()
        if content is None:
            logger.warning("Correct: master context not readable")
            return False

        # Try exact match first
        if item in content:
            new_content = content.replace(item, correction)
            return kb_gateway.write_master_context(
                new_content,
                commit_message=f"ContextEngine: correction applied - replaced '{item[:50]}...'"
            )

        # Try case-insensitive match
        lower_content = content.lower()
        lower_item = item.lower()
        if lower_item in lower_content:
            idx = lower_content.index(lower_item)
            new_content = content[:idx] + correction + content[idx + len(item):]
            return kb_gateway.write_master_context(
                new_content,
                commit_message=f"ContextEngine: correction applied (case-insensitive)"
            )

        logger.warning(f"Correct: '{item[:50]}' not found in master context")
        return False

    except Exception as e:
        logger.error(f"Correct hot context failed: {e}")
        return False


def _correct_archive(item: str, correction: str) -> int:
    """Search and correct matching entries in ChromaDB."""
    records_affected = 0
    collections_to_search = ["project_archive", "decisions", "failures", "sessions", "entities"]

    try:
        for col_name in collections_to_search:
            hits = chromadb_client.search_collection(col_name, item, n_results=5)
            for hit in hits:
                # Only correct very close matches
                if hit.get("distance", 999) > 0.5:
                    continue

                content = hit.get("content", "")
                doc_id = hit.get("id", "")

                # Take snapshot before modifying
                chromadb_client.take_snapshot(col_name, doc_id)

                # Apply correction
                if item in content:
                    new_content = content.replace(item, correction)
                else:
                    # Append correction note
                    new_content = content + f"\n[CORRECTION: {correction}]"

                metadata = hit.get("metadata", {})
                metadata["corrected"] = "true"
                metadata["correction_note"] = f"Replaced: {item[:100]}"

                success = chromadb_client.upsert_document(
                    col_name, doc_id, new_content, metadata
                )
                if success:
                    records_affected += 1
                    logger.info(f"Correct: updated {doc_id} in {col_name}")

    except Exception as e:
        logger.error(f"Correct archive failed: {e}")

    return records_affected


@router.post("/api/correct", response_model=CorrectResponse)
async def context_correct(request: CorrectRequest):
    """Correct wrong information in hot context and/or archive."""
    logger.info(f"context_correct: scope={request.scope.value}, item='{request.item[:50]}...'")

    hot_updated = False
    archive_updated = False
    records_affected = 0

    if request.scope in (CorrectionScope.HOT, CorrectionScope.BOTH):
        hot_updated = _correct_hot_context(request.item, request.correction)

    if request.scope in (CorrectionScope.ARCHIVE, CorrectionScope.BOTH):
        records_affected = _correct_archive(request.item, request.correction)
        archive_updated = records_affected > 0

    # Build message
    parts = []
    if hot_updated:
        parts.append("master context updated")
    if archive_updated:
        parts.append(f"{records_affected} archive record(s) corrected")
    if not parts:
        parts.append("no matching content found to correct")

    message = f"Correction applied: {'; '.join(parts)}."
    logger.info(f"context_correct: {message}")

    return CorrectResponse(
        item=request.item,
        correction=request.correction,
        hot_updated=hot_updated,
        archive_updated=archive_updated,
        records_affected=records_affected,
        message=message,
    )
