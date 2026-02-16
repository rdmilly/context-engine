"""
LLM failover chain.

Tries providers in order until one works:
1. Primary (configured provider, e.g. OpenRouter)
2. Secondary (e.g. local Ollama)
3. Emergency (return cached/empty response)

Integrates with the existing OpenRouterClient.
"""

import os
import logging
import httpx
from typing import Optional

logger = logging.getLogger("context-engine")


class FailoverChain:
    """
    Wraps the LLM client with automatic failover.

    Usage:
        chain = FailoverChain(primary_client)
        chain.add_fallback("ollama", "http://localhost:11434/v1", model="llama3.2:3b")
        result = await chain.call(messages, task="session_summary")
    """

    def __init__(self, primary_client):
        self.primary = primary_client
        self.fallbacks = []
        self._primary_failures = 0
        self._max_primary_failures = 3  # Switch to fallback after 3 consecutive failures
        self._active_provider = "primary"

    def add_fallback(self, name: str, base_url: str, api_key: str = "", model: str = ""):
        """Add a fallback provider."""
        self.fallbacks.append({
            "name": name,
            "base_url": base_url.rstrip("/"),
            "api_key": api_key,
            "model": model,
        })
        logger.info(f"Failover: added fallback '{name}' at {base_url}")

    async def call(self, messages: list, task: str = "", **kwargs) -> Optional[str]:
        """
        Try primary, then fallbacks, then return None.
        """
        # Try primary
        if self._primary_failures < self._max_primary_failures:
            try:
                result = await self.primary.call(messages, task=task, **kwargs)
                if result:
                    self._primary_failures = 0
                    self._active_provider = "primary"
                    return result
            except Exception as e:
                self._primary_failures += 1
                logger.warning(f"Failover: primary failed ({self._primary_failures}/{self._max_primary_failures}): {e}")
        else:
            logger.info("Failover: primary disabled (too many failures), trying fallbacks")

        # Try fallbacks
        for fb in self.fallbacks:
            try:
                result = await self._call_fallback(fb, messages, task, **kwargs)
                if result:
                    self._active_provider = fb["name"]
                    logger.info(f"Failover: using fallback '{fb['name']}'")
                    return result
            except Exception as e:
                logger.warning(f"Failover: fallback '{fb['name']}' failed: {e}")

        # Emergency: return None, let caller handle gracefully
        self._active_provider = "none"
        logger.error("Failover: all providers failed")
        return None

    async def _call_fallback(self, fb: dict, messages: list, task: str, **kwargs) -> Optional[str]:
        """Call a fallback provider using OpenAI-compatible API."""
        model = fb.get("model") or kwargs.get("model") or "llama3.2:3b"

        headers = {"Content-Type": "application/json"}
        if fb.get("api_key"):
            headers["Authorization"] = f"Bearer {fb['api_key']}"

        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": kwargs.get("max_tokens", 4096),
            "temperature": kwargs.get("temperature", 0.3),
        }

        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{fb['base_url']}/chat/completions",
                headers=headers,
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"]

    def reset_primary(self):
        """Reset primary failure count (e.g., after config change)."""
        self._primary_failures = 0
        self._active_provider = "primary"
        logger.info("Failover: primary reset")

    @property
    def status(self) -> dict:
        return {
            "active_provider": self._active_provider,
            "primary_failures": self._primary_failures,
            "fallbacks": [fb["name"] for fb in self.fallbacks],
            "primary_disabled": self._primary_failures >= self._max_primary_failures,
        }


# ── Singleton ─────────────────────────────────────────────────
_chain: Optional[FailoverChain] = None


def get_failover_chain(primary_client=None) -> FailoverChain:
    """Get or create the failover chain."""
    global _chain
    if _chain is None and primary_client is not None:
        _chain = FailoverChain(primary_client)

        # Auto-configure Ollama fallback if OLLAMA_URL is set
        ollama_url = os.environ.get("OLLAMA_URL", "")
        if ollama_url:
            _chain.add_fallback(
                "ollama",
                f"{ollama_url}/v1" if not ollama_url.endswith("/v1") else ollama_url,
                model=os.environ.get("OLLAMA_MODEL_LIGHT", "llama3.2:3b"),
            )

    return _chain
