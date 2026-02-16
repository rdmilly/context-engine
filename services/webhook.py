"""Webhook service for n8n-based alerts."""

import httpx
from typing import Optional
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
from utils.logging_ import logger

N8N_WEBHOOK_URL = None


def _send_telegram_direct(message: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    try:
        resp = httpx.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"},
            timeout=10.0,
        )
        resp.raise_for_status()
        return True
    except Exception as e:
        logger.error(f"Telegram send failed: {e}")
        return False


def send_alert(title: str, body: str, level: str = "info") -> bool:
    emoji = {"info": "\u2139\ufe0f", "warning": "\u26a0\ufe0f", "error": "\u274c", "critical": "\U0001f525"}.get(level, "\u2139\ufe0f")
    message = f"{emoji} *ContextEngine: {title}*\n\n{body}"
    if N8N_WEBHOOK_URL:
        try:
            resp = httpx.post(N8N_WEBHOOK_URL, json={"title": title, "body": body, "level": level}, timeout=10.0)
            resp.raise_for_status()
            return True
        except Exception:
            pass
    return _send_telegram_direct(message)


def send_worker_status(session_id: str, status: str, details: str = "") -> bool:
    return send_alert(f"Worker {status}", f"Session: `{session_id}`\n{details}",
                      level="info" if status == "completed" else "warning")
