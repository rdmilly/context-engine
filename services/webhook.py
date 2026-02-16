"""Webhook service for n8n-based alerts.

Sends alerts to n8n webhook which routes to Telegram.
Fallback: direct Telegram API if webhook URL not configured.
"""

import httpx
from typing import Optional

from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
from utils.logging_ import logger

# Will be set from env or config
N8N_WEBHOOK_URL = None  # e.g. http://n8n:5678/webhook/context-engine-alerts


def _send_telegram_direct(message: str) -> bool:
    """Fallback: send directly to Telegram API."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram not configured, alert dropped")
        return False
    try:
        resp = httpx.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "Markdown",
            },
            timeout=10.0,
        )
        resp.raise_for_status()
        return True
    except Exception as e:
        logger.error(f"Telegram direct send failed: {e}")
        return False


def send_alert(title: str, body: str, level: str = "info") -> bool:
    """Send an alert through n8n webhook or fallback to Telegram.

    Args:
        title: Alert title
        body: Alert details
        level: info, warning, error, critical
    """
    emoji = {"info": "\u2139\ufe0f", "warning": "\u26a0\ufe0f", "error": "\u274c", "critical": "\U0001f525"}.get(level, "\u2139\ufe0f")
    message = f"{emoji} *ContextEngine: {title}*\n\n{body}"

    # Try n8n webhook first
    if N8N_WEBHOOK_URL:
        try:
            resp = httpx.post(
                N8N_WEBHOOK_URL,
                json={"title": title, "body": body, "level": level, "source": "context-engine"},
                timeout=10.0,
            )
            resp.raise_for_status()
            logger.info(f"Alert sent via n8n: {title}")
            return True
        except Exception as e:
            logger.warning(f"n8n webhook failed, falling back to Telegram: {e}")

    # Fallback to direct Telegram
    return _send_telegram_direct(message)


def send_worker_status(session_id: str, status: str, details: str = "") -> bool:
    """Send worker processing status update."""
    return send_alert(
        f"Worker {status}",
        f"Session: `{session_id}`\n{details}",
        level="info" if status == "completed" else "warning",
    )
