"""Worker processor — queue-based session processing with rate limiting.

Processes saved sessions through the LLM triage pipeline:
1. Load session JSON from cold storage
2. Summarize session (free/cheap model)
3. Triage content (Haiku with Sonnet escalation)
4. Write to ChromaDB collections
5. Rewrite master context
6. Take snapshot before writes

IMPORTANT: All synchronous LLM calls are wrapped in asyncio.to_thread()
to avoid blocking the FastAPI event loop. This was the root cause of
checkpoint timeouts when the worker was actively processing.
"""

import asyncio
import json
import os
import time
from datetime import datetime, timezone
from typing import Optional
from collections import deque

from config import (
    DATA_DIR, SESSIONS_DIR, LEARNING_MODE,
    CHROMADB_COLLECTIONS, WORKER_RATE_LIMIT_PER_MIN
)
from services.openrouter import get_client
from services.chromadb_client import get_chromadb
from services import chromadb_client as chromadb
from services.kb_gateway import read_master_context, write_master_context
from utils.logging_ import logger
from utils.nudges import store_nudges
from utils.degradation import get_manager as get_degradation_manager
import time as _time
from utils.anomalies import store_anomalies


class WorkerProcessor:
    """Processes session saves through LLM triage and ChromaDB archival."""

    def __init__(self):
        self.queue: deque = deque()
        self.processing = False
        self._last_backup_time = 0.0
        self._backup_interval = 86400  # 24 hours
        self.last_process_time = 0.0
        self.rate_limit = WORKER_RATE_LIMIT_PER_MIN
        self.stats = {
            "processed": 0,
            "failed": 0,
            "skipped": 0,
            "last_processed": None,
            "last_error": None,
        }
        self._task: Optional[asyncio.Task] = None

    def enqueue(self, session_id: str, session_file: str):
        """Add a session to the processing queue."""
        self.queue.append({"session_id": session_id, "file": session_file, "queued_at": time.time()})
        logger.info(f"Worker: queued {session_id} (queue depth: {len(self.queue)})")

    def start(self):
        """Start the background processing loop."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._process_loop())
            logger.info("Worker: background processor started")

    async def _process_loop(self):
        """Main processing loop — runs forever, checks queue."""
        while True:
            if self.queue:
                # Rate limiting
                elapsed = time.time() - self.last_process_time
                min_interval = 60.0 / self.rate_limit
                if elapsed < min_interval:
                    await asyncio.sleep(min_interval - elapsed)

                item = self.queue.popleft()
                await self._process_session(item)
            else:
                # Auto-backup check (every 24h)
                if _time.time() - self._last_backup_time > self._backup_interval:
                    await self._auto_backup()
                await asyncio.sleep(5)  # Poll every 5s


    async def _auto_backup(self):
        """Create automatic periodic backup."""
        try:
            import httpx
            async with httpx.AsyncClient() as client:
                resp = await client.post("http://localhost:9040/api/backup/create", timeout=60)
                if resp.status_code == 200:
                    result = resp.json()
                    logger.info(f"Auto-backup: created {result.get('backup_name', '?')} ({result.get('total_size_bytes', 0)} bytes)")
                else:
                    logger.warning(f"Auto-backup: failed with HTTP {resp.status_code}")
        except Exception as e:
            logger.warning(f"Auto-backup: {e}")
        self._last_backup_time = _time.time()

        # Check LLM credits (daily, after backup)
        try:
            from services.credit_tracker import check_and_alert
            check_and_alert()
        except Exception as e:
            logger.debug(f"Credit check: {e}")

        # Run retention (daily, after backup)
        try:
            from services.retention import run_retention
            from services import chromadb_client
            if chromadb_client.is_connected():
                client = chromadb_client.get_chromadb()
                # Load retention settings
                retention_overrides = None
                try:
                    from routers.settings import _load_settings
                    s = _load_settings()
                    retention_overrides = {
                        "sessions": s.retention.sessions_days,
                        "project_archive": s.retention.project_archive_days,
                        "decisions": s.retention.decisions_days,
                        "failures": s.retention.failures_days,
                        "entities": s.retention.entities_days,
                        "patterns": s.retention.patterns_days,
                        "snapshots": s.retention.snapshots_days,
                        "anomalies": s.retention.anomalies_days,
                    }
                except Exception:
                    pass
                results = run_retention(client, retention_overrides=retention_overrides)
                total_pruned = sum(r.get("pruned", 0) for r in results)
                if total_pruned > 0:
                    logger.info(f"Retention: pruned {total_pruned} expired documents")
        except Exception as e:
            logger.warning(f"Retention: {e}")

    async def _process_session(self, item: dict):
        """Process a single session through the full pipeline.
        
        All synchronous LLM/IO calls are wrapped in asyncio.to_thread()
        to prevent blocking the FastAPI event loop.
        """
        session_id = item["session_id"]
        session_file = item["file"]
        logger.info(f"Worker: processing {session_id}")
        self.processing = True

        dm = get_degradation_manager()

        # Check if OpenRouter is available
        if not dm.can_call("openrouter"):
            logger.warning(f"Worker: OpenRouter circuit breaker open, re-queuing {session_id}")
            self.queue.append(item)  # Re-queue
            await asyncio.sleep(30)  # Back off
            self.processing = False
            return

        try:
            # 1. Load session data
            session_data = self._load_session(session_file)
            if session_data is None:
                self.stats["failed"] += 1
                self.stats["last_error"] = f"Could not load {session_file}"
                return

            # Skip low significance in non-learning mode
            significance = session_data.get("significance", "medium")
            if not LEARNING_MODE and significance == "low":
                logger.info(f"Worker: skipping low significance session {session_id}")
                self.stats["skipped"] += 1
                return

            # 2. Read current master context (IO — run in thread)
            current_master = await asyncio.to_thread(read_master_context)
            if current_master is None:
                logger.warning("Worker: master context unavailable, processing with empty context")
                current_master = "# Master Context\n*Not available*"

            # 3. Snapshot before any writes
            await self._take_snapshot(session_id, current_master)

            # 4. Summarize session (LLM call — run in thread)
            llm = get_client()
            summary_result = await asyncio.to_thread(llm.summarize_session, session_data)
            if summary_result:
                logger.info(f"Worker: session summarized — {summary_result.get('compressed_summary', '')[:80]}")
            else:
                logger.warning("Worker: summarization failed, using raw session data")
                summary_result = {
                    "compressed_summary": session_data.get("summary", "No summary"),
                    "key_topics": session_data.get("tags", []),
                    "significance_confirmed": significance,
                }

            # 5. Triage session (LLM call — run in thread)
            triage_result = await asyncio.to_thread(llm.triage_session, session_data, current_master)
            if triage_result is None:
                logger.error(f"Worker: triage failed for {session_id}")
                self.stats["failed"] += 1
                self.stats["last_error"] = f"Triage failed for {session_id}"
                return

            logger.info(f"Worker: triage complete — {len(triage_result.get('items', []))} items, {len(triage_result.get('master_context_updates', []))} updates")

            # 6. Write to ChromaDB
            await self._write_to_chromadb(session_id, session_data, summary_result, triage_result)

            # 6.5. Extract entities (LLM call — run in thread)
            entity_result = await asyncio.to_thread(llm.extract_entities, session_data)
            if entity_result and entity_result.get("entities"):
                entities = entity_result["entities"]
                for ent in entities:
                    doc_id = f"entity-{ent['name'].lower().replace(' ', '-')}-{session_id}"
                    metadata = {
                        "name": ent["name"],
                        "type": ent["type"],
                        "session_id": session_id,
                        "timestamp": session_data.get("created_at", ""),
                        "relationships": ",".join(ent.get("relationships", [])),
                    }
                    chromadb.upsert_document("entities", doc_id, ent.get("context", ent["name"]), metadata)
                logger.info(f"Worker: extracted {len(entities)} entities")

            # 7. Update master context (LLM call — run in thread)
            new_master = await asyncio.to_thread(
                llm.compress_master_context, current_master, triage_result, session_data
            )
            if new_master and new_master.get("master_context_markdown"):
                # Integrity check before writing
                try:
                    from services.integrity_checker import check_integrity, load_kb_facts
                    import os
                    kb_root = os.environ.get("KB_ROOT", "/data/kb")
                    kb_facts = load_kb_facts(kb_root)
                    integrity = check_integrity(current_master, new_master["master_context_markdown"], kb_facts)
                    if not integrity["passed"]:
                        logger.warning(f"Worker: integrity check FAILED (severity={integrity['severity']}, dropped={integrity['drop_count']}): {integrity['details']}")
                        if integrity["severity"] == "high":
                            # High severity = block the write, keep existing master
                            logger.error("Worker: BLOCKING master context update due to high-severity integrity failure")
                            # Still take a snapshot of what would have been written
                            await self._take_snapshot(f"{session_id}-blocked", new_master["master_context_markdown"])
                            # Alert Ryan via Telegram
                            try:
                                from services.webhook import send_alert
                                details = integrity.get("details", "unknown")[:300]
                                dropped = integrity.get("drop_count", "?")
                                msg = f"Session: {session_id} | Dropped: {dropped} facts | {details}"
                                send_alert("Master Context Update BLOCKED", msg, level="error")
                            except Exception as alert_err:
                                logger.warning(f"Worker: failed to send block alert: {alert_err}")
                        else:
                            # Medium/low = write but log the warning
                            await asyncio.to_thread(write_master_context, new_master["master_context_markdown"])
                            changes = new_master.get("changes_made", [])
                            logger.info(f"Worker: master context updated with warnings — {len(changes)} changes, {integrity['drop_count']} facts flagged")
                    else:
                        await asyncio.to_thread(write_master_context, new_master["master_context_markdown"])
                        changes = new_master.get("changes_made", [])
                        logger.info(f"Worker: master context updated — {len(changes)} changes, integrity OK")
                except ImportError:
                    # Integrity checker not available, write anyway
                    await asyncio.to_thread(write_master_context, new_master["master_context_markdown"])
                    changes = new_master.get("changes_made", [])
                    logger.info(f"Worker: master context updated — {len(changes)} changes")
            else:
                logger.warning("Worker: master context compression failed, keeping existing")
                try:
                    from services.webhook import send_alert
                    send_alert("Master Compression Failed", f"Session: {session_id} | LLM returned no result.", level="warning")
                except Exception:
                    pass

            # 8. Mark session as processed
            self._mark_processed(session_file, session_id, summary_result, triage_result)

            # 8.2. Cockpit update (every session — lightweight project state tracker)
            try:
                from services.cockpit import read_cockpit, write_cockpit
                current_cockpit = read_cockpit()
                if current_cockpit:
                    cockpit_result = await asyncio.to_thread(
                        llm.update_cockpit, current_cockpit, session_data
                    )
                    if cockpit_result and cockpit_result.get("cockpit_markdown"):
                        cockpit_md = cockpit_result["cockpit_markdown"]
                        # Inject credit tracker data (deterministic, no LLM cost)
                        try:
                            from services.credit_tracker import format_for_cockpit, check_and_alert
                            credit_lines = format_for_cockpit()
                            if credit_lines:
                                if "LLM Credits" in cockpit_md:
                                    import re
                                    pat = re.compile(r"\| LLM Credits \|[^\n]*\n(\| LLM Spend \|[^\n]*\n)?")
                                    cockpit_md = pat.sub(credit_lines + "\n", cockpit_md)
                                elif "SYSTEM HEALTH" in cockpit_md:
                                    cockpit_md = cockpit_md.replace("## SYSTEM HEALTH", "## SYSTEM HEALTH\n\n" + credit_lines, 1)
                                else:
                                    cockpit_md += "\n\n## LLM CREDITS\n\n| Metric | Value | Status |\n|--------|-------|--------|\n" + credit_lines + "\n"
                            check_and_alert()
                        except Exception as e:
                            logger.debug(f"Worker: credit tracker inject skipped: {e}")
                        write_cockpit(cockpit_md)
                        updated = cockpit_result.get("projects_updated", [])
                        logger.info(f"Worker: cockpit updated — projects: {updated}")
                    else:
                        logger.warning("Worker: cockpit LLM update returned no result")
                else:
                    logger.info("Worker: no cockpit file found, skipping update")
            except Exception as e:
                logger.warning(f"Worker: cockpit update failed (non-fatal): {e}")

            # 8.5. Pattern detection (every 5th session, LLM call — run in thread)
            if self.stats["processed"] % 5 == 0:
                try:
                    recent = chromadb.get_recent_sessions(n=10)
                    if len(recent) >= 5:
                        pattern_result = await asyncio.to_thread(llm.detect_patterns, recent)
                        if pattern_result and pattern_result.get("patterns"):
                            for pat in pattern_result["patterns"]:
                                pat_id = f"pattern-{session_id}-{pat['type']}"
                                pat_meta = {
                                    "type": pat["type"],
                                    "frequency": str(pat.get("frequency", 0)),
                                    "session_id": session_id,
                                    "timestamp": session_data.get("created_at", ""),
                                }
                                chromadb.upsert_document("patterns", pat_id, pat["pattern"], pat_meta)
                            logger.info(f"Worker: detected {len(pattern_result['patterns'])} patterns")
                except Exception as e:
                    logger.warning(f"Worker: pattern detection failed (non-fatal): {e}")

            # 9. Nudge generation (every 3rd session)
            if self.stats["processed"] % 3 == 0 and not LEARNING_MODE:
                try:
                    recent = chromadb.get_recent_sessions(n=10)
                    patterns_list = []
                    failures_list = []
                    try:
                        patterns_list = [h.get("content", "") for h in chromadb.search_collection("patterns", "recent", n_results=5) if h.get("distance", 999) < 1.5]
                    except Exception:
                        pass
                    try:
                        failures_list = [h.get("content", "") for h in chromadb.search_collection("failures", "recent", n_results=5) if h.get("distance", 999) < 1.5]
                    except Exception:
                        pass

                    nudge_list = await asyncio.to_thread(
                        llm.generate_nudges, current_master, recent, patterns_list, failures_list
                    )
                    if nudge_list:
                        stored = store_nudges(nudge_list, session_id=session_id)
                        logger.info(f"Worker: generated {len(nudge_list)} nudges, stored {stored}")
                except Exception as e:
                    logger.warning(f"Worker: nudge generation failed (non-fatal): {e}")


            # 10. Anomaly detection (every 4th session)
            if self.stats["processed"] % 4 == 0 and not LEARNING_MODE:
                try:
                    recent_decisions = []
                    recent_failures_list = []
                    try:
                        recent_decisions = [h.get("content", "") for h in chromadb.search_collection("decisions", "recent", n_results=10) if h.get("distance", 999) < 1.5]
                    except Exception:
                        pass
                    try:
                        recent_failures_list = [h.get("content", "") for h in chromadb.search_collection("failures", "resolved", n_results=10) if h.get("distance", 999) < 1.5]
                    except Exception:
                        pass

                    anomaly_list = await asyncio.to_thread(
                        llm.detect_anomalies, session_data, current_master, recent_decisions, recent_failures_list
                    )
                    if anomaly_list:
                        stored = store_anomalies(anomaly_list, session_id=session_id)
                        logger.info(f"Worker: detected {len(anomaly_list)} anomalies, stored {stored}")
                    else:
                        logger.info("Worker: no anomalies detected")
                except Exception as e:
                    logger.warning(f"Worker: anomaly detection failed (non-fatal): {e}")

            self.stats["processed"] += 1
            self.stats["last_processed"] = session_id
            self.last_process_time = time.time()
            logger.info(f"Worker: {session_id} processed successfully")

        except Exception as e:
            import traceback
            logger.error(f"Worker: error processing {session_id}: {e}\n{traceback.format_exc()}")
            self.stats["failed"] += 1
            self.stats["last_error"] = str(e)
        finally:
            self.processing = False

    def _load_session(self, session_file: str) -> Optional[dict]:
        """Load session JSON from cold storage."""
        try:
            path = session_file
            if not os.path.isabs(path):
                path = os.path.join(SESSIONS_DIR, os.path.basename(path))
            with open(path, "r") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Worker: failed to load session file {session_file}: {e}")
            return None

    async def _take_snapshot(self, session_id: str, master_context: str):
        """Take a snapshot of current state before writes."""
        try:
            chromadb = get_chromadb()
            collection = chromadb.get_collection("snapshots")
            snapshot = {
                "master_context": master_context,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "trigger": session_id,
            }
            collection.add(
                documents=[json.dumps(snapshot)],
                metadatas=[{
                    "session_id": session_id,
                    "timestamp": snapshot["timestamp"],
                    "type": "pre_write_snapshot",
                }],
                ids=[f"snap-{session_id}-{int(time.time())}"]
            )
            logger.info(f"Worker: snapshot taken for {session_id}")
        except Exception as e:
            logger.warning(f"Worker: snapshot failed (non-fatal): {e}")

    async def _write_to_chromadb(self, session_id: str, session_data: dict, summary: dict, triage: dict):
        """Write session data to ChromaDB collections."""
        chromadb = get_chromadb()
        ts = datetime.now(timezone.utc).isoformat()

        # Write to sessions collection
        try:
            sessions_col = chromadb.get_collection("sessions")
            sessions_col.add(
                documents=[json.dumps({
                    "summary": summary.get("compressed_summary", ""),
                    "key_topics": summary.get("key_topics", []),
                    "significance": summary.get("significance_confirmed", "medium"),
                    "raw_summary": session_data.get("summary", ""),
                    "files_changed": session_data.get("files_changed", []),
                    "decisions": session_data.get("decisions", []),
                    "failures": session_data.get("failures", []),
                    "next_steps": session_data.get("next_steps", []),
                })],
                metadatas=[{
                    "session_id": session_id,
                    "timestamp": ts,
                    "significance": summary.get("significance_confirmed", "medium"),
                    "topics": ",".join(summary.get("key_topics", [])),
                    "source": session_data.get("source", "mcp"),
                }],
                ids=[f"session-{session_id}"]
            )
            logger.info(f"Worker: wrote to sessions collection")
        except Exception as e:
            logger.error(f"Worker: sessions write failed: {e}")

        # Write archived items to project_archive
        archived_items = [i for i in triage.get("items", []) if i.get("action") == "archive"]
        if archived_items:
            try:
                archive_col = chromadb.get_collection("project_archive")
                for idx, item in enumerate(archived_items):
                    archive_col.add(
                        documents=[item.get("content", "")],
                        metadatas=[{
                            "session_id": session_id,
                            "timestamp": ts,
                            "reason": item.get("reason", ""),
                            "source_collection": item.get("collection", "project_archive"),
                        }],
                        ids=[f"archive-{session_id}-{idx}"]
                    )
                logger.info(f"Worker: archived {len(archived_items)} items to project_archive")
            except Exception as e:
                logger.error(f"Worker: project_archive write failed: {e}")

        # Write decisions
        decisions = session_data.get("decisions", [])
        if decisions:
            try:
                decisions_col = chromadb.get_collection("decisions")
                for idx, decision in enumerate(decisions):
                    decisions_col.add(
                        documents=[decision if isinstance(decision, str) else json.dumps(decision)],
                        metadatas=[{
                            "session_id": session_id,
                            "timestamp": ts,
                            "tags": ",".join(session_data.get("tags", [])),
                        }],
                        ids=[f"decision-{session_id}-{idx}"]
                    )
                logger.info(f"Worker: wrote {len(decisions)} decisions")
            except Exception as e:
                logger.error(f"Worker: decisions write failed: {e}")

        # Write failures
        failures = session_data.get("failures", [])
        if failures:
            try:
                failures_col = chromadb.get_collection("failures")
                for idx, failure in enumerate(failures):
                    failures_col.add(
                        documents=[failure if isinstance(failure, str) else json.dumps(failure)],
                        metadatas=[{
                            "session_id": session_id,
                            "timestamp": ts,
                            "tags": ",".join(session_data.get("tags", [])),
                        }],
                        ids=[f"failure-{session_id}-{idx}"]
                    )
                logger.info(f"Worker: wrote {len(failures)} failures")
            except Exception as e:
                logger.error(f"Worker: failures write failed: {e}")

    def _mark_processed(self, session_file: str, session_id: str, summary: dict, triage: dict):
        """Mark session file as processed by adding metadata."""
        try:
            path = session_file
            if not os.path.isabs(path):
                path = os.path.join(SESSIONS_DIR, os.path.basename(path))
            with open(path, "r") as f:
                data = json.load(f)
            data["_processed"] = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "summary": summary.get("compressed_summary", ""),
                "triage_items": len(triage.get("items", [])),
                "master_updates": len(triage.get("master_context_updates", [])),
            }
            with open(path, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.warning(f"Worker: failed to mark {session_id} as processed: {e}")

    def stop(self):
        """Stop the background processing loop."""
        if self._task and not self._task.done():
            self._task.cancel()
            logger.info("Worker: background processor stopped")

    @property
    def status(self) -> dict:
        return {
            "queue_depth": len(self.queue),
            "processing": self.processing,
            **self.stats,
        }


# Singleton
_processor: Optional[WorkerProcessor] = None

def get_processor() -> WorkerProcessor:
    global _processor
    if _processor is None:
        _processor = WorkerProcessor()
    return _processor
