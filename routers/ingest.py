"""
Webhook intake router.

Accepts context from external sources:
- n8n workflows
- GitHub Actions / CI/CD
- Telegram bots
- Custom scripts
- Any HTTP client

POST /api/ingest — submit context payload
POST /api/ingest/raw — submit raw text (auto-wrapped into session)

Authentication: CE API key in X-API-Key header or ?api_key= query param.
"""

import time
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional, List
from pathlib import Path

from fastapi import APIRouter, HTTPException, Header, Query, Request
from pydantic import BaseModel, Field

logger = logging.getLogger("context-engine")
router = APIRouter()


# ── Auth ──────────────────────────────────────────────────────
def _check_auth(api_key: Optional[str] = None, header_key: Optional[str] = None) -> bool:
    """Validate API key from header or query param."""
    import os
    expected = os.environ.get("CONTEXT_ENGINE_API_KEY", "")
    if not expected:
        return True  # No key configured = open access (standalone mode)

    provided = header_key or api_key
    if not provided:
        return False
    return provided == expected


# ── Models ────────────────────────────────────────────────────
class IngestPayload(BaseModel):
    """Structured context ingestion."""
    summary: str = Field(..., description="What happened — the core context to save")
    source: str = Field(default="webhook", description="Where this came from (e.g. 'n8n', 'github-actions', 'telegram', 'script')")
    source_id: Optional[str] = Field(default=None, description="ID from the source system (e.g. workflow run ID, commit SHA)")
    tags: List[str] = Field(default_factory=list, description="Tags for categorization")
    significance: str = Field(default="medium", description="low, medium, or high")
    decisions: List[str] = Field(default_factory=list, description="Key decisions made")
    failures: List[str] = Field(default_factory=list, description="What broke or didn't work")
    files_changed: List[str] = Field(default_factory=list, description="Files created or modified")
    next_steps: List[str] = Field(default_factory=list, description="What to do next")
    metadata: Optional[dict] = Field(default=None, description="Any additional structured data")


class RawIngestPayload(BaseModel):
    """Raw text ingestion — auto-wrapped into a session."""
    text: str = Field(..., description="Raw text to ingest")
    source: str = Field(default="webhook", description="Source identifier")
    tags: List[str] = Field(default_factory=list)
    significance: str = Field(default="low")


# ── Endpoints ─────────────────────────────────────────────────
@router.post("/api/ingest")
async def ingest_context(
    payload: IngestPayload,
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
    api_key: Optional[str] = Query(None),
):
    """
    Ingest structured context from an external source.

    Creates a session and queues it for worker processing.
    """
    if not _check_auth(api_key, x_api_key):
        raise HTTPException(status_code=401, detail="Invalid API key")

    from config import SESSIONS_DIR

    session_id = f"{payload.source}-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"

    session = {
        "session_id": session_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source": payload.source,
        "source_id": payload.source_id,
        "summary": payload.summary,
        "decisions": payload.decisions,
        "failures": payload.failures,
        "files_changed": payload.files_changed,
        "next_steps": payload.next_steps,
        "tags": payload.tags,
        "significance": payload.significance,
        "status": "pending",
        "metadata": payload.metadata or {},
        "ingested_via": "webhook",
    }

    # Write session file
    session_file = SESSIONS_DIR / f"{session_id}.json"
    session_file.write_text(json.dumps(session, indent=2, default=str))

    logger.info(f"Ingest: {session_id} from {payload.source} (significance={payload.significance}, tags={payload.tags})")

    return {
        "status": "accepted",
        "session_id": session_id,
        "message": f"Context from {payload.source} queued for processing",
    }


@router.post("/api/ingest/raw")
async def ingest_raw(
    payload: RawIngestPayload,
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
    api_key: Optional[str] = Query(None),
):
    """
    Ingest raw text — auto-wrapped into a session for Haiku processing.
    Good for piping in logs, transcripts, or notes.
    """
    if not _check_auth(api_key, x_api_key):
        raise HTTPException(status_code=401, detail="Invalid API key")

    from config import SESSIONS_DIR

    session_id = f"{payload.source}-raw-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"

    session = {
        "session_id": session_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source": payload.source,
        "summary": payload.text,
        "tags": payload.tags,
        "significance": payload.significance,
        "status": "pending",
        "ingested_via": "webhook-raw",
    }

    session_file = SESSIONS_DIR / f"{session_id}.json"
    session_file.write_text(json.dumps(session, indent=2, default=str))

    logger.info(f"Ingest (raw): {session_id} from {payload.source} ({len(payload.text)} chars)")

    return {
        "status": "accepted",
        "session_id": session_id,
        "message": f"Raw text from {payload.source} queued for processing",
        "text_length": len(payload.text),
    }


@router.get("/api/ingest/sources")
async def list_sources():
    """List all known ingestion sources and their session counts."""
    from config import SESSIONS_DIR

    sources = {}
    for f in SESSIONS_DIR.glob("*.json"):
        try:
            data = json.loads(f.read_text())
            src = data.get("source", "unknown")
            via = data.get("ingested_via", "mcp")
            key = f"{src} ({via})"
            if key not in sources:
                sources[key] = {"count": 0, "latest": None}
            sources[key]["count"] += 1
            ts = data.get("created_at", "")
            if ts > (sources[key]["latest"] or ""):
                sources[key]["latest"] = ts
        except Exception:
            continue

    return {"sources": sources}
