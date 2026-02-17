"""OpenRouter credit tracker.

Fetches balance, usage, and runway estimates from OpenRouter API.
Integrated into cockpit updates and Prometheus metrics.
Sends Telegram alert when balance drops below threshold.
"""

import os
import httpx
import logging
from typing import Optional
from datetime import datetime, timezone

logger = logging.getLogger("context-engine")

# Alert when remaining credits drop below this
LOW_CREDIT_THRESHOLD = 10.0  # dollars

# Cache to avoid hammering the API (refresh every 30 min)
_cache = {"data": None, "fetched_at": 0}
CACHE_TTL = 1800  # 30 minutes


def _get_api_key() -> str:
    """Get OpenRouter API key from settings or env."""
    try:
        from routers.settings import _load_settings
        s = _load_settings()
        if s.llm.api_key and s.llm.api_key.startswith("sk-or-"):
            return s.llm.api_key
    except Exception:
        pass
    return os.environ.get("LLM_API_KEY", "") or os.environ.get("OPENROUTER_API_KEY", "")


def fetch_credits(force: bool = False) -> Optional[dict]:
    """
    Fetch current credit balance from OpenRouter.
    
    Returns:
        {
            "total_credits": float,
            "total_usage": float,
            "remaining": float,
            "usage_daily": float,
            "usage_weekly": float,
            "usage_monthly": float,
            "days_remaining": float or None,
            "low_balance": bool,
            "fetched_at": str (ISO)
        }
    """
    import time
    now = time.time()

    # Return cache if fresh
    if not force and _cache["data"] and (now - _cache["fetched_at"]) < CACHE_TTL:
        return _cache["data"]

    api_key = _get_api_key()
    if not api_key or not api_key.startswith("sk-or-"):
        logger.debug("Credit tracker: not an OpenRouter key, skipping")
        return None

    headers = {"Authorization": f"Bearer {api_key}"}

    try:
        # Fetch both endpoints
        with httpx.Client(timeout=10) as client:
            credits_resp = client.get("https://openrouter.ai/api/v1/credits", headers=headers)
            credits_resp.raise_for_status()
            credits_data = credits_resp.json().get("data", {})

            key_resp = client.get("https://openrouter.ai/api/v1/auth/key", headers=headers)
            key_resp.raise_for_status()
            key_data = key_resp.json().get("data", {})

        total = credits_data.get("total_credits", 0)
        used = credits_data.get("total_usage", 0)
        remaining = total - used
        daily = key_data.get("usage_daily", 0)

        # Estimate runway
        days_remaining = None
        if daily > 0.01:
            days_remaining = round(remaining / daily, 1)

        result = {
            "total_credits": round(total, 2),
            "total_usage": round(used, 2),
            "remaining": round(remaining, 2),
            "usage_daily": round(daily, 4),
            "usage_weekly": round(key_data.get("usage_weekly", 0), 4),
            "usage_monthly": round(key_data.get("usage_monthly", 0), 4),
            "days_remaining": days_remaining,
            "low_balance": remaining < LOW_CREDIT_THRESHOLD,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }

        _cache["data"] = result
        _cache["fetched_at"] = now

        if result["low_balance"]:
            logger.warning(f"Credit tracker: LOW BALANCE — ${remaining:.2f} remaining (~{days_remaining} days)")

        return result

    except Exception as e:
        logger.warning(f"Credit tracker: fetch failed: {e}")
        return _cache.get("data")  # Return stale cache if available


def format_for_cockpit() -> str:
    """Format credit info as markdown for cockpit insertion."""
    data = fetch_credits()
    if not data:
        return "| LLM Credits | Unknown | — |\n"

    status = "🔴 LOW" if data["low_balance"] else "🟢 OK"
    runway = f"~{data['days_remaining']:.0f} days" if data["days_remaining"] else "—"

    lines = [
        f"| LLM Credits | ${data['remaining']:.2f} remaining | {status} |",
        f"| LLM Spend | ${data['usage_daily']:.2f}/day · ${data['usage_monthly']:.2f}/month | Runway: {runway} |",
    ]
    return "\n".join(lines)


def check_and_alert() -> bool:
    """Check balance and send Telegram alert if low. Returns True if alert sent."""
    data = fetch_credits(force=True)
    if not data or not data["low_balance"]:
        return False

    try:
        from services.webhook import send_alert
        runway = f"~{data['days_remaining']:.0f} days" if data["days_remaining"] else "unknown"
        msg = (
            f"⚠️ OpenRouter Low Balance\n"
            f"Remaining: ${data['remaining']:.2f}\n"
            f"Daily spend: ${data['usage_daily']:.2f}\n"
            f"Runway: {runway}\n"
            f"Top up: https://openrouter.ai/credits"
        )
        send_alert(msg)
        logger.info("Credit tracker: low balance alert sent")
        return True
    except Exception as e:
        logger.warning(f"Credit tracker: alert failed: {e}")
        return False
