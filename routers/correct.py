"""context_correct endpoint."""

from fastapi import APIRouter
from models import CorrectRequest, CorrectResponse, CorrectionScope
from services import kb_gateway, chromadb_client
from utils.logging_ import logger

router = APIRouter()


def _correct_hot_context(item: str, correction: str) -> bool:
    try:
        content = kb_gateway.read_master_context()
        if content is None: return False
        if item in content:
            return kb_gateway.write_master_context(content.replace(item, correction), commit_message=f"ContextEngine: correction applied")
        lower_content = content.lower()
        if item.lower() in lower_content:
            idx = lower_content.index(item.lower())
            return kb_gateway.write_master_context(content[:idx] + correction + content[idx + len(item):], commit_message="ContextEngine: correction (case-insensitive)")
        return False
    except Exception as e:
        logger.error(f"Correct hot context failed: {e}")
        return False


def _correct_archive(item: str, correction: str) -> int:
    records_affected = 0
    for col_name in ["project_archive", "decisions", "failures", "sessions", "entities"]:
        try:
            hits = chromadb_client.search_collection(col_name, item, n_results=5)
            for hit in hits:
                if hit.get("distance", 999) > 0.5: continue
                chromadb_client.take_snapshot(col_name, hit["id"])
                content = hit.get("content", "")
                new_content = content.replace(item, correction) if item in content else content + f"\n[CORRECTION: {correction}]"
                metadata = hit.get("metadata", {})
                metadata["corrected"] = "true"
                if chromadb_client.upsert_document(col_name, hit["id"], new_content, metadata):
                    records_affected += 1
        except Exception as e:
            logger.error(f"Correct archive failed for {col_name}: {e}")
    return records_affected


@router.post("/api/correct", response_model=CorrectResponse)
async def context_correct(request: CorrectRequest):
    hot_updated = False
    records_affected = 0
    if request.scope in (CorrectionScope.HOT, CorrectionScope.BOTH):
        hot_updated = _correct_hot_context(request.item, request.correction)
    if request.scope in (CorrectionScope.ARCHIVE, CorrectionScope.BOTH):
        records_affected = _correct_archive(request.item, request.correction)
    parts = []
    if hot_updated: parts.append("master context updated")
    if records_affected > 0: parts.append(f"{records_affected} archive record(s) corrected")
    if not parts: parts.append("no matching content found")
    return CorrectResponse(item=request.item, correction=request.correction, hot_updated=hot_updated,
                           archive_updated=records_affected > 0, records_affected=records_affected,
                           message=f"Correction: {'; '.join(parts)}")
