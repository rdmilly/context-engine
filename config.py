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

# ─── LLM Provider (unified config) ─────────────────────────────
# New vars take priority; falls back to legacy OPENROUTER_* vars
OPENROUTER_BASE_URL = os.environ.get("LLM_BASE_URL", os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"))
OPENROUTER_API_KEY = os.environ.get("LLM_API_KEY", os.environ.get("OPENROUTER_API_KEY", ""))

# Model routing — fast for extraction/summaries, smart for triage/compression
_MODEL_FAST = os.environ.get("LLM_MODEL_FAST", "anthropic/claude-haiku-4.5")
_MODEL_SMART = os.environ.get("LLM_MODEL_SMART", "anthropic/claude-haiku-4.5")

TASK_MODELS = {
    "session_summary": _MODEL_FAST,
    "entity_extraction": _MODEL_FAST,
    "nudge_generation": _MODEL_FAST,
    "failure_extraction": _MODEL_FAST,
    "triage": _MODEL_FAST,
    "anomaly_detection": _MODEL_FAST,
    "decision_extraction": _MODEL_SMART,
    "master_compression": _MODEL_SMART,
    "pattern_analysis": _MODEL_SMART,
    "cockpit_update": _MODEL_FAST,
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
    "cockpit_update": os.environ.get("OLLAMA_MODEL_LIGHT", "llama3.2:3b"),
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
# ─── Context Budget (dynamic) ──────────────────────────────────
# Base budget for master context. Grows with active projects/sources.
MASTER_CONTEXT_BASE_CHARS = 20000    # ~5000 tokens base
MASTER_CONTEXT_MAX_CHARS = 32000     # ~8000 tokens ceiling
MASTER_CONTEXT_PER_PROJECT = 2000    # +500 tokens per active project
MASTER_CONTEXT_PER_SOURCE = 1500     # +375 tokens per active source

MAX_LOAD_RESPONSE_CHARS = 40000      # ~10000 tokens, master + archive hits
LEARNING_MODE_THRESHOLD = 20      # Sessions before learning mode disables

# ─── Transcripts ──────────────────────────────────────────────
TRANSCRIPTS_DIR = Path(os.environ.get("TRANSCRIPTS_DIR", "/app/data/transcripts"))
MAX_TRANSCRIPT_CHARS = 120000  # ~30K tokens, truncate beyond this for Haiku

# ─── File Watcher ─────────────────────────────────────────────
# Comma-separated list of directories to watch for infrastructure changes.
# Empty = disabled. Mount host dirs into container to use.
WATCH_DIRS = [d.strip() for d in os.environ.get("WATCH_DIRS", "").split(",") if d.strip()]
WATCH_GIT_ROOT = os.environ.get("WATCH_GIT_ROOT", "/watch")
WATCH_TRANSCRIPT_DIR = os.environ.get("WATCH_TRANSCRIPT_DIR", "")
WATCH_DEBOUNCE_SECONDS = int(os.environ.get("WATCH_DEBOUNCE_SECONDS", "10"))
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

def get_dynamic_budget() -> int:
    """Calculate current master context budget based on active projects and sources."""
    budget = MASTER_CONTEXT_BASE_CHARS

    # Count active projects from master context
    try:
        from services import kb_gateway
        mc = kb_gateway.read_master_context()
        if mc:
            import re
            # Count ### headings in Active Projects section
            projects_section = re.search(r'## Active Projects(.*?)## ', mc, re.DOTALL)
            if projects_section:
                project_count = len(re.findall(r'### ', projects_section.group(1)))
                budget += project_count * MASTER_CONTEXT_PER_PROJECT
    except Exception:
        pass

    # Count active sources from recent sessions
    try:
        from pathlib import Path
        import json
        sessions_dir = Path(SESSIONS_DIR)
        sources = set()
        for f in sorted(sessions_dir.glob("*.json"), reverse=True)[:50]:
            try:
                data = json.loads(f.read_text())
                sources.add(data.get("source", "mcp"))
            except Exception:
                continue
        budget += len(sources) * MASTER_CONTEXT_PER_SOURCE
    except Exception:
        pass

    return min(budget, MASTER_CONTEXT_MAX_CHARS)
