"""Session ID generation and management."""

import uuid
from datetime import datetime, timezone


def generate_session_id() -> str:
    """Generate a unique session ID.
    Format: ce-{date}-{short_uuid}
    Example: ce-20260212-a1b2c3d4
    """
    date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    short_id = uuid.uuid4().hex[:8]
    return f"ce-{date_str}-{short_id}"


def session_filename(session_id: str) -> str:
    """Get the filename for a session's cold storage JSON."""
    return f"{session_id}.json"
