"""Infrastructure file watcher.

Monitors configured directories for file changes, auto-commits to git,
and feeds change events directly into the checkpoint pipeline.

Runs as a background thread inside ContextEngine — no separate service needed.
Anyone deploying CE can enable this by setting WATCH_DIRS and mounting volumes.

Uses Python watchdog (inotify under the hood on Linux).
"""

import os
import subprocess
import threading
import time
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler, FileSystemEvent

from utils.logging_ import logger

# ─── Ignore patterns ────────────────────────────────────────────
IGNORE_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "data"}
IGNORE_EXTENSIONS = {".pyc", ".swp", ".swo", ".tmp", ".log", ".db", ".sqlite"}
IGNORE_PREFIXES = (".#", "#")


def _should_ignore(path: str) -> bool:
    """Check if a path should be ignored."""
    parts = Path(path).parts
    for part in parts:
        if part in IGNORE_DIRS:
            return True
    name = Path(path).name
    if name.startswith(IGNORE_PREFIXES):
        return True
    if Path(path).suffix in IGNORE_EXTENSIONS:
        return True
    return False


class InfraWatcher:
    """Watches infrastructure directories, auto-commits, feeds CE pipeline."""

    def __init__(
        self,
        watch_dirs: list[str],
        git_root: str,
        transcript_dir: Optional[str] = None,
        debounce_seconds: int = 10,
        telegram_token: Optional[str] = None,
        telegram_chat_id: Optional[str] = None,
    ):
        self.watch_dirs = [d for d in watch_dirs if os.path.isdir(d)]
        self.git_root = git_root
        self.transcript_dir = transcript_dir
        self.debounce_seconds = debounce_seconds
        self.telegram_token = telegram_token
        self.telegram_chat_id = telegram_chat_id

        self._observer: Optional[Observer] = None
        self._pending_changes: set[str] = set()
        self._lock = threading.Lock()
        self._debounce_timer: Optional[threading.Timer] = None
        self._running = False

        # Stats
        self.commits_count = 0
        self.files_tracked = 0
        self.last_commit_at: Optional[str] = None
        self.started_at: Optional[str] = None

    def start(self):
        """Start watching directories."""
        if not self.watch_dirs:
            logger.warning("FileWatcher: No valid watch directories configured")
            return

        self._running = True
        self.started_at = datetime.now(timezone.utc).isoformat()

        # Ensure git repo exists
        self._ensure_git_repo()

        self._observer = Observer()
        handler = _ChangeHandler(self)

        for watch_dir in self.watch_dirs:
            try:
                self._observer.schedule(handler, watch_dir, recursive=True)
                logger.info(f"FileWatcher: Watching {watch_dir}")
            except Exception as e:
                logger.warning(f"FileWatcher: Failed to watch {watch_dir}: {e}")

        # Watch transcript dir separately if configured
        if self.transcript_dir and os.path.isdir(self.transcript_dir):
            transcript_handler = _TranscriptHandler(self)
            self._observer.schedule(transcript_handler, self.transcript_dir, recursive=False)
            logger.info(f"FileWatcher: Watching transcripts at {self.transcript_dir}")

        self._observer.start()
        logger.info(f"FileWatcher: Started — monitoring {len(self.watch_dirs)} directories")

    def stop(self):
        """Stop watching."""
        self._running = False
        if self._debounce_timer:
            self._debounce_timer.cancel()
        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=5)
        logger.info("FileWatcher: Stopped")

    def _ensure_git_repo(self):
        """Initialize git repo if it doesn't exist."""
        git_dir = Path(self.git_root) / ".git"
        if not git_dir.exists():
            try:
                subprocess.run(
                    ["git", "init", "--initial-branch=main"],
                    cwd=self.git_root, capture_output=True, timeout=10,
                )
                subprocess.run(
                    ["git", "config", "user.email", "contextengine@millyweb.com"],
                    cwd=self.git_root, capture_output=True, timeout=5,
                )
                subprocess.run(
                    ["git", "config", "user.name", "ContextEngine FileWatcher"],
                    cwd=self.git_root, capture_output=True, timeout=5,
                )
                logger.info(f"FileWatcher: Initialized git repo at {self.git_root}")
            except Exception as e:
                logger.warning(f"FileWatcher: Git init failed: {e}")

    def on_file_changed(self, path: str):
        """Called when a file changes. Debounces then processes."""
        if _should_ignore(path):
            return

        with self._lock:
            self._pending_changes.add(path)

            # Reset debounce timer
            if self._debounce_timer:
                self._debounce_timer.cancel()

            self._debounce_timer = threading.Timer(
                self.debounce_seconds, self._process_changes
            )
            self._debounce_timer.daemon = True
            self._debounce_timer.start()

    def on_transcript_arrived(self, path: str):
        """Called when a new transcript file appears."""
        logger.info(f"FileWatcher: New transcript detected: {path}")
        try:
            from routers.checkpoint import context_checkpoint
            from models import CheckpointRequest, Significance
            import asyncio

            request = CheckpointRequest(
                session_id=f"transcript-{Path(path).stem}",
                note=f"Transcript arrived: {Path(path).name}",
                significance=Significance.MEDIUM,
                transcript_path=path,
                tags=["transcript", "auto-captured"],
            )

            # Run the async checkpoint in a new event loop
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(context_checkpoint(request))
                logger.info(f"FileWatcher: Transcript processed: {Path(path).name}")
            finally:
                loop.close()

        except Exception as e:
            logger.warning(f"FileWatcher: Failed to process transcript {path}: {e}")

    def _process_changes(self):
        """Debounced: commit changes, detect infra changes, feed CE pipeline."""
        with self._lock:
            if not self._pending_changes:
                return
            changed = list(self._pending_changes)
            self._pending_changes.clear()

        try:
            # Git add + commit
            result = self._git_commit(changed)
            if not result:
                return

            commit_hash, commit_msg, diff_stat, file_count = result
            self.commits_count += 1
            self.files_tracked += file_count
            self.last_commit_at = datetime.now(timezone.utc).isoformat()

            # ── Tier 1: Infrastructure detection (no LLM) ──────────
            try:
                from services.infra_detector import analyze_changes, write_to_kb
                from config import KB_ROOT

                analysis = analyze_changes(changed, self.git_root)

                # Write compose/directory changes directly to KB
                if analysis["kb_updates"]:
                    written = write_to_kb(KB_ROOT, analysis["kb_updates"])
                    if written:
                        logger.info(f"FileWatcher: Wrote {len(written)} KB updates")

                # Alert on credentials (never send to LLM)
                if analysis["credential_alerts"]:
                    cred_count = len(analysis["credential_alerts"])
                    cred_types = set(a["type"] for a in analysis["credential_alerts"])
                    alert_msg = (
                        f"\U0001f6a8 CREDENTIAL DETECTED: {cred_count} "
                        f"credential(s) ({', '.join(cred_types)}) in recent changes. "
                        f"Verify these are in Infisical, not committed in plaintext."
                    )
                    self._send_telegram(alert_msg)
                    logger.warning(f"FileWatcher: {alert_msg}")

                has_compose = bool(analysis["compose_changes"])
                has_new_dirs = bool(analysis["new_directories"])
                has_creds = bool(analysis["credential_alerts"])
            except Exception as e:
                logger.warning(f"FileWatcher: Infra detection failed (non-fatal): {e}")
                has_compose = any(
                    "docker-compose" in f or "compose.yml" in f for f in changed
                )
                has_new_dirs = False
                has_creds = False

            # ── Categorize ─────────────────────────────────────────
            affected_stacks = list(set(
                Path(f).parts[0] if len(Path(f).parts) > 1 else ""
                for f in changed if f.startswith("stacks/")
            ))
            affected_projects = list(set(
                Path(f).parts[1] if len(Path(f).parts) > 1 else ""
                for f in changed if f.startswith("projects/")
            ))

            # Significance: infra changes are always medium+
            if has_compose or has_new_dirs or has_creds:
                significance = "medium"
            else:
                significance = "low"

            summary = f"[{commit_hash}] {commit_msg}. {diff_stat}"
            tags = ["infra-watcher"]
            if has_compose:
                tags.append("compose-change")
            if has_creds:
                tags.append("credential-detected")
            if has_new_dirs:
                tags.append("new-service")
            tags.extend(s for s in affected_stacks if s)
            tags.extend(p for p in affected_projects if p)

            # Feed into checkpoint pipeline (low sig = archived only, medium+ = processed)
            self._create_checkpoint(summary, significance, tags, changed)

            # Telegram for significant changes
            if has_compose or has_new_dirs or file_count >= 5:
                self._send_telegram(f"\U0001f527 {summary}")

            logger.info(
                f"FileWatcher: Committed {commit_hash} "
                f"({file_count} files, significance={significance})"
            )

        except Exception as e:
            logger.warning(f"FileWatcher: Process changes failed: {e}")

    def _git_commit(self, changed_files: list[str]) -> Optional[tuple]:
        """Stage and commit changes. Returns (hash, msg, stat, count) or None."""
        try:
            # Stage all
            subprocess.run(
                ["git", "add", "-A"],
                cwd=self.git_root, capture_output=True, timeout=15,
            )

            # Check what's actually staged
            result = subprocess.run(
                ["git", "diff", "--cached", "--name-only"],
                cwd=self.git_root, capture_output=True, text=True, timeout=10,
            )
            staged = [f for f in result.stdout.strip().split("\n") if f]
            if not staged:
                return None

            file_count = len(staged)

            # Build commit message
            if file_count <= 3:
                msg = f"auto: {', '.join(staged)}"
            else:
                contexts = set()
                for f in staged:
                    parts = f.split("/")
                    if len(parts) > 1:
                        contexts.add(parts[0])
                msg = f"auto: {file_count} file(s) in {', '.join(sorted(contexts))}"

            # Commit
            subprocess.run(
                ["git", "commit", "-m", msg],
                cwd=self.git_root, capture_output=True, timeout=15,
            )

            # Get hash and stat
            hash_result = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=self.git_root, capture_output=True, text=True, timeout=5,
            )
            commit_hash = hash_result.stdout.strip()

            stat_result = subprocess.run(
                ["git", "diff", "HEAD~1", "--stat"],
                cwd=self.git_root, capture_output=True, text=True, timeout=10,
            )
            diff_stat = stat_result.stdout.strip().split("\n")[-1] if stat_result.stdout else ""

            return (commit_hash, msg, diff_stat, file_count)

        except Exception as e:
            logger.warning(f"FileWatcher: Git commit failed: {e}")
            return None

    def _create_checkpoint(
        self, summary: str, significance: str, tags: list[str], files: list[str]
    ):
        """Feed change event directly into CE's checkpoint pipeline."""
        try:
            from config import SESSIONS_DIR
            from models import SessionRecord, Significance
            from worker.processor import get_processor

            now = datetime.now(timezone.utc)
            session_id = f"infra-watch-{now.strftime('%Y%m%d-%H%M%S')}"

            record = SessionRecord(
                session_id=session_id,
                created_at=now.isoformat(),
                summary=summary,
                significance=Significance(significance),
                files_changed=files[:20],
                decisions=[],
                failures=[],
                project_states={},
                next_steps=[],
                tags=tags,
                worker_processed=False,
                worker_processed_at=None,
            )

            filepath = SESSIONS_DIR / f"{session_id}.json"
            filepath.write_text(
                json.dumps(record.model_dump(), indent=2, default=str),
                encoding="utf-8",
            )

            processor = get_processor()
            processor.enqueue(session_id, str(filepath))

        except Exception as e:
            logger.warning(f"FileWatcher: Checkpoint creation failed: {e}")

    def _send_telegram(self, text: str):
        """Send notification to Telegram."""
        if not self.telegram_token or not self.telegram_chat_id:
            return
        try:
            import urllib.request
            payload = json.dumps({
                "chat_id": self.telegram_chat_id,
                "text": text,
            }).encode()
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{self.telegram_token}/sendMessage",
                data=payload,
                headers={"Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=10)
        except Exception:
            pass  # Don't log telegram failures — not critical

    def get_stats(self) -> dict:
        """Return watcher stats for health/status endpoints."""
        return {
            "enabled": bool(self.watch_dirs),
            "running": self._running,
            "watch_dirs": self.watch_dirs,
            "git_root": self.git_root,
            "commits": self.commits_count,
            "files_tracked": self.files_tracked,
            "last_commit": self.last_commit_at,
            "started_at": self.started_at,
            "pending_changes": len(self._pending_changes),
        }


class _ChangeHandler(FileSystemEventHandler):
    """Watchdog handler that feeds events to InfraWatcher."""

    def __init__(self, watcher: InfraWatcher):
        self.watcher = watcher

    def on_any_event(self, event: FileSystemEvent):
        if event.is_directory:
            return
        if event.event_type in ("modified", "created", "deleted", "moved"):
            # Convert to relative path from git root
            try:
                rel_path = os.path.relpath(event.src_path, self.watcher.git_root)
            except ValueError:
                rel_path = event.src_path
            self.watcher.on_file_changed(rel_path)


class _TranscriptHandler(FileSystemEventHandler):
    """Watches for new transcript files arriving."""

    def __init__(self, watcher: InfraWatcher):
        self.watcher = watcher

    def on_created(self, event: FileSystemEvent):
        if event.is_directory:
            return
        if event.src_path.endswith((".json", ".txt", ".md")):
            # Small delay to let the file finish writing
            threading.Timer(2.0, self.watcher.on_transcript_arrived, args=[event.src_path]).start()


# ─── Singleton ──────────────────────────────────────────────────
_watcher: Optional[InfraWatcher] = None


def get_watcher() -> Optional[InfraWatcher]:
    return _watcher


def init_watcher(
    watch_dirs: list[str],
    git_root: str,
    transcript_dir: Optional[str] = None,
    debounce_seconds: int = 10,
    telegram_token: Optional[str] = None,
    telegram_chat_id: Optional[str] = None,
) -> InfraWatcher:
    global _watcher
    _watcher = InfraWatcher(
        watch_dirs=watch_dirs,
        git_root=git_root,
        transcript_dir=transcript_dir,
        debounce_seconds=debounce_seconds,
        telegram_token=telegram_token,
        telegram_chat_id=telegram_chat_id,
    )
    return _watcher
