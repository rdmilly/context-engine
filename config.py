"""ContextEngine configuration.

All config comes from environment variables.
Secrets injected via env_file from Infisical-sourced .env.
"""

import os
from pathlib import Path


# ─── Server ─────────────────────────────────────────────────────
PORT = int(os.environ.get("PORT", 9040))
DEBUG = os.environ.get("DEBUG", "false").lower() == "true"
LEARNING_MODE = os.environ.get("LEARNING_MODE", "true").lower() == "true"

# ─── ChromaDB ─────────────────────────────────────────────────
CHROMADB_HOST = os.environ.get("CHROMADB_HOST", "context-engine-chromadb")
CHROMADB_PORT = int(os.environ.get("CHROMADB_PORT", 8000))

# ─── Data Paths (early, needed by KB config) ─────────────────
DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))

# ─── KB Gateway (direct file access) ──────────────────────────
KB_ROOT = Path(os.environ.get("KB_ROOT", "/data/kb"))
MASTER_CONTEXT_PATH = "projects/context-engine/master-context.md"
# Local fallback when external KB is not mounted (standalone/product mode)
LOCAL_MASTER_CONTEXT_PATH = DATA_DIR / "master-context.md"
STANDALONE_MODE = os.environ.get("STANDALONE_MODE", "false").lower() == "true"

# ─── Data Paths ───────────────────────────────────────────────
SESSIONS_DIR = Path(os.environ.get("SESSIONS_DIR", "/app/data/sessions"))
LOGS_DIR = Path(os.environ.get("LOGS_DIR", "/app/data/logs"))
PROMPTS_DIR = Path(os.environ.get("PROMPTS_DIR", "/app/data/prompts"))

# ─── OpenRouter ────────────────────────────────────────────────
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

# ─── Model Routing ─────────────────────────────────────────────
# Free models unreliable (spend limits), use Haiku as baseline
TASK_MODELS = {
    "session_summary": "anthropic/claude-haiku-4.5",
    "entity_extraction": "anthropic/claude-haiku-4.5",
    "nudge_generation": "anthropic/claude-haiku-4.5",
    "failure_extraction": "anthropic/claude-haiku-4.5",
    "triage": "anthropic/claude-haiku-4.5",
    "decision_extraction": "anthropic/claude-sonnet-4.5",
    "master_compression": "anthropic/claude-sonnet-4.5",
    "pattern_analysis": "anthropic/claude-sonnet-4.5",
    "anomaly_detection": "anthropic/claude-haiku-4.5",
}

# ─── LLM Backend Selection ─────────────────────────────────
# "openrouter" (default) or "ollama" for local zero-cloud operation
LLM_BACKEND = os.environ.get("LLM_BACKEND", "openrouter")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://host.docker.internal:11434")

# Ollama model mappings (used when LLM_BACKEND=ollama)
OLLAMA_TASK_MODELS = {
    "session_summary": os.environ.get("OLLAMA_MODEL_LIGHT", "llama3.2:3b"),
    "entity_extraction": os.environ.get("OLLAMA_MODEL_LIGHT", "llama3.2:3b"),
    "nudge_generation": os.environ.get("OLLAMA_MODEL_LIGHT", "llama3.2:3b"),
    "failure_extraction": os.environ.get("OLLAMA_MODEL_LIGHT", "llama3.2:3b"),
    "triage": os.environ.get("OLLAMA_MODEL_LIGHT", "llama3.2:3b"),
    "anomaly_detection": os.environ.get("OLLAMA_MODEL_LIGHT", "llama3.2:3b"),
    "decision_extraction": os.environ.get("OLLAMA_MODEL_HEAVY", "llama3.1:8b"),
    "master_compression": os.environ.get("OLLAMA_MODEL_HEAVY", "llama3.1:8b"),
    "pattern_analysis": os.environ.get("OLLAMA_MODEL_HEAVY", "llama3.1:8b"),
}

# ─── MinIO (backup storage) ──────────────────────────────────
MINIO_ENDPOINT = os.environ.get("MINIO_ENDPOINT", "minio:9000")
MINIO_ACCESS_KEY = os.environ.get("MINIO_ACCESS_KEY", "")
MINIO_SECRET_KEY = os.environ.get("MINIO_SECRET_KEY", "")
MINIO_BUCKET = os.environ.get("MINIO_BUCKET", "contextengine-backups")
MINIO_SECURE = os.environ.get("MINIO_SECURE", "false").lower() == "true"

# ─── Worker ───────────────────────────────────────────────────
WORKER_RATE_LIMIT_SECONDS = 60
WORKER_RATE_LIMIT_PER_MIN = 1
IDLE_CHECK_INTERVAL = 30

# ─── Alerts ───────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# ─── ChromaDB Collections ──────────────────────────────────────
CHROMADB_COLLECTIONS = COLLECTIONS = {
    "project_archive": {"ttl": None, "description": "Completed/paused project context"},
    "decisions": {"ttl": None, "description": "Decision rationale with outcomes"},
    "failures": {"ttl": None, "description": "What broke, why, what worked instead"},
    "entities": {"ttl": None, "description": "People, services, relationships"},
    "sessions": {"ttl": "6mo", "description": "Compressed session summaries"},
    "patterns": {"ttl": "90d", "description": "Cross-session behavioral patterns"},
    "snapshots": {"ttl": "30d", "description": "Pre-write copies for rollback"},
    "anomalies": {"ttl": "60d", "description": "Detected context conflicts and regressions"},
}

# Mapping from LLM-hallucinated names to actual collection names
COLLECTION_ALIASES = {
    "session_history": "sessions",
    "session_summaries": "sessions",
    "projects": "project_archive",
    "project_history": "project_archive",
    "decision_log": "decisions",
    "failure_log": "failures",
    "error_log": "failures",
    "people": "entities",
    "services": "entities",
    "anomaly_log": "anomalies",
    "conflicts": "anomalies",
}


def resolve_collection_name(name: str) -> str:
    """Resolve a collection name, handling LLM hallucinated names."""
    if name in COLLECTIONS:
        return name
    resolved = COLLECTION_ALIASES.get(name, name)
    if resolved in COLLECTIONS:
        return resolved
    # Default to project_archive for unknown
    return "project_archive"

# ─── Token Budget ──────────────────────────────────────────────
MAX_MASTER_CONTEXT_CHARS = 8000   # ~2000 tokens, triggers compression if exceeded
MAX_LOAD_RESPONSE_CHARS = 12000   # ~3000 tokens, truncates archive hits if exceeded
LEARNING_MODE_THRESHOLD = 20      # Sessions before learning mode disables

# ─── Transcripts ──────────────────────────────────────────────
TRANSCRIPTS_DIR = Path(os.environ.get("TRANSCRIPTS_DIR", "/app/data/transcripts"))
MAX_TRANSCRIPT_CHARS = 120000  # ~30K tokens, truncate beyond this for Haiku
