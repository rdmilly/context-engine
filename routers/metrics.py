"""
Prometheus metrics endpoint.

Exposes /metrics in Prometheus text format. Zero dependencies beyond stdlib.

Metrics:
- contextengine_sessions_total (counter)
- contextengine_sessions_processed (counter)
- contextengine_sessions_unprocessed (gauge)
- contextengine_worker_queue_depth (gauge)
- contextengine_worker_processed (counter)
- contextengine_worker_failed (counter)
- contextengine_chromadb_documents{collection} (gauge)
- contextengine_llm_calls_total (counter)
- contextengine_llm_cost_dollars (counter)
- contextengine_backup_age_seconds (gauge)
- contextengine_backup_size_bytes (gauge)
- contextengine_degradation_level (gauge: 0=full, 1=partial, 2=minimal, 3=offline)
- contextengine_watcher_commits_total (counter)
- contextengine_uptime_seconds (gauge)
"""

import time
import logging
from pathlib import Path
from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

logger = logging.getLogger("context-engine")
router = APIRouter()

_start_time = time.time()


def _prom_line(name: str, value, help_text: str = "", type_: str = "gauge", labels: dict = None) -> str:
    """Format a single Prometheus metric line."""
    lines = []
    if help_text:
        lines.append(f"# HELP {name} {help_text}")
    if type_:
        lines.append(f"# TYPE {name} {type_}")

    label_str = ""
    if labels:
        pairs = [f'{k}="{v}"' for k, v in labels.items()]
        label_str = "{" + ",".join(pairs) + "}"

    lines.append(f"{name}{label_str} {value}")
    return "\n".join(lines)


@router.get("/metrics", response_class=PlainTextResponse)
async def prometheus_metrics():
    """Prometheus-compatible metrics endpoint."""
    from config import SESSIONS_DIR, LEARNING_MODE, DATA_DIR
    from services import chromadb_client, kb_gateway
    from worker.processor import get_processor

    lines = []

    # ── Uptime ─────────────────────────────────────────
    lines.append(_prom_line(
        "contextengine_uptime_seconds",
        round(time.time() - _start_time, 1),
        "Seconds since ContextEngine started",
    ))

    # ── Sessions ───────────────────────────────────────
    try:
        all_sessions = list(SESSIONS_DIR.glob("*.json"))
        total = len(all_sessions)
        import json
        processed = sum(1 for s in all_sessions if json.loads(s.read_text()).get("status") == "processed")
        unprocessed = total - processed

        lines.append(_prom_line("contextengine_sessions_total", total, "Total sessions saved", "counter"))
        lines.append(_prom_line("contextengine_sessions_processed", processed, "Sessions processed by worker", "counter"))
        lines.append(_prom_line("contextengine_sessions_unprocessed", unprocessed, "Sessions awaiting processing", "gauge"))
    except Exception:
        pass

    # ── Worker ─────────────────────────────────────────
    try:
        processor = get_processor()
        status = processor.status
        lines.append(_prom_line("contextengine_worker_queue_depth", status.get("queue_depth", 0), "Worker queue depth", "gauge"))
        lines.append(_prom_line("contextengine_worker_processed_total", status.get("processed", 0), "Total sessions processed", "counter"))
        lines.append(_prom_line("contextengine_worker_failed_total", status.get("failed", 0), "Total sessions failed", "counter"))
        lines.append(_prom_line("contextengine_worker_skipped_total", status.get("skipped", 0), "Total sessions skipped", "counter"))
    except Exception:
        pass

    # ── ChromaDB collections ───────────────────────────
    try:
        if chromadb_client.is_connected():
            client = chromadb_client.get_chromadb()
            for col in client.list_collections():
                count = col.count()
                lines.append(_prom_line(
                    "contextengine_chromadb_documents",
                    count,
                    "Documents in ChromaDB collection",
                    "gauge",
                    {"collection": col.name},
                ))
    except Exception:
        pass

    # ── LLM stats ──────────────────────────────────────
    try:
        from services.openrouter import get_client
        llm = get_client()
        lines.append(_prom_line("contextengine_llm_calls_total", llm.stats.get("calls", 0), "Total LLM API calls", "counter"))
        lines.append(_prom_line("contextengine_llm_cost_dollars", llm.stats.get("estimated_cost", 0.0), "Estimated LLM cost in dollars", "counter"))
    except Exception:
        pass

    # ── Backup stats ───────────────────────────────────
    try:
        backup_dir = DATA_DIR / "backups"
        if backup_dir.exists():
            backups = sorted(backup_dir.iterdir(), reverse=True)
            if backups:
                latest = backups[0]
                age = time.time() - latest.stat().st_mtime
                size = sum(f.stat().st_size for f in latest.iterdir() if f.is_file())
                lines.append(_prom_line("contextengine_backup_age_seconds", round(age, 0), "Seconds since last backup", "gauge"))
                lines.append(_prom_line("contextengine_backup_size_bytes", size, "Size of latest backup in bytes", "gauge"))
                lines.append(_prom_line("contextengine_backups_total", len(backups), "Total backup count", "gauge"))
    except Exception:
        pass

    # ── Degradation ────────────────────────────────────
    try:
        from services.degradation import get_degradation_manager
        dm = get_degradation_manager()
        level_map = {"full": 0, "partial": 1, "minimal": 2, "offline": 3}
        lines.append(_prom_line(
            "contextengine_degradation_level",
            level_map.get(dm.level.value, 3),
            "Degradation level (0=full, 1=partial, 2=minimal, 3=offline)",
        ))
    except Exception:
        pass

    # ── File watcher ───────────────────────────────────
    try:
        from services.file_watcher import _watcher_instance
        if _watcher_instance:
            stats = _watcher_instance.stats
            lines.append(_prom_line("contextengine_watcher_commits_total", stats.get("commits", 0), "Git commits from file watcher", "counter"))
            lines.append(_prom_line("contextengine_watcher_changes_total", stats.get("changes_detected", 0), "File changes detected", "counter"))
    except Exception:
        pass

    # ── Learning mode ──────────────────────────────────
    lines.append(_prom_line("contextengine_learning_mode", 1 if LEARNING_MODE else 0, "Learning mode enabled (1=yes)", "gauge"))

    # ── KB accessible ──────────────────────────────────
    try:
        lines.append(_prom_line("contextengine_kb_accessible", 1 if kb_gateway.kb_accessible() else 0, "KB Gateway reachable (1=yes)", "gauge"))
    except Exception:
        pass

    return "\n\n".join(lines) + "\n"
