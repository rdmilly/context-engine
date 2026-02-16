"""ContextEngine — Recursive Context Management System.

FastAPI application providing persistent, self-improving memory
for Claude across conversations.

Phase 1: Basic load/save cycle with cold storage.
Phase 2: Compression worker with LLM triage.
Phase 3: Full search + corrections.
Phase 4: Layer 2 auto-injection.
Phase 5: Intelligence layer (patterns, nudges).
Phase 6: Bootstrap + hardening.
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pathlib import Path as _Path

from config import PORT, DEBUG, LEARNING_MODE, SESSIONS_DIR, LOGS_DIR, DATA_DIR, OPENROUTER_API_KEY
from services import chromadb_client, kb_gateway
from worker.processor import get_processor
from utils.logging_ import logger
from utils.degradation import get_manager as get_degradation_manager
from config import WATCH_DIRS, WATCH_GIT_ROOT, WATCH_TRANSCRIPT_DIR, WATCH_DEBOUNCE_SECONDS, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown logic."""
    # Startup
    logger.info("=" * 60)
    logger.info("ContextEngine v0.4.0 starting up...")
    logger.info(f"  Port: {PORT}")
    logger.info(f"  Debug: {DEBUG}")
    logger.info(f"  Learning mode: {LEARNING_MODE}")

    # Ensure data directories exist
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "backups").mkdir(parents=True, exist_ok=True)

    # Initialize settings (loads from settings.json or bootstraps from env vars)
    try:
        from routers.settings import _load_settings, _apply_llm_settings
        saved_settings = _load_settings()
        _apply_llm_settings(saved_settings.llm)
        logger.info(f"  Settings: loaded (LLM: {saved_settings.llm.base_url})")
    except Exception as e:
        logger.warning(f"  Settings: init failed ({e}), using env var defaults")

    # Initialize LLM failover chain
    try:
        from services.openrouter import get_client
        from services.failover import get_failover_chain
        chain = get_failover_chain(get_client())
        logger.info(f"  Failover: initialized ({len(chain.fallbacks)} fallbacks)")
    except Exception as e:
        logger.warning(f"  Failover: init failed ({e})")

    # Check KB Gateway
    dm = get_degradation_manager()
    if kb_gateway.kb_accessible():
        logger.info("  KB Gateway: accessible")
        # Initialize master context cache
        mc = kb_gateway.read_master_context()
        if mc:
            dm.update_cache(mc, source="startup")
            logger.info(f"  Context cache: initialized ({len(mc)} bytes)")
    else:
        logger.warning("  KB Gateway: NOT accessible — degraded mode")

    # Check ChromaDB and ensure collections
    if chromadb_client.is_connected():
        logger.info("  ChromaDB: connected")
        collections = chromadb_client.ensure_collections()
        logger.info(f"  Collections: {len(collections)} initialized")
    else:
        logger.warning("  ChromaDB: NOT connected — degraded mode")

    # Check OpenRouter
    if OPENROUTER_API_KEY and not OPENROUTER_API_KEY.startswith("placeholder"):
        logger.info("  OpenRouter: API key configured")
    else:
        logger.warning("  OpenRouter: NOT configured — worker will fail")

    # Count existing sessions
    session_count = len(list(SESSIONS_DIR.glob("*.json")))
    logger.info(f"  Existing sessions: {session_count}")

    # Start worker processor
    processor = get_processor()
    processor.start()
    logger.info("  Worker: started")

    # Start file watcher (if configured)
    file_watcher = None
    if WATCH_DIRS:
        from services.file_watcher import init_watcher
        file_watcher = init_watcher(
            watch_dirs=WATCH_DIRS,
            git_root=WATCH_GIT_ROOT,
            transcript_dir=WATCH_TRANSCRIPT_DIR or None,
            debounce_seconds=WATCH_DEBOUNCE_SECONDS,
            telegram_token=TELEGRAM_BOT_TOKEN or None,
            telegram_chat_id=TELEGRAM_CHAT_ID or None,
        )
        file_watcher.start()
        logger.info(f"  FileWatcher: monitoring {len(WATCH_DIRS)} dirs")
    else:
        logger.info("  FileWatcher: disabled (WATCH_DIRS not set)")

    logger.info("ContextEngine ready.")
    logger.info("=" * 60)

    yield

    # Shutdown
    logger.info("ContextEngine shutting down...")
    if file_watcher:
        file_watcher.stop()
    processor = get_processor()
    processor.stop()
    logger.info("ContextEngine shutdown complete.")


app = FastAPI(
    title="ContextEngine",
    description="Recursive context management system for Claude.",
    version="0.3.0",
    lifespan=lifespan,
)

# CORS (internal only, but useful for debugging)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount routers
from routers import load, save, search, correct, internal, checkpoint, bootstrap, backup, settings, metrics, ingest

app.include_router(load.router, tags=["MCP Tools"])
app.include_router(save.router, tags=["MCP Tools"])
app.include_router(search.router, tags=["MCP Tools"])
app.include_router(correct.router, tags=["MCP Tools"])
app.include_router(checkpoint.router, tags=["MCP Tools"])
app.include_router(internal.router, tags=["Internal"])
app.include_router(bootstrap.router, tags=["Bootstrap"])
app.include_router(backup.router, tags=["Backup"])
app.include_router(settings.router, tags=["Settings"])
app.include_router(metrics.router, tags=["Metrics"])
app.include_router(ingest.router, tags=["Ingest"])


# Dashboard
_static_dir = _Path(__file__).parent / "static"
if _static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")


@app.get("/dashboard")
async def dashboard():
    """Serve the web dashboard."""
    index = _static_dir / "index.html"
    if index.exists():
        return FileResponse(str(index))
    return {"error": "Dashboard not available"}


@app.get("/")
async def root():
    return {
        "service": "ContextEngine",
        "version": "0.3.0",
        "phase": 6,
        "docs": "/docs",
    }
