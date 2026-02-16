"""context_save endpoint."""

import asyncio
import json
from datetime import datetime, timezone
from fastapi import APIRouter
from models import SaveRequest, SaveResponse, SessionRecord, Significance
from config import SESSIONS_DIR
from utils.session import session_filename
from utils.logging_ import logger
from utils.transcripts import store_transcript, truncate_for_haiku
from worker.processor import get_processor

router = APIRouter()


def _is_lite_save(request):
    return not any([request.decisions, request.failures, request.files_changed, request.next_steps, request.tags])


async def _extract_from_transcript(transcript, note):
    try:
        from services.openrouter import get_openrouter
        client = get_openrouter()
        return await asyncio.to_thread(client.extract_from_transcript, truncate_for_haiku(transcript), note) or {}
    except Exception as e:
        logger.warning(f"Transcript extraction failed: {e}")
        return {}


async def _extract_from_note(note):
    try:
        from services.openrouter import get_openrouter
        return await asyncio.to_thread(get_openrouter().extract_session_fields, note) or {}
    except Exception as e:
        logger.warning(f"Note extraction failed: {e}")
        return {}


@router.post("/api/save", response_model=SaveResponse)
async def context_save(request: SaveRequest):
    now = datetime.now(timezone.utc)
    transcript_stored = False
    transcript_size_kb = None
    transcript_action = None
    if request.transcript_text:
        t = store_transcript(request.session_id, request.transcript_text)
        transcript_stored, transcript_size_kb, transcript_action = t["stored"], t["size_kb"], t["action"]

    summary, decisions, failures = request.summary, request.decisions or [], request.failures or []
    files_changed, next_steps, tags = request.files_changed or [], request.next_steps or [], request.tags or []
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
            if ext_sig in ("low", "medium", "high"): significance = Significance(ext_sig)
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
            if ext_sig in ("low", "medium", "high"): significance = Significance(ext_sig)

    record = SessionRecord(session_id=request.session_id, created_at=now.isoformat(), summary=summary,
                           significance=significance, files_changed=files_changed, decisions=decisions,
                           failures=failures, project_states=request.project_states or {},
                           next_steps=next_steps, tags=tags)
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    filepath = SESSIONS_DIR / session_filename(request.session_id)
    filepath.write_text(json.dumps(record.model_dump(), indent=2, default=str), encoding="utf-8")
    processor = get_processor()
    processor.enqueue(request.session_id, str(filepath))

    parts = [f"Session saved ({significance.value})."]
    if transcript_action == "created": parts.append(f"Transcript stored ({transcript_size_kb} KB).")
    elif transcript_action == "updated": parts.append(f"Transcript updated ({transcript_size_kb} KB).")
    parts.append(f"Worker queued (depth: {len(processor.queue)}).")
    return SaveResponse(session_id=request.session_id, saved_at=now.isoformat(),
                        session_file=str(filepath), worker_queued=True, message=" ".join(parts))
