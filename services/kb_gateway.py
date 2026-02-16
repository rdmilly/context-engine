"""KB Gateway service for reading/writing master context.

Supports two modes:
- External KB mode: Reads/writes to mounted KB repo (e.g., Working KB)
- Standalone mode: Reads/writes to local data directory only

In both modes, writes also go to local path as a backup.
Phase 6 cache fallback is maintained as a third tier.
"""

import subprocess
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional

from config import KB_ROOT, MASTER_CONTEXT_PATH, LOCAL_MASTER_CONTEXT_PATH, STANDALONE_MODE
from utils.logging_ import logger
from utils.degradation import get_manager


def _safe_path(relative: str) -> Path:
    resolved = (KB_ROOT / relative).resolve()
    if not str(resolved).startswith(str(KB_ROOT.resolve())):
        raise ValueError(f"Path traversal blocked: {relative}")
    return resolved


def _git_commit(message: str) -> Optional[str]:
    try:
        subprocess.run(["git", "add", "-A"], cwd=KB_ROOT, capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", message, "--allow-empty"], cwd=KB_ROOT, capture_output=True, text=True, check=True)
        logger.info(f"KB git commit: {message}")
        return "committed"
    except subprocess.CalledProcessError as e:
        logger.warning(f"KB git commit failed: {e}")
        return None


def _external_kb_accessible() -> bool:
    try:
        return KB_ROOT.exists() and KB_ROOT.is_dir() and not STANDALONE_MODE
    except Exception:
        return False


def _read_external() -> Optional[str]:
    try:
        filepath = _safe_path(MASTER_CONTEXT_PATH)
        if filepath.exists():
            return filepath.read_text(encoding="utf-8")
    except Exception as e:
        logger.warning(f"External KB read failed: {e}")
    return None


def _read_local() -> Optional[str]:
    try:
        if LOCAL_MASTER_CONTEXT_PATH.exists():
            return LOCAL_MASTER_CONTEXT_PATH.read_text(encoding="utf-8")
    except Exception as e:
        logger.warning(f"Local master context read failed: {e}")
    return None


def read_master_context() -> Optional[str]:
    dm = get_manager()
    if _external_kb_accessible():
        content = _read_external()
        if content:
            dm.mark_healthy("kb_gateway")
            dm.update_cache(content, source="live")
            return content
    content = _read_local()
    if content:
        if STANDALONE_MODE:
            dm.mark_healthy("kb_gateway")
        else:
            dm.mark_unhealthy("kb_gateway", "external KB unavailable, using local")
        dm.update_cache(content, source="local")
        return content
    dm.mark_unhealthy("kb_gateway", "no file sources available")
    cached = dm.get_cached_context()
    if cached:
        logger.warning(f"Using cached master context ({dm.cache_age_seconds:.0f}s old)")
        return cached
    return None


def write_master_context(content: str, commit_message: str = "ContextEngine: update master context") -> bool:
    dm = get_manager()
    dm.update_cache(content, source="live")
    success = False
    try:
        LOCAL_MASTER_CONTEXT_PATH.parent.mkdir(parents=True, exist_ok=True)
        LOCAL_MASTER_CONTEXT_PATH.write_text(content, encoding="utf-8")
        success = True
    except Exception as e:
        logger.error(f"Failed to write local master context: {e}")
    if _external_kb_accessible():
        try:
            filepath = _safe_path(MASTER_CONTEXT_PATH)
            filepath.parent.mkdir(parents=True, exist_ok=True)
            filepath.write_text(content, encoding="utf-8")
            _git_commit(commit_message)
            dm.mark_healthy("kb_gateway")
            success = True
        except Exception as e:
            dm.mark_unhealthy("kb_gateway", str(e))
    if not success:
        dm.mark_unhealthy("kb_gateway", "all write targets failed")
    return success


def kb_accessible() -> bool:
    dm = get_manager()
    if _external_kb_accessible():
        dm.mark_healthy("kb_gateway")
        return True
    if LOCAL_MASTER_CONTEXT_PATH.exists():
        if STANDALONE_MODE:
            dm.mark_healthy("kb_gateway")
        return True
    return False
