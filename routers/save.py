"""context_save endpoint."""

import asyncio
import json
from datetime import datetime, timezone
from fastapi import APIRouter
from models import SaveRequest, SaveResponse, SessionRecord
from config import SESSIONS_DIR
from utils.session import session_filename
from utils.logging_ import logger
from utils.transcripts import store_transcript, truncate_for_haiku
from worker.processor import get_processor

router = APIRouter()


def _is_lite_save(request: SaveRequest) -> bool:
    return not any([request.decisions and len(request.decisions) > 0, request.failures and len(request.failures) > 0, request.files_changed and len(request.files_changed) > 0, request.next_steps and len(request.next_steps) > 0, request.tags and len(request.tags) > 0])


async def _extract_from_transcript(transcript: str, note: str) -> dict:
    try:
        from services.openrouter import get_openrouter
        client = get_openrouter()
        trimmed = truncate_for_haiku(transcript)
        result = await asyncio.to_thread(client.extract_from_transcript, trimmed, note)
        return result or {}
    except Exception as e:
        logger.warning(f"Transcript extraction failed: {e}")
    return {}


async def _extract_from_note(note: str) -> dict:
    try:
        from services.openrouter import get_openrouter
        client = get_openrouter()
        result = await asyncio.to_thread(client.extract_session_fields, note)
        return result or {}
    except Exception as e:
        logger.warning(f"Note extraction failed: {e}")
    return {}


@router.post("/api/save", response_model=SaveResponse)
async def context_save(request: SaveRequest):
    logger.info(f"context_save: session={request.session_id}, significance={request.significance.value}, has_transcript={request.transcript_text is not None}")
    now = datetime.now(timezone.utc)
    transcript_stored = False
    transcript_size_kb = None
    transcript_action = None
    if request.transcript_text:
        t_result = store_transcript(request.session_id, request.transcript_text)
        transcript_stored = t_result["stored"]
        transcript_size_kb = t_result["size_kb"]
        transcript_action = t_result["action"]
    summary = request.summary
    decisions = request.decisions or []
    failures = request.failures or []
    files_changed = request.files_changed or []
    next_steps = request.next_steps or []
    tags = request.tags or []
    significance = request.significance
    if request.transcript_text:
        extracted = await _extract_from_transcript(request.transcript_text, request.summary)
        if extracted:
            if _is_lite_save(request):
                summary = extracted.get("summary", summary)
                decisions = extracted.get("decisions", decisions)
                failures = extracted.get("failures", failures)
                files_changed = extracted.get("files_changed", files_changed)
                next_steps = extracted.get("next_steps", next_steps)
                tags = extracted.get("tags", tags)
            else:
                if not decisions: decisions = extracted.get("decisions", [])
                if not failures: failures = extracted.get("failures", [])
                if not files_changed: files_changed = extracted.get("files_changed", [])
                if not next_steps: next_steps = extracted.get("next_steps", [])
                if not tags: tags = extracted.get("tags", [])
            ext_sig = extracted.get("significance")
            if ext_sig and ext_sig in ("low", "medium", "high"):
                from models import Significance
                significance = Significance(ext_sig)
    elif _is_lite_save(request):
        extracted = await _extract_from_note(request.summary)
        if extracted:
            summary = extracted.get("summary", summary)
            decisions = extracted.get("decisions", [])
            failures = extracted.get("failures", [])
            files_changed = extracted.get("files_changed", [])
            next_steps = extracted.get("next_steps", [])
            tags = extracted.get("tags", [])
            ext_sig = extracted.get("significance")
            if ext_sig and ext_sig in ("low", "medium", "high"):
                from models import Significance
                significance = Significance(ext_sig)
    # Auto-detect source from session_id prefix or explicit field
    source = request.source or "mcp"
    if source == "mcp" and "-" in request.session_id:
        prefix = request.session_id.split("-")[0]
        source_map = {"ce": "claude-ai", "jerry": "jerry", "infra": "file-watcher", "n8n": "n8n"}
        source = source_map.get(prefix, "mcp")

    record = SessionRecord(session_id=request.session_id, created_at=now.isoformat(), summary=summary, significance=significance, files_changed=files_changed, decisions=decisions, failures=failures, project_states=request.project_states or {}, next_steps=next_steps, tags=tags, source=source, worker_processed=False, worker_processed_at=None)
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    filename = session_filename(request.session_id)
    filepath = SESSIONS_DIR / filename
    try:
        filepath.write_text(json.dumps(record.model_dump(), indent=2, default=str), encoding="utf-8")
    except Exception as e:
        logger.error(f"Failed to save session: {e}")
        raise
    processor = get_processor()
    processor.enqueue(request.session_id, str(filepath))
    parts = [f"Session saved ({significance.value})."]
    if transcript_action == "created": parts.append(f"Transcript stored ({transcript_size_kb} KB).")
    elif transcript_action == "updated": parts.append(f"Transcript updated ({transcript_size_kb} KB).")
    elif transcript_action == "skipped": parts.append("Transcript unchanged (dedup).")
    parts.append(f"Worker queued (depth: {len(processor.queue)}).")
    return SaveResponse(session_id=request.session_id, saved_at=now.isoformat(), session_file=str(filepath), worker_queued=True, message=" ".join(parts))
