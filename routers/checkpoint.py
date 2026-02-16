"""context_checkpoint endpoint."""

import json
from datetime import datetime, timezone
from pathlib import Path
from fastapi import APIRouter
from models import CheckpointRequest, CheckpointResponse, SessionRecord, Significance
from config import SESSIONS_DIR
from utils.session import session_filename
from utils.logging_ import logger
from utils.transcripts import store_transcript, truncate_for_haiku
from worker.processor import get_processor

router = APIRouter()


def _read_transcript_file(path: str):
    try:
        p = Path(path)
        if not p.exists(): return None
        return p.read_text(encoding="utf-8")
    except Exception: return None


async def _extract_fields(note, transcript=None):
    import asyncio
    try:
        from services.openrouter import get_openrouter
        client = get_openrouter()
        if transcript:
            result = await asyncio.to_thread(client.extract_from_transcript, truncate_for_haiku(transcript), note)
        else:
            result = await asyncio.to_thread(client.extract_session_fields, note)
        return result or {}
    except Exception as e:
        logger.warning(f"Extraction failed: {e}")
        return {}


@router.post("/api/checkpoint", response_model=CheckpointResponse)
async def context_checkpoint(request: CheckpointRequest):
    now = datetime.now(timezone.utc)
    transcript = request.transcript_text or (_read_transcript_file(request.transcript_path) if request.transcript_path else None)
    transcript_stored = False
    transcript_size_kb = None
    transcript_action = None
    if transcript:
        t_result = store_transcript(request.session_id, transcript)
        transcript_stored = t_result["stored"]
        transcript_size_kb = t_result["size_kb"]
        transcript_action = t_result["action"]
    extracted = await _extract_fields(request.note, transcript)
    significance = request.significance
    ext_sig = extracted.get("significance")
    if ext_sig and ext_sig in ("low", "medium", "high"): significance = Significance(ext_sig)
    record = SessionRecord(session_id=request.session_id, created_at=now.isoformat(),
                           summary=extracted.get("summary", request.note), significance=significance,
                           files_changed=extracted.get("files_changed", []), decisions=extracted.get("decisions", []),
                           failures=extracted.get("failures", []), next_steps=extracted.get("next_steps", []),
                           tags=extracted.get("tags", []))
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    filepath = SESSIONS_DIR / session_filename(request.session_id)
    filepath.write_text(json.dumps(record.model_dump(), indent=2, default=str), encoding="utf-8")
    get_processor().enqueue(request.session_id, str(filepath))
    parts = [f"Checkpoint saved ({significance.value})."]
    if transcript_action == "created": parts.append(f"Transcript archived ({transcript_size_kb} KB).")
    elif transcript_action == "updated": parts.append(f"Transcript updated ({transcript_size_kb} KB).")
    parts.append("Worker queued.")
    return CheckpointResponse(session_id=request.session_id, saved_at=now.isoformat(),
                              session_file=str(filepath), transcript_stored=transcript_stored or (transcript_action == "skipped"),
                              transcript_size_kb=transcript_size_kb, worker_queued=True, message=" ".join(parts))
