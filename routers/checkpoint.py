"""context_checkpoint endpoint.

Lightweight mid-session save. Accepts a brief note and optional transcript.
Transcript can be provided as:
  - transcript_path: path to a file on VPS (read from filesystem)
  - transcript_text: raw text passed directly (no file write needed)

If a transcript is provided, Haiku parses it for richer extraction.
Otherwise falls back to note-only extraction.

Transcripts are gzip-compressed, deduplicated by session_id, and stored permanently.
Cost: ~$0.04 per save with transcript, ~$0.02 without.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter

from models import (
    CheckpointRequest, CheckpointResponse,
    SessionRecord, Significance,
)
from config import SESSIONS_DIR
from utils.session import session_filename
from utils.logging_ import logger
from utils.transcripts import store_transcript, truncate_for_haiku
from worker.processor import get_processor

router = APIRouter()


def _read_transcript_file(path: str) -> str | None:
    """Read a transcript file from the filesystem."""
    try:
        p = Path(path)
        if not p.exists():
            logger.warning(f"Transcript not found: {path}")
            return None
        content = p.read_text(encoding="utf-8")
        logger.info(f"Read transcript: {len(content)} chars from {path}")
        return content
    except Exception as e:
        logger.warning(f"Failed to read transcript {path}: {e}")
        return None


async def _extract_fields(note: str, transcript: str | None = None) -> dict:
    """Call Haiku to extract structured fields.

    If transcript is provided, uses the richer transcript-aware extraction.
    Otherwise falls back to note-only extraction.

    LLM calls are run in a thread to avoid blocking the event loop.
    """
    import asyncio
    try:
        from services.openrouter import get_openrouter
        client = get_openrouter()

        if transcript:
            trimmed = truncate_for_haiku(transcript)
            result = await asyncio.to_thread(client.extract_from_transcript, trimmed, note)
            source = "transcript"
        else:
            result = await asyncio.to_thread(client.extract_session_fields, note)
            source = "note"

        if result:
            logger.info(
                f"Checkpoint extraction ({source}): "
                f"{len(result.get('tags', []))} tags, "
                f"{len(result.get('decisions', []))} decisions, "
                f"{len(result.get('failures', []))} failures"
            )
            return result
    except Exception as e:
        logger.warning(f"Checkpoint extraction failed: {e}")
    return {}


@router.post("/api/checkpoint", response_model=CheckpointResponse)
async def context_checkpoint(request: CheckpointRequest):
    """Lightweight mid-session save with optional transcript.

    Three modes:
    - Note only: Just session_id + brief note. Haiku extracts from note.
    - Transcript text: session_id + note + transcript_text. Text passed directly.
    - Transcript path: session_id + note + transcript_path. Read from VPS file.

    Transcripts are stored permanently with smart dedup:
    - Same session, longer transcript = overwrite (conversation continued)
    - Same session, same/shorter transcript = skip (already have it)
    """
    logger.info(
        f"checkpoint: session={request.session_id}, "
        f"significance={request.significance.value}, "
        f"has_transcript_path={request.transcript_path is not None}, "
        f"has_transcript_text={request.transcript_text is not None}"
    )

    now = datetime.now(timezone.utc)

    # Resolve transcript from either source
    transcript = None
    transcript_stored = False
    transcript_size_kb = None
    transcript_action = None

    if request.transcript_text:
        transcript = request.transcript_text
        logger.info(f"Using direct transcript text: {len(transcript)} chars")
    elif request.transcript_path:
        transcript = _read_transcript_file(request.transcript_path)

    # Store transcript with dedup
    if transcript:
        t_result = store_transcript(request.session_id, transcript)
        transcript_stored = t_result["stored"]
        transcript_size_kb = t_result["size_kb"]
        transcript_action = t_result["action"]

    # Extract structured fields via Haiku
    extracted = await _extract_fields(request.note, transcript)

    summary = extracted.get("summary", request.note)
    decisions = extracted.get("decisions", [])
    failures = extracted.get("failures", [])
    files_changed = extracted.get("files_changed", [])
    next_steps = extracted.get("next_steps", [])
    tags = extracted.get("tags", [])
    significance = request.significance

    # Let Haiku override significance if it has strong opinion
    ext_sig = extracted.get("significance")
    if ext_sig and ext_sig in ("low", "medium", "high"):
        significance = Significance(ext_sig)

    # Build session record
    record = SessionRecord(
        session_id=request.session_id,
        created_at=now.isoformat(),
        summary=summary,
        significance=significance,
        files_changed=files_changed,
        decisions=decisions,
        failures=failures,
        project_states={},
        next_steps=next_steps,
        tags=tags,
        worker_processed=False,
        worker_processed_at=None,
    )

    # Write to cold storage
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    filename = session_filename(request.session_id)
    filepath = SESSIONS_DIR / filename

    try:
        filepath.write_text(
            json.dumps(record.model_dump(), indent=2, default=str),
            encoding="utf-8",
        )
        logger.info(f"Checkpoint saved: {filepath}")
    except Exception as e:
        logger.error(f"Failed to save checkpoint: {e}")
        raise

    # Queue for worker processing
    processor = get_processor()
    processor.enqueue(request.session_id, str(filepath))

    # Build message
    parts = [f"Checkpoint saved ({significance.value})."]
    if transcript_action == "created":
        parts.append(f"Transcript archived ({transcript_size_kb} KB).")
    elif transcript_action == "updated":
        parts.append(f"Transcript updated ({transcript_size_kb} KB).")
    elif transcript_action == "skipped":
        parts.append("Transcript unchanged (dedup).")
    parts.append("Worker queued.")

    return CheckpointResponse(
        session_id=request.session_id,
        saved_at=now.isoformat(),
        session_file=str(filepath),
        transcript_stored=transcript_stored or (transcript_action == "skipped"),
        transcript_size_kb=transcript_size_kb,
        worker_queued=True,
        message=" ".join(parts),
    )
