"""Settings API — Runtime configuration for ContextEngine.

Persists settings to a JSON file in the data directory.
Changes take effect immediately without container restart.

Covers:
- LLM provider configuration (any OpenAI-compatible API)
- File watcher directories
- Notification settings (Telegram)
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from config import DATA_DIR
from utils.logging_ import logger

router = APIRouter()

SETTINGS_FILE = DATA_DIR / "settings.json"

# ─── Models ──────────────────────────────────────────────────────

class LLMSettings(BaseModel):
    base_url: str = Field(default="https://openrouter.ai/api/v1", description="OpenAI-compatible API base URL")
    api_key: str = Field(default="", description="API key (leave empty for local models)")
    model_fast: str = Field(default="anthropic/claude-haiku-4.5", description="Fast/cheap model for extraction, summaries, nudges")
    model_smart: str = Field(default="anthropic/claude-sonnet-4.5", description="Smart/expensive model for triage, compression, patterns")
    timeout_seconds: int = Field(default=60, description="Request timeout")

class WatcherSettings(BaseModel):
    enabled: bool = Field(default=False, description="Enable file watcher")
    watch_dirs: list[str] = Field(default_factory=list, description="Directories to watch")
    git_root: str = Field(default="/watch", description="Git root for auto-commits")
    transcript_dir: str = Field(default="", description="Directory for transcript drop zone")
    debounce_seconds: int = Field(default=10, description="Seconds to wait before committing")

class NotificationSettings(BaseModel):
    telegram_enabled: bool = Field(default=False)
    telegram_bot_token: str = Field(default="")
    telegram_chat_id: str = Field(default="")

class RetentionSettings(BaseModel):
    sessions_days: int = 180       # 6 months
    project_archive_days: int = 365
    decisions_days: int = 365
    failures_days: int = 365
    entities_days: int = 0         # 0 = never prune
    patterns_days: int = 365
    snapshots_days: int = 30
    anomalies_days: int = 180


class AllSettings(BaseModel):
    llm: LLMSettings = Field(default_factory=LLMSettings)
    watcher: WatcherSettings = Field(default_factory=WatcherSettings)
    notifications: NotificationSettings = Field(default_factory=NotificationSettings)
    retention: RetentionSettings = Field(default_factory=RetentionSettings)
    updated_at: Optional[str] = None


# ─── Persistence ─────────────────────────────────────────────────

def _load_settings() -> AllSettings:
    """Load settings from file, falling back to env vars for initial config."""
    if SETTINGS_FILE.exists():
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            return AllSettings(**data)
        except Exception as e:
            logger.warning(f"Settings: Failed to load {SETTINGS_FILE}: {e}")

    # Bootstrap from env vars (first run)
    settings = AllSettings(
        llm=LLMSettings(
            base_url=os.environ.get("LLM_BASE_URL", os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")),
            api_key=os.environ.get("LLM_API_KEY", "") or os.environ.get("OPENROUTER_API_KEY", ""),
            model_fast=os.environ.get("LLM_MODEL_FAST", "anthropic/claude-haiku-4.5"),
            model_smart=os.environ.get("LLM_MODEL_SMART", "anthropic/claude-sonnet-4.5"),
        ),
        watcher=WatcherSettings(
            enabled=bool(os.environ.get("WATCH_DIRS", "")),
            watch_dirs=[d.strip() for d in os.environ.get("WATCH_DIRS", "").split(",") if d.strip()],
            git_root=os.environ.get("WATCH_GIT_ROOT", "/watch"),
            transcript_dir=os.environ.get("WATCH_TRANSCRIPT_DIR", ""),
            debounce_seconds=int(os.environ.get("WATCH_DEBOUNCE_SECONDS", "10")),
        ),
        notifications=NotificationSettings(
            telegram_enabled=bool(os.environ.get("TELEGRAM_BOT_TOKEN", "")),
            telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
            telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID", ""),
        ),
    )
    _save_settings(settings)
    return settings


def _save_settings(settings: AllSettings):
    """Persist settings to JSON file."""
    settings.updated_at = datetime.now(timezone.utc).isoformat()
    try:
        SETTINGS_FILE.write_text(
            json.dumps(settings.model_dump(), indent=2),
            encoding="utf-8",
        )
        logger.info(f"Settings: saved to {SETTINGS_FILE}")
    except Exception as e:
        logger.error(f"Settings: Failed to save: {e}")


def _apply_llm_settings(llm: LLMSettings):
    """Hot-reload LLM client with new settings."""
    try:
        from services.openrouter import get_client
        client = get_client()

        client.base_url = llm.base_url
        client.api_key = llm.api_key
        client.client.timeout = llm.timeout_seconds

        # Update task model routing
        import config
        for task in config.TASK_MODELS:
            if task in ("decision_extraction", "master_compression", "pattern_analysis"):
                config.TASK_MODELS[task] = llm.model_smart
            else:
                config.TASK_MODELS[task] = llm.model_fast

        # Update escalation map
        from services.openrouter import ESCALATION_MAP
        ESCALATION_MAP.clear()
        ESCALATION_MAP[llm.model_fast] = llm.model_smart

        logger.info(f"Settings: LLM reconfigured — {llm.base_url} (fast={llm.model_fast}, smart={llm.model_smart})")
    except Exception as e:
        logger.error(f"Settings: Failed to apply LLM settings: {e}")


def _apply_watcher_settings(watcher: WatcherSettings, notifications: NotificationSettings):
    """Hot-reload file watcher with new settings."""
    try:
        from services.file_watcher import get_watcher, init_watcher

        # Stop existing watcher
        existing = get_watcher()
        if existing:
            existing.stop()

        if not watcher.enabled or not watcher.watch_dirs:
            logger.info("Settings: FileWatcher disabled")
            return

        # Start new watcher with updated config
        new_watcher = init_watcher(
            watch_dirs=watcher.watch_dirs,
            git_root=watcher.git_root,
            transcript_dir=watcher.transcript_dir or None,
            debounce_seconds=watcher.debounce_seconds,
            telegram_token=notifications.telegram_bot_token or None,
            telegram_chat_id=notifications.telegram_chat_id or None,
        )
        new_watcher.start()
        logger.info(f"Settings: FileWatcher restarted with {len(watcher.watch_dirs)} dirs")
    except Exception as e:
        logger.error(f"Settings: Failed to apply watcher settings: {e}")


# ─── API Endpoints ───────────────────────────────────────────────

@router.get("/api/settings")
async def get_settings():
    """Get all current settings."""
    settings = _load_settings()
    # Mask API key in response
    masked = settings.model_dump()
    if masked["llm"]["api_key"]:
        key = masked["llm"]["api_key"]
        masked["llm"]["api_key_masked"] = f"{key[:8]}...{key[-4:]}" if len(key) > 12 else "***"
        masked["llm"]["api_key_set"] = True
    else:
        masked["llm"]["api_key_masked"] = ""
        masked["llm"]["api_key_set"] = False
    del masked["llm"]["api_key"]

    # Same for telegram
    if masked["notifications"]["telegram_bot_token"]:
        masked["notifications"]["telegram_bot_token_set"] = True
    else:
        masked["notifications"]["telegram_bot_token_set"] = False
    del masked["notifications"]["telegram_bot_token"]

    return masked


@router.post("/api/settings")
async def update_settings(request: dict):
    """Update settings. Partial updates supported."""
    current = _load_settings()

    # Merge LLM settings
    if "llm" in request:
        llm_update = request["llm"]
        current_llm = current.llm.model_dump()
        # Don't overwrite API key with empty string (masked)
        if "api_key" in llm_update and not llm_update["api_key"]:
            del llm_update["api_key"]
        current_llm.update(llm_update)
        current.llm = LLMSettings(**current_llm)
        _apply_llm_settings(current.llm)

    # Merge watcher settings
    if "watcher" in request:
        watcher_update = request["watcher"]
        current_watcher = current.watcher.model_dump()
        current_watcher.update(watcher_update)
        current.watcher = WatcherSettings(**current_watcher)
        _apply_watcher_settings(current.watcher, current.notifications)

    # Merge notification settings
    if "notifications" in request:
        notif_update = request["notifications"]
        current_notif = current.notifications.model_dump()
        if "telegram_bot_token" in notif_update and not notif_update["telegram_bot_token"]:
            del notif_update["telegram_bot_token"]
        current_notif.update(notif_update)
        current.notifications = NotificationSettings(**current_notif)

    _save_settings(current)
    return {"status": "saved", "updated_at": current.updated_at}


@router.post("/api/settings/test-llm")
async def test_llm_connection():
    """Test LLM connection with a simple call."""
    try:
        from services.openrouter import get_client
        client = get_client()
        settings = _load_settings()

        response = client._call(
            settings.llm.model_fast,
            [{"role": "user", "content": "Reply with exactly: OK"}],
        )
        choices = response.get("choices", [])
        if choices:
            content = choices[0].get("message", {}).get("content", "")
            usage = response.get("usage", {})
            return {
                "status": "connected",
                "model": settings.llm.model_fast,
                "response": content[:100],
                "tokens": usage,
            }
        return {"status": "error", "message": "No response from model"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@router.post("/api/settings/test-telegram")
async def test_telegram():
    """Send a test Telegram notification."""
    settings = _load_settings()
    if not settings.notifications.telegram_bot_token:
        return {"status": "error", "message": "Telegram bot token not configured"}

    try:
        import urllib.request
        payload = json.dumps({
            "chat_id": settings.notifications.telegram_chat_id,
            "text": "✅ ContextEngine test notification — connection working!",
        }).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{settings.notifications.telegram_bot_token}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        resp = urllib.request.urlopen(req, timeout=10)
        return {"status": "sent", "message": "Test notification sent"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@router.get("/api/settings/presets")
async def get_llm_presets():
    """Return common LLM provider presets for easy setup."""
    return {
        "presets": [
            {
                "name": "OpenRouter",
                "description": "Access 200+ models through one API. Pay per token.",
                "base_url": "https://openrouter.ai/api/v1",
                "needs_key": True,
                "suggested_fast": "anthropic/claude-haiku-4.5",
                "suggested_smart": "anthropic/claude-sonnet-4.5",
                "signup_url": "https://openrouter.ai/keys",
            },
            {
                "name": "OpenAI",
                "description": "Direct OpenAI API access.",
                "base_url": "https://api.openai.com/v1",
                "needs_key": True,
                "suggested_fast": "gpt-4o-mini",
                "suggested_smart": "gpt-4o",
                "signup_url": "https://platform.openai.com/api-keys",
            },
            {
                "name": "Anthropic (via OpenRouter)",
                "description": "Claude models via OpenRouter.",
                "base_url": "https://openrouter.ai/api/v1",
                "needs_key": True,
                "suggested_fast": "anthropic/claude-haiku-4.5",
                "suggested_smart": "anthropic/claude-sonnet-4.5",
                "signup_url": "https://openrouter.ai/keys",
            },
            {
                "name": "Ollama (Local)",
                "description": "Run models locally. Free, no API key needed.",
                "base_url": "http://host.docker.internal:11434/v1",
                "needs_key": False,
                "suggested_fast": "llama3.2:3b",
                "suggested_smart": "llama3.1:8b",
                "signup_url": "https://ollama.com/download",
            },
            {
                "name": "Together AI",
                "description": "Fast inference for open models.",
                "base_url": "https://api.together.xyz/v1",
                "needs_key": True,
                "suggested_fast": "meta-llama/Llama-3.2-3B-Instruct-Turbo",
                "suggested_smart": "meta-llama/Meta-Llama-3.1-70B-Instruct-Turbo",
                "signup_url": "https://api.together.xyz/settings/api-keys",
            },
            {
                "name": "Groq",
                "description": "Ultra-fast inference. Generous free tier.",
                "base_url": "https://api.groq.com/openai/v1",
                "needs_key": True,
                "suggested_fast": "llama-3.1-8b-instant",
                "suggested_smart": "llama-3.3-70b-versatile",
                "signup_url": "https://console.groq.com/keys",
            },
            {
                "name": "LM Studio (Local)",
                "description": "Local GUI for running models. Free.",
                "base_url": "http://host.docker.internal:1234/v1",
                "needs_key": False,
                "suggested_fast": "loaded-model",
                "suggested_smart": "loaded-model",
                "signup_url": "https://lmstudio.ai/",
            },
        ]
    }


# ── Retention endpoints ───────────────────────────────────────
@router.get("/api/retention")
async def get_retention_status():
    """Show current retention settings and what would be pruned (dry run)."""
    from services.retention import run_retention
    from services import chromadb_client

    settings = _load_settings()

    if not chromadb_client.is_connected():
        return {"error": "ChromaDB not connected"}

    client = chromadb_client.get_chromadb()
    overrides = {
        "sessions": settings.retention.sessions_days,
        "project_archive": settings.retention.project_archive_days,
        "decisions": settings.retention.decisions_days,
        "failures": settings.retention.failures_days,
        "entities": settings.retention.entities_days,
        "patterns": settings.retention.patterns_days,
        "snapshots": settings.retention.snapshots_days,
        "anomalies": settings.retention.anomalies_days,
    }

    results = run_retention(client, retention_overrides=overrides, dry_run=True)

    return {
        "settings": overrides,
        "dry_run": results,
        "total_would_prune": sum(r.get("pruned", 0) for r in results),
    }


@router.post("/api/retention/run")
async def run_retention_now():
    """Run retention immediately (actually deletes expired docs)."""
    from services.retention import run_retention
    from services import chromadb_client

    settings = _load_settings()

    if not chromadb_client.is_connected():
        raise HTTPException(status_code=503, detail="ChromaDB not connected")

    client = chromadb_client.get_chromadb()
    overrides = {
        "sessions": settings.retention.sessions_days,
        "project_archive": settings.retention.project_archive_days,
        "decisions": settings.retention.decisions_days,
        "failures": settings.retention.failures_days,
        "entities": settings.retention.entities_days,
        "patterns": settings.retention.patterns_days,
        "snapshots": settings.retention.snapshots_days,
        "anomalies": settings.retention.anomalies_days,
    }

    results = run_retention(client, retention_overrides=overrides, dry_run=False)

    return {
        "settings": overrides,
        "results": results,
        "total_pruned": sum(r.get("pruned", 0) for r in results),
    }
