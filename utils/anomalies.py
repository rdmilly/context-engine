"""Anomaly storage and retrieval.

Stores detected anomalies from session processing. Anomalies represent
conflicts between new session data and established context:
- contradiction: Session claims conflict with master context or recent decisions
- regression: A previously resolved failure is recurring
- drift: Project scope/direction changing without explicit decision
- inconsistency: Entity or fact mentioned differently across sessions
- escalation: Issue severity increasing across sessions

Anomalies expire after their configured TTL and are deduplicated.
"""

import json
import time
from pathlib import Path
from datetime import datetime, timezone, timedelta
from config import DATA_DIR
from utils.logging_ import logger

ANOMALIES_FILE = DATA_DIR / "anomalies.json"
DEFAULT_EXPIRY_DAYS = 14
MAX_ACTIVE_ANOMALIES = 30


def _load_anomalies() -> list:
    if ANOMALIES_FILE.exists():
        try:
            return json.loads(ANOMALIES_FILE.read_text())
        except Exception:
            return []
    return []


def _save_anomalies(anomalies: list):
    ANOMALIES_FILE.write_text(json.dumps(anomalies, indent=2))


def _is_duplicate(existing: list, new_msg: str) -> bool:
    """Check if an anomaly description is too similar to existing ones."""
    new_lower = new_msg.lower().strip()
    for a in existing:
        existing_lower = a.get("description", "").lower().strip()
        if new_lower == existing_lower:
            return True
        new_words = set(new_lower.split())
        existing_words = set(existing_lower.split())
        if new_words and existing_words:
            overlap = len(new_words & existing_words) / max(len(new_words), len(existing_words))
            if overlap > 0.8:
                return True
    return False


def store_anomalies(anomalies: list, session_id: str = None) -> int:
    """Store new anomalies, deduplicating and enforcing limits."""
    existing = _load_anomalies()

    # Prune expired anomalies first
    now = datetime.now(timezone.utc)
    active = []
    for a in existing:
        expires_at = a.get("expires_at")
        if expires_at and datetime.fromisoformat(expires_at) < now:
            continue
        if a.get("dismissed"):
            continue
        active.append(a)

    stored = 0
    for anomaly in anomalies:
        desc = anomaly.get("description", "")
        if not desc:
            continue
        if _is_duplicate(active, desc):
            continue

        expiry_days = anomaly.get("expires_after_days", DEFAULT_EXPIRY_DAYS)
        expires_at = (now + timedelta(days=expiry_days)).isoformat()

        active.append({
            "description": desc,
            "type": anomaly.get("type", "inconsistency"),
            "severity": anomaly.get("severity", "medium"),
            "evidence": anomaly.get("evidence", ""),
            "session_id": session_id,
            "created_at": now.isoformat(),
            "expires_at": expires_at,
            "dismissed": False,
        })
        stored += 1

    # Enforce max limit — keep highest severity first
    severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    active.sort(key=lambda a: severity_order.get(a.get("severity", "medium"), 2))
    active = active[:MAX_ACTIVE_ANOMALIES]

    _save_anomalies(active)
    logger.info(f"Anomalies: stored {stored} new, {len(active)} total active")
    return stored


def get_active_anomalies() -> list:
    """Return non-expired, non-dismissed anomalies."""
    anomalies = _load_anomalies()
    now = datetime.now(timezone.utc)
    active = []
    for a in anomalies:
        expires_at = a.get("expires_at")
        if expires_at and datetime.fromisoformat(expires_at) < now:
            continue
        if a.get("dismissed"):
            continue
        active.append(a)
    return active


def dismiss_anomaly(description_substring: str) -> bool:
    """Dismiss an anomaly by partial description match."""
    anomalies = _load_anomalies()
    found = False
    for a in anomalies:
        if description_substring.lower() in a.get("description", "").lower():
            a["dismissed"] = True
            found = True
    if found:
        _save_anomalies(anomalies)
    return found


def get_anomaly_stats() -> dict:
    """Return anomaly statistics."""
    anomalies = _load_anomalies()
    active = get_active_anomalies()
    by_type = {}
    by_severity = {}
    for a in active:
        t = a.get("type", "unknown")
        s = a.get("severity", "medium")
        by_type[t] = by_type.get(t, 0) + 1
        by_severity[s] = by_severity.get(s, 0) + 1
    return {
        "total_stored": len(anomalies),
        "active": len(active),
        "by_type": by_type,
        "by_severity": by_severity,
    }
