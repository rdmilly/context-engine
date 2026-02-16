"""Cockpit service — reads/writes the daily project cockpit.

The cockpit is a single markdown file in the Working KB that tracks
all active project states. Updated automatically by the worker after
every session save/checkpoint.

Path: /watch/data/working-kb/cockpit/daily-status.md
  (host: /opt/data/working-kb/cockpit/daily-status.md)
"""

import subprocess
from pathlib import Path
from typing import Optional

from utils.logging_ import logger

# Working KB is accessible via the /watch mount (host /opt)
WORKDOCS_ROOT = Path("/watch/data/working-kb")
COCKPIT_PATH = WORKDOCS_ROOT / "cockpit" / "daily-status.md"


def read_cockpit() -> Optional[str]:
    """Read the current cockpit markdown."""
    try:
        if COCKPIT_PATH.exists():
            content = COCKPIT_PATH.read_text(encoding="utf-8")
            logger.info(f"Cockpit: read {len(content)} bytes")
            return content
        else:
            logger.warning(f"Cockpit: file not found at {COCKPIT_PATH}")
            return None
    except Exception as e:
        logger.error(f"Cockpit: read failed: {e}")
        return None


def write_cockpit(content: str) -> bool:
    """Write updated cockpit and git commit."""
    try:
        COCKPIT_PATH.parent.mkdir(parents=True, exist_ok=True)
        COCKPIT_PATH.write_text(content, encoding="utf-8")
        logger.info(f"Cockpit: wrote {len(content)} bytes")
        _git_commit()
        return True
    except Exception as e:
        logger.error(f"Cockpit: write failed: {e}")
        return False


def _git_commit():
    """Auto-commit cockpit changes in the Working KB repo."""
    try:
        subprocess.run(
            ["git", "add", "cockpit/daily-status.md"],
            cwd=WORKDOCS_ROOT, capture_output=True, check=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "ContextEngine: cockpit auto-update", "--allow-empty"],
            cwd=WORKDOCS_ROOT, capture_output=True, text=True, check=True,
        )
        logger.info("Cockpit: git committed")
    except subprocess.CalledProcessError as e:
        # No changes to commit is fine
        if "nothing to commit" in str(e.stderr):
            logger.info("Cockpit: no changes to commit")
        else:
            logger.warning(f"Cockpit: git commit failed: {e}")
