"""Nudge storage and retrieval.

Stores generated nudges as JSON. Nudges expire after their configured TTL.
Nudges are deduplicated by message similarity to avoid repetition.
"""

import json
import time
from pathlib import Path
from datetime import datetime, timezone, timedelta
from config import DATA_DIR
from utils.logging_ import logger

NUDGES_FILE = DATA_DIR / "nudges.json"
DEFAULT_EXPIRY_DAYS = 7
MAX_ACTIVE_NUDGES = 20


def _load_nudges() -> list:
    if NUDGES_FILE.exists():
        try:
            return json.loads(NUDGES_FILE.read_text())
        except Exception:
            return []
    return []


def _save_nudges(nudges: list):
    NUDGES_FILE.write_text(json.dumps(nudges, indent=2))


def _is_duplicate(existing: list, new_msg: str) -> bool:
    """Check if a nudge message is too similar to existing ones."""
    new_lower = new_msg.lower().strip()
    for n in existing:
        existing_lower = n.get("message", "").lower().strip()
        # Exact match
        if new_lower == existing_lower:
            return True
        # High overlap (>80% shared words)
        new_words = set(new_lower.split())
        existing_words = set(existing_lower.split())
        if new_words and existing_words:
            overlap = len(new_words & existing_words) / max(len(new_words), len(existing_words))
            if overlap > 0.8:
                return True
    return False


def store_nudges(nudges: list, session_id: str = None):
    """Store new nudges, deduplicating and enforcing limits."""
    existing = _load_nudges()
    
    # Prune expired nudges first
    now = datetime.now(timezone.utc)
    active = []
    for n in existing:
        expires_at = n.get("expires_at")
        if expires_at and datetime.fromisoformat(expires_at) < now:
            continue
        active.append(n)

    added = 0
    for nudge in nudges:
        if _is_duplicate(active, nudge.get("message", "")):
            continue
        
        expiry_days = nudge.get("expires_after_days", DEFAULT_EXPIRY_DAYS)
        nudge_entry = {
            "message": nudge["message"],
            "type": nudge.get("type", "reminder"),
            "priority": nudge.get("priority", "medium"),
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(days=expiry_days)).isoformat(),
            "session_id": session_id or nudge.get("related_session"),
            "dismissed": False,
        }
        active.append(nudge_entry)
        added += 1

    # Sort by priority (high first) and trim to max
    priority_order = {"high": 0, "medium": 1, "low": 2}
    active.sort(key=lambda n: priority_order.get(n.get("priority"), 1))
    active = active[:MAX_ACTIVE_NUDGES]

    _save_nudges(active)
    logger.info(f"Nudges: stored {added} new, {len(active)} total active")
    return added


def get_active_nudges(limit: int = 5, topic: str = None) -> list:
    """Get active, non-dismissed, non-expired nudges."""
    all_nudges = _load_nudges()
    now = datetime.now(timezone.utc)
    
    active = []
    for n in all_nudges:
        if n.get("dismissed"):
            continue
        expires_at = n.get("expires_at")
        if expires_at and datetime.fromisoformat(expires_at) < now:
            continue
        active.append(n["message"])

    return active[:limit]


def dismiss_nudge(message_substring: str) -> bool:
    """Dismiss a nudge by partial message match."""
    nudges = _load_nudges()
    found = False
    for n in nudges:
        if message_substring.lower() in n.get("message", "").lower():
            n["dismissed"] = True
            found = True
    if found:
        _save_nudges(nudges)
    return found


def get_nudge_stats() -> dict:
    all_nudges = _load_nudges()
    now = datetime.now(timezone.utc)
    active = [n for n in all_nudges if not n.get("dismissed") 
              and (not n.get("expires_at") or datetime.fromisoformat(n["expires_at"]) > now)]
    return {
        "total": len(all_nudges),
        "active": len(active),
        "dismissed": len([n for n in all_nudges if n.get("dismissed")]),
        "by_type": {},
    }
