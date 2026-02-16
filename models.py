"""Pydantic models for ContextEngine API."""

from datetime import datetime
from typing import Optional, List, Dict, Any
from enum import Enum

from pydantic import BaseModel, Field


# ─── Enums ───────────────────────────────────────────────────

class Significance(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class CorrectionScope(str, Enum):
    HOT = "hot"
    ARCHIVE = "archive"
    BOTH = "both"


# ─── Request Models ─────────────────────────────────────────

class LoadRequest(BaseModel):
    """Input for context_load."""
    topic: Optional[str] = Field(
        None,
        description="Optional topic to focus context retrieval on. "
                    "If provided, searches archive for relevant hits.",
    )


class SaveRequest(BaseModel):
    """Input for context_save."""
    session_id: str = Field(
        ...,
        description="Session UUID returned by context_load.",
    )
    summary: str = Field(
        ...,
        description="Structured session summary. Include: what was done, "
                    "decisions made, files changed, next steps.",
    )
    significance: Significance = Field(
        Significance.MEDIUM,
        description="Session significance: low (quick chat), "
                    "medium (standard work), high (major changes).",
    )
    files_changed: Optional[List[str]] = Field(
        None, description="List of files created/modified this session.",
    )
    decisions: Optional[List[str]] = Field(
        None, description="Key decisions made and their rationale.",
    )
    failures: Optional[List[str]] = Field(
        None, description="Things that broke or didn't work.",
    )
    project_states: Optional[Dict[str, str]] = Field(
        None, description="Project name -> current state mapping.",
    )
    next_steps: Optional[List[str]] = Field(
        None, description="Prioritized next steps.",
    )
    tags: Optional[List[str]] = Field(
        None, description="Tags for categorization (e.g. 'infra', 'mcp', 'content').",
    )
    transcript_text: Optional[str] = Field(
        None,
        description="Optional raw conversation transcript. "
                    "If provided, stored permanently and used for Haiku summarization.",
    )


class SearchRequest(BaseModel):
    """Input for context_search."""
    query: str = Field(
        ...,
        description="Natural language search query.",
        min_length=1,
        max_length=500,
    )
    collections: Optional[List[str]] = Field(
        None, description="Specific collections to search. Default: all.",
    )
    limit: int = Field(
        5, description="Max results to return.", ge=1, le=50,
    )
    date_after: Optional[str] = Field(
        None, description="Only results after this ISO date.",
    )
    date_before: Optional[str] = Field(
        None, description="Only results before this ISO date.",
    )
    tags: Optional[List[str]] = Field(
        None, description="Filter by tags.",
    )


class CorrectRequest(BaseModel):
    """Input for context_correct."""
    item: str = Field(
        ..., description="What is incorrect (quote or describe).",
    )
    correction: str = Field(
        ..., description="What it should be.",
    )
    scope: CorrectionScope = Field(
        CorrectionScope.BOTH,
        description="Where to apply: hot (KB only), archive (ChromaDB only), both.",
    )


# ─── Response Models ────────────────────────────────────────

class LoadResponse(BaseModel):
    session_id: str
    hot_context: str
    archive_hits: List[Dict[str, Any]] = []
    failure_warnings: List[str] = []
    nudges: List[str] = []
    conflicts: List[str] = []
    degraded: bool = False
    degraded_reason: Optional[str] = None


class SaveResponse(BaseModel):
    session_id: str
    saved_at: str
    session_file: str
    worker_queued: bool
    message: str


class SearchResponse(BaseModel):
    query: str
    results: List[Dict[str, Any]]
    total_results: int
    collections_searched: List[str]


class CorrectResponse(BaseModel):
    item: str
    correction: str
    hot_updated: bool
    archive_updated: bool
    records_affected: int
    message: str


class HealthResponse(BaseModel):
    status: str
    version: str
    chromadb_connected: bool
    kb_accessible: bool
    sessions_count: int
    uptime_seconds: float
    learning_mode: bool
    degradation_level: str = "full"


# ─── Internal Models ───────────────────────────────────────

class SessionRecord(BaseModel):
    """Full session record written to cold storage."""
    session_id: str
    created_at: str
    summary: str
    significance: Significance
    files_changed: List[str] = []
    decisions: List[str] = []
    failures: List[str] = []
    project_states: Dict[str, str] = {}
    next_steps: List[str] = []
    tags: List[str] = []
    worker_processed: bool = False
    worker_processed_at: Optional[str] = None


class CheckpointRequest(BaseModel):
    """Input for context_checkpoint — lightweight mid-session save."""
    session_id: str = Field(
        ...,
        description="Session UUID returned by context_load.",
    )
    note: str = Field(
        ...,
        description="Brief note (1-3 sentences) about what happened. "
                    "Haiku extracts structured fields automatically.",
    )
    significance: Significance = Field(
        Significance.MEDIUM,
        description="Session significance: low, medium, high.",
    )
    transcript_path: Optional[str] = Field(
        None,
        description="Optional path to conversation transcript file on VPS. "
                    "If provided, Haiku uses transcript for richer extraction.",
    )
    transcript_text: Optional[str] = Field(
        None,
        description="Optional raw transcript text passed directly. "
                    "Alternative to transcript_path — no need to write file first.",
    )


class CheckpointResponse(BaseModel):
    session_id: str
    saved_at: str
    session_file: str
    transcript_stored: bool
    transcript_size_kb: Optional[float] = None
    worker_queued: bool
    message: str
