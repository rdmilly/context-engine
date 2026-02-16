"""Pydantic models for ContextEngine API."""

from datetime import datetime
from typing import Optional, List, Dict, Any
from enum import Enum
from pydantic import BaseModel, Field


class Significance(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class CorrectionScope(str, Enum):
    HOT = "hot"
    ARCHIVE = "archive"
    BOTH = "both"


class LoadRequest(BaseModel):
    topic: Optional[str] = Field(None, description="Optional topic to focus context retrieval on.")


class SaveRequest(BaseModel):
    session_id: str = Field(..., description="Session UUID returned by context_load.")
    summary: str = Field(..., description="Structured session summary.")
    significance: Significance = Field(Significance.MEDIUM)
    files_changed: Optional[List[str]] = None
    decisions: Optional[List[str]] = None
    failures: Optional[List[str]] = None
    project_states: Optional[Dict[str, str]] = None
    next_steps: Optional[List[str]] = None
    tags: Optional[List[str]] = None
    transcript_text: Optional[str] = None


class SearchRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=500)
    collections: Optional[List[str]] = None
    limit: int = Field(5, ge=1, le=50)
    date_after: Optional[str] = None
    date_before: Optional[str] = None
    tags: Optional[List[str]] = None


class CorrectRequest(BaseModel):
    item: str = Field(..., description="What is incorrect.")
    correction: str = Field(..., description="What it should be.")
    scope: CorrectionScope = Field(CorrectionScope.BOTH)


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


class SessionRecord(BaseModel):
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
    session_id: str = Field(..., description="Session UUID from context_load.")
    note: str = Field(..., description="Brief note (1-3 sentences).")
    significance: Significance = Field(Significance.MEDIUM)
    transcript_path: Optional[str] = None
    transcript_text: Optional[str] = None


class CheckpointResponse(BaseModel):
    session_id: str
    saved_at: str
    session_file: str
    transcript_stored: bool
    transcript_size_kb: Optional[float] = None
    worker_queued: bool
    message: str
