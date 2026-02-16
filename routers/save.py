"""context_save endpoint.

Writes session data to cold storage (Tier 3) and queues for worker processing.
Supports "lite save" — just session_id + brief note, Haiku extracts the rest.

If transcript_text is provided:
- Full transcript is stored permanently (gzip compressed, deduplicated)
- Haiku uses transcript for richer extraction (even on full saves)
"""

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
    """Check if this is a lite save (only summary provided, no structured fields)."""
    has_decisions = request.decisions and len(request.decisions) > 0
    has_failures = request.failures and len(request.failures) > 0
    has_files = request.files_changed and len(request.files_changed) > 0
    has_next = request.next_steps and len(request.next_steps) > 0
    has_tags = request.tags and len(request.tags) > 0
    return not any([has_decisions, has_failures, has_files, has_next, has_tags])


async def _extract_from_transcript(transcript: str, note: str) -> dict:
    """Call Haiku to extract structured fields from full transcript.

    LLM call runs in a thread to avoid blocking the event loop.
    """
    try:
        from services.openrouter import get_openrouter
        client = get_openrouter()
        trimmed = truncate_for_haiku(transcript)
        result = await asyncio.to_thread(client.extract_from_transcript, trimmed, note)
        if result:
            logger.info(
                f"Transcript extraction: {len(result.get('tags', []))} tags, "
                f"{len(result.get('decisions', []))} decisions, "
                f"{len(result.get('failures', []))} failures"
            )
            return result
    except Exception as e:
        logger.warning(f"Transcript extraction failed: {e}")
    return {}


async def _extract_from_note(note: str) -> dict:
    """Call Haiku to extract structured fields from a brief note.

    LLM call runs in a thread to avoid blocking the event loop.
    """
    try:
        from services.openrouter import get_openrouter
        client = get_openrouter()
        result = await asyncio.to_thread(client.extract_session_fields, note)
        if result:
            logger.info(
                f"Note extraction: {len(result.get('tags', []))} tags, "
                f"{len(result.get('decisions', []))} decisions"
            )
            return result
    except Exception as e:
        logger.warning(f"Note extraction failed (using raw note): {e}")
    return {}


@router.post("/api/save", response_model=SaveResponse)
async def context_save(request: SaveRequest):
    """Save session context to cold storage and queue for worker processing.

    Supports three modes:
    - Full: All structured fields provided (backward compatible)
    - Lite: Just session_id + summary note, Haiku extracts the rest
    - Transcript: Any mode + transcript_text, Haiku uses full conversation

    When transcript_text is provided, the full transcript is always stored
    permanently (gzip compressed, deduplicated by session_id) and Haiku
    uses it for richer extraction regardless of whether structured fields
    were also provided.
    """
    logger.info(
        f"context_save: session={request.session_id}, "
        f"significance={request.significance.value}, "
        f"has_transcript={request.transcript_text is not None}"
    )

    now = datetime.now(timezone.utc)

    # Store transcript if provided (dedup handled automatically)
    transcript_stored = False
    transcript_size_kb = None
    transcript_action = None

    if request.transcript_text:
        t_result = store_transcript(request.session_id, request.transcript_text)
        transcript_stored = t_result["stored"]
        transcript_size_kb = t_result["size_kb"]
        transcript_action = t_result["action"]

    # Start with provided fields
    summary = request.summary
    decisions = request.decisions or []
    failures = request.failures or []
    files_changed = request.files_changed or []
    next_steps = request.next_steps or []
    tags = request.tags or []
    significance = request.significance

    # If transcript available, ALWAYS use it for Haiku extraction
    # (even if structured fields were provided — transcript gives richer context)
    if request.transcript_text:
        logger.info("Using transcript for Haiku extraction")
        extracted = await _extract_from_transcript(request.transcript_text, request.summary)
        if extracted:
            # On lite save: replace everything with Haiku's extraction
            # On full save: merge — use Haiku for anything not explicitly provided
            if _is_lite_save(request):
                summary = extracted.get("summary", summary)
                decisions = extracted.get("decisions", decisions)
                failures = extracted.get("failures", failures)
                files_changed = extracted.get("files_changed", files_changed)
                next_steps = extracted.get("next_steps", next_steps)
                tags = extracted.get("tags", tags)
            else:
                # Full save: Haiku supplements but doesn't override explicit fields
                if not decisions:
                    decisions = extracted.get("decisions", [])
                if not failures:
                    failures = extracted.get("failures", [])
                if not files_changed:
                    files_changed = extracted.get("files_changed", [])
                if not next_steps:
                    next_steps = extracted.get("next_steps", [])
                if not tags:
                    tags = extracted.get("tags", [])

            ext_sig = extracted.get("significance")
            if ext_sig and ext_sig in ("low", "medium", "high"):
                from models import Significance
                significance = Significance(ext_sig)

    elif _is_lite_save(request):
        # No transcript, lite save — extract from note only
        logger.info("Lite save (no transcript) — extracting from note")
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

    # Build session record
    record = SessionRecord(
        session_id=request.session_id,
        created_at=now.isoformat(),
        summary=summary,
        significance=significance,
        files_changed=files_changed,
        decisions=decisions,
        failures=failures,
        project_states=request.project_states or {},
        next_steps=next_steps,
        tags=tags,
        worker_processed=False,
        worker_processed_at=None,
    )

    # Write to cold storage (Tier 3)
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    filename = session_filename(request.session_id)
    filepath = SESSIONS_DIR / filename

    try:
        filepath.write_text(
            json.dumps(record.model_dump(), indent=2, default=str),
            encoding="utf-8",
        )
        logger.info(f"Session saved: {filepath}")
    except Exception as e:
        logger.error(f"Failed to save session: {e}")
        raise

    # Queue for worker processing
    processor = get_processor()
    processor.enqueue(request.session_id, str(filepath))

    # Build message
    parts = [f"Session saved ({significance.value} significance)."]
    if transcript_action == "created":
        parts.append(f"Transcript stored ({transcript_size_kb} KB).")
    elif transcript_action == "updated":
        parts.append(f"Transcript updated ({transcript_size_kb} KB).")
    elif transcript_action == "skipped":
        parts.append("Transcript unchanged (dedup).")
    parts.append(f"Worker processing: queued (queue depth: {len(processor.queue)}).")

    return SaveResponse(
        session_id=request.session_id,
        saved_at=now.isoformat(),
        session_file=str(filepath),
        worker_queued=True,
        message=" ".join(parts),
    )
