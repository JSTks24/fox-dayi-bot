"""Small in-memory cache and cooldown primitives."""

import math
import time
from typing import Generic, Hashable, TypeVar

T = TypeVar("T")


class TTLCache(Generic[T]):
    """A lightweight TTL cache with lazy expiration."""

    def __init__(self):
        self._entries: dict[Hashable, tuple[T, float]] = {}

    def _now(self) -> float:
        return time.monotonic()

    def _is_expired(self, expires_at: float) -> bool:
        return expires_at <= self._now()

    def _cleanup_key(self, key: Hashable) -> bool:
        entry = self._entries.get(key)
        if entry is None:
            return False
        _, expires_at = entry
        if self._is_expired(expires_at):
            self._entries.pop(key, None)
            return False
        return True

    def set(self, key: Hashable, value: T, ttl_seconds: float) -> None:
        ttl_seconds = max(0.0, float(ttl_seconds))
        self._entries[key] = (value, self._now() + ttl_seconds)

    def get(self, key: Hashable, default: T | None = None) -> T | None:
        if not self._cleanup_key(key):
            return default
        return self._entries[key][0]

    def pop(self, key: Hashable, default: T | None = None) -> T | None:
        if not self._cleanup_key(key):
            self._entries.pop(key, None)
            return default
        value, _ = self._entries.pop(key)
        return value

    def delete(self, key: Hashable) -> None:
        self._entries.pop(key, None)

    def __contains__(self, key: Hashable) -> bool:
        return self._cleanup_key(key)


class CooldownManager:
    """Maintain per-key cooldowns and discard expired entries on access."""

    def __init__(self, default_seconds: float):
        self.default_seconds = max(0.0, float(default_seconds))
        self._cache: TTLCache[bool] = TTLCache()

    def get_remaining(self, key: Hashable) -> int:
        entry = self._cache._entries.get(key)
        if entry is None:
            return 0
        _, expires_at = entry
        remaining = expires_at - self._cache._now()
        if remaining <= 0:
            self._cache.delete(key)
            return 0
        return max(1, math.ceil(remaining))

    def set_cooldown(self, key: Hashable, seconds: float | None = None) -> None:
        ttl_seconds = self.default_seconds if seconds is None else max(0.0, float(seconds))
        self._cache.set(key, True, ttl_seconds)

    def check(self, key: Hashable) -> tuple[bool, int]:
        remaining = self.get_remaining(key)
        return remaining > 0, remaining

    def check_and_update(self, key: Hashable, seconds: float | None = None) -> tuple[bool, int]:
        is_on_cooldown, remaining = self.check(key)
        if is_on_cooldown:
            return True, remaining
        self.set_cooldown(key, seconds=seconds)
        return False, 0
