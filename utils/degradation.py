"""Graceful degradation manager for ContextEngine.

Tracks dependency health and determines operational level:
- FULL: All systems operational
- PARTIAL: Some non-critical deps degraded (e.g. ChromaDB search slow)
- MINIMAL: Core deps degraded (e.g. KB mount missing, using cache)
- OFFLINE: Cannot serve any useful context

Also provides:
- In-memory cache for master context (last known good)
- Circuit breaker for external service calls
- Dependency health tracking with auto-recovery
"""

import time
from typing import Optional, Dict, Any
from enum import Enum
from utils.logging_ import logger


class DegradationLevel(str, Enum):
    FULL = "full"
    PARTIAL = "partial"
    MINIMAL = "minimal"
    OFFLINE = "offline"


class CircuitBreaker:
    """Simple circuit breaker for external service calls."""

    def __init__(self, name: str, failure_threshold: int = 3, recovery_timeout: float = 60.0):
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.failure_count = 0
        self.last_failure_time = 0.0
        self.state = "closed"  # closed = normal, open = blocking, half_open = testing

    def record_success(self):
        self.failure_count = 0
        if self.state != "closed":
            logger.info(f"CircuitBreaker[{self.name}]: recovered, closing circuit")
        self.state = "closed"

    def record_failure(self):
        self.failure_count += 1
        self.last_failure_time = time.time()
        if self.failure_count >= self.failure_threshold:
            if self.state != "open":
                logger.warning(f"CircuitBreaker[{self.name}]: opened after {self.failure_count} failures")
            self.state = "open"

    def can_proceed(self) -> bool:
        if self.state == "closed":
            return True
        if self.state == "open":
            elapsed = time.time() - self.last_failure_time
            if elapsed >= self.recovery_timeout:
                self.state = "half_open"
                logger.info(f"CircuitBreaker[{self.name}]: half-open, allowing test call")
                return True
            return False
        # half_open — allow one test
        return True


class DegradationManager:
    """Central manager for graceful degradation."""

    def __init__(self):
        # In-memory cache
        self._master_context_cache: Optional[str] = None
        self._cache_timestamp: float = 0.0
        self._cache_source: str = "none"  # "live", "cache", "bootstrap"

        # Circuit breakers
        self.breakers: Dict[str, CircuitBreaker] = {
            "openrouter": CircuitBreaker("openrouter", failure_threshold=3, recovery_timeout=120.0),
            "chromadb": CircuitBreaker("chromadb", failure_threshold=5, recovery_timeout=60.0),
            "kb_gateway": CircuitBreaker("kb_gateway", failure_threshold=3, recovery_timeout=30.0),
        }

        # Dependency health
        self._dep_health: Dict[str, Dict[str, Any]] = {
            "kb_gateway": {"healthy": True, "last_check": 0, "error": None},
            "chromadb": {"healthy": True, "last_check": 0, "error": None},
            "openrouter": {"healthy": True, "last_check": 0, "error": None},
        }

    # --- Cache management ---

    def update_cache(self, content: str, source: str = "live"):
        """Update the in-memory master context cache."""
        if content and len(content) > 50:  # Sanity check
            self._master_context_cache = content
            self._cache_timestamp = time.time()
            self._cache_source = source

    def get_cached_context(self) -> Optional[str]:
        """Return cached master context if available."""
        return self._master_context_cache

    @property
    def cache_age_seconds(self) -> float:
        if self._cache_timestamp == 0:
            return float("inf")
        return time.time() - self._cache_timestamp

    @property
    def cache_info(self) -> dict:
        return {
            "available": self._master_context_cache is not None,
            "source": self._cache_source,
            "age_seconds": round(self.cache_age_seconds, 1) if self._master_context_cache else None,
            "size_bytes": len(self._master_context_cache) if self._master_context_cache else 0,
        }

    # --- Dependency health ---

    def mark_healthy(self, dep: str):
        if dep in self._dep_health:
            self._dep_health[dep] = {"healthy": True, "last_check": time.time(), "error": None}
        if dep in self.breakers:
            self.breakers[dep].record_success()

    def mark_unhealthy(self, dep: str, error: str = "unknown"):
        if dep in self._dep_health:
            was_healthy = self._dep_health[dep]["healthy"]
            self._dep_health[dep] = {"healthy": False, "last_check": time.time(), "error": error}
            if was_healthy:
                logger.warning(f"Degradation: {dep} became unhealthy: {error}")
        if dep in self.breakers:
            self.breakers[dep].record_failure()

    def can_call(self, dep: str) -> bool:
        """Check if a dependency call should be attempted (circuit breaker)."""
        if dep in self.breakers:
            return self.breakers[dep].can_proceed()
        return True

    # --- Overall level ---

    @property
    def level(self) -> DegradationLevel:
        """Determine current degradation level based on dependency health."""
        kb_ok = self._dep_health["kb_gateway"]["healthy"]
        chroma_ok = self._dep_health["chromadb"]["healthy"]
        or_ok = self._dep_health["openrouter"]["healthy"]

        if kb_ok and chroma_ok and or_ok:
            return DegradationLevel.FULL

        # KB down but we have cache = partial
        if not kb_ok and self._master_context_cache:
            if chroma_ok:
                return DegradationLevel.PARTIAL
            return DegradationLevel.MINIMAL

        # ChromaDB down but KB up = partial
        if not chroma_ok and kb_ok:
            return DegradationLevel.PARTIAL

        # OpenRouter down = partial (worker paused, reads still work)
        if not or_ok and kb_ok:
            return DegradationLevel.PARTIAL

        # KB down, no cache = minimal if chromadb has data
        if not kb_ok and not self._master_context_cache and chroma_ok:
            return DegradationLevel.MINIMAL

        # Everything down
        if not kb_ok and not chroma_ok:
            return DegradationLevel.OFFLINE

        return DegradationLevel.MINIMAL

    @property
    def status(self) -> dict:
        return {
            "level": self.level.value,
            "dependencies": {
                name: {
                    "healthy": info["healthy"],
                    "error": info["error"],
                    "circuit_breaker": self.breakers[name].state if name in self.breakers else "n/a",
                }
                for name, info in self._dep_health.items()
            },
            "cache": self.cache_info,
        }


# Singleton
_manager: Optional[DegradationManager] = None


def get_manager() -> DegradationManager:
    global _manager
    if _manager is None:
        _manager = DegradationManager()
    return _manager
