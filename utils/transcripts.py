"""Transcript manager — storage, dedup, and retrieval for conversation transcripts.

Handles:
- Intelligent dedup: same session_id with longer transcript = overwrite, shorter = skip
- Dual storage: gzip archive (space-efficient) + plaintext (searchable)
- Listing and retrieval for audit/search
"""

import gzip
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from config import TRANSCRIPTS_DIR
from utils.logging_ import logger


# Max chars before we truncate for Haiku (keeping full copy in storage)
MAX_HAIKU_CHARS = 120_000  # ~30K tokens


def _get_existing_transcript(session_id: str) -> Optional[Path]:
    """Find existing transcript for this session_id."""
    TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
    # Look for any file matching session_id pattern
    matches = sorted(TRANSCRIPTS_DIR.glob(f"{session_id}_*.txt.gz"))
    return matches[-1] if matches else None


def _read_existing_size(path: Path) -> int:
    """Read the uncompressed size of a gzipped transcript."""
    try:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            return len(f.read())
    except Exception:
        return 0


def store_transcript(session_id: str, transcript: str) -> dict:
    """Store a transcript with intelligent deduplication.

    Returns dict with:
        stored: bool — whether a new file was written
        path: str — path to the stored file
        size_kb: float — compressed size in KB
        action: str — 'created', 'updated', 'skipped'
        chars: int — transcript length
    """
    TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)

    existing = _get_existing_transcript(session_id)
    new_len = len(transcript)

    if existing:
        old_len = _read_existing_size(existing)
        if new_len <= old_len:
            # Same or shorter — already have this content
            size_kb = round(existing.stat().st_size / 1024, 1)
            logger.info(
                f"Transcript dedup: skipping {session_id} "
                f"(existing={old_len} chars, new={new_len} chars)"
            )
            return {
                "stored": False,
                "path": str(existing),
                "size_kb": size_kb,
                "action": "skipped",
                "chars": old_len,
            }
        else:
            # Longer transcript — overwrite (conversation continued)
            logger.info(
                f"Transcript dedup: updating {session_id} "
                f"(existing={old_len} chars -> new={new_len} chars)"
            )
            # Remove old file, write new one
            existing.unlink()

    # Write new gzipped transcript
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = f"{session_id}_{ts}.txt.gz"
    filepath = TRANSCRIPTS_DIR / filename

    compressed = gzip.compress(transcript.encode("utf-8"), compresslevel=6)
    filepath.write_bytes(compressed)

    size_kb = round(len(compressed) / 1024, 1)
    action = "updated" if existing else "created"

    logger.info(
        f"Transcript {action}: {filepath} "
        f"({new_len} chars -> {size_kb} KB compressed)"
    )

    return {
        "stored": True,
        "path": str(filepath),
        "size_kb": size_kb,
        "action": action,
        "chars": new_len,
    }


def get_transcript(session_id: str) -> Optional[str]:
    """Retrieve a stored transcript by session_id."""
    existing = _get_existing_transcript(session_id)
    if not existing:
        return None
    try:
        with gzip.open(existing, "rt", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        logger.warning(f"Failed to read transcript {existing}: {e}")
        return None


def truncate_for_haiku(transcript: str) -> str:
    """Truncate transcript for Haiku if it exceeds token budget.

    Keeps beginning and end (most important context),
    truncates middle with marker.
    """
    if len(transcript) <= MAX_HAIKU_CHARS:
        return transcript

    half = MAX_HAIKU_CHARS // 2
    return (
        transcript[:half]
        + "\n\n[...TRUNCATED FOR SUMMARIZATION...]\n\n"
        + transcript[-half:]
    )


def list_transcripts(limit: int = 50) -> list[dict]:
    """List stored transcripts, most recent first."""
    TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
    files = sorted(TRANSCRIPTS_DIR.glob("*.txt.gz"), reverse=True)

    results = []
    for f in files[:limit]:
        name = f.name
        # Parse session_id from filename: {session_id}_{timestamp}.txt.gz
        parts = name.rsplit("_", 2)
        session_id = parts[0] if len(parts) >= 3 else name.replace(".txt.gz", "")

        results.append({
            "session_id": session_id,
            "filename": name,
            "size_kb": round(f.stat().st_size / 1024, 1),
            "modified": datetime.fromtimestamp(
                f.stat().st_mtime, tz=timezone.utc
            ).isoformat(),
        })

    return results
