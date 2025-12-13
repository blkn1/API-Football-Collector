from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, TypedDict

import redis

from utils.logging import get_logger


logger = get_logger(component="delta_detector")


class FixtureState(TypedDict, total=False):
    status: str | None
    goals_home: int | None
    goals_away: int | None
    elapsed: int | None


@dataclass(frozen=True)
class DeltaResult:
    changed: bool
    diff: dict[str, Any]


def create_redis_client_from_env(*, redis_url_env: str = "REDIS_URL") -> redis.Redis:
    """
    Create a Redis client using REDIS_URL from environment.
    Default: redis://localhost:6379/0
    """
    url = os.getenv(redis_url_env, "redis://localhost:6379/0")
    return redis.Redis.from_url(url, decode_responses=True)


class DeltaDetector:
    """
    Redis-backed delta detector for live fixtures.

    - Cache key: fixture:{fixture_id}
    - Payload: JSON with keys {status, goals_home, goals_away, elapsed}
    - TTL: default 2 hours (7200s). Each update refreshes TTL.
    - Error handling: fail-open (treat as changed) if Redis is unavailable or payload is corrupt.
    """

    def __init__(self, redis_client: redis.Redis, *, ttl_seconds: int = 7200) -> None:
        self.redis = redis_client
        self.ttl_seconds = int(ttl_seconds)

    def _key(self, fixture_id: int) -> str:
        return f"fixture:{int(fixture_id)}"

    def _normalize_state(self, current_state: dict[str, Any]) -> FixtureState:
        # Only keep the compared fields; tolerate missing keys.
        def _to_int(v: Any) -> int | None:
            if v is None:
                return None
            try:
                return int(v)
            except Exception:
                return None

        status = current_state.get("status")
        if status is not None:
            status = str(status)

        return {
            "status": status,
            "goals_home": _to_int(current_state.get("goals_home")),
            "goals_away": _to_int(current_state.get("goals_away")),
            "elapsed": _to_int(current_state.get("elapsed")),
        }

    def _get_cached(self, fixture_id: int) -> FixtureState | None:
        key = self._key(fixture_id)
        try:
            raw = self.redis.get(key)
        except redis.exceptions.RedisError:
            logger.warning("redis_get_failed", fixture_id=fixture_id)
            return None

        if not raw:
            return None

        try:
            payload = json.loads(raw)
        except Exception:
            logger.warning("redis_payload_invalid_json", fixture_id=fixture_id)
            return None

        if not isinstance(payload, dict):
            return None

        # Defensive normalization (cached payload may be from older versions)
        return self._normalize_state(payload)

    def has_changed(self, fixture_id: int, current_state: dict[str, Any]) -> bool:
        """
        Check if fixture state changed since last check.

        Args:
            fixture_id: Fixture ID
            current_state: dict with keys: status, goals_home, goals_away, elapsed

        Returns:
            True if changed, False if same.
            Fail-open: if cache is missing OR Redis is unavailable, returns True.
        """
        current = self._normalize_state(current_state)
        cached = self._get_cached(fixture_id)
        if cached is None:
            return True

        return (
            cached.get("status") != current.get("status")
            or cached.get("goals_home") != current.get("goals_home")
            or cached.get("goals_away") != current.get("goals_away")
            or cached.get("elapsed") != current.get("elapsed")
        )

    def get_diff(self, fixture_id: int, current_state: dict[str, Any]) -> dict[str, Any]:
        """
        Returns a diff dict: {field: {old: X, new: Y}} for fields that changed.
        If first time (no cache), returns all tracked fields with old=None.
        If Redis is unavailable/corrupt cache, returns {"_cache_unavailable": True}.
        """
        current = self._normalize_state(current_state)
        cached = self._get_cached(fixture_id)

        if cached is None:
            # Could be first-seen OR redis failure; we can't distinguish perfectly here without more signals.
            # Heuristic: attempt an EXISTS call to distinguish "missing key" from "redis down".
            try:
                exists = self.redis.exists(self._key(fixture_id))
            except redis.exceptions.RedisError:
                return {"_cache_unavailable": True}
            if not exists:
                return {
                    k: {"old": None, "new": current.get(k)}
                    for k in ("status", "goals_home", "goals_away", "elapsed")
                }
            return {"_cache_unavailable": True}

        diff: dict[str, Any] = {}
        for k in ("status", "goals_home", "goals_away", "elapsed"):
            old = cached.get(k)
            new = current.get(k)
            if old != new:
                diff[k] = {"old": old, "new": new}
        return diff

    def update_cache(self, fixture_id: int, current_state: dict[str, Any]) -> None:
        """Store current state in Redis with TTL."""
        key = self._key(fixture_id)
        payload = self._normalize_state(current_state)
        try:
            self.redis.setex(key, self.ttl_seconds, json.dumps(payload, separators=(",", ":")))
        except redis.exceptions.RedisError:
            logger.warning("redis_setex_failed", fixture_id=fixture_id)

    def clear_cache(self, fixture_id: int) -> None:
        """Delete fixture state from Redis."""
        try:
            self.redis.delete(self._key(fixture_id))
        except redis.exceptions.RedisError:
            logger.warning("redis_delete_failed", fixture_id=fixture_id)


