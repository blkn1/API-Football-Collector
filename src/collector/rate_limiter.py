from __future__ import annotations

import time
from dataclasses import dataclass
from threading import Lock


@dataclass(frozen=True)
class QuotaSnapshot:
    daily_remaining: int | None
    minute_remaining: int | None


class EmergencyStopError(RuntimeError):
    """
    Raised when API quota is dangerously low and the system must stop to avoid exhausting daily budget
    (and potential firewall / operational issues).
    """


class RateLimiter:
    """
    Token Bucket rate limiter (in-memory, Phase 1).

    API-Football constraints:
    - ~300 req/minute  -> max_tokens=300
    - refill_rate=5.0  -> 300/60 tokens/sec
    """

    def __init__(
        self,
        *,
        max_tokens: int = 300,
        refill_rate: float = 5.0,
        emergency_stop_threshold: int | None = None,
    ) -> None:
        if max_tokens <= 0:
            raise ValueError("max_tokens must be > 0")
        if refill_rate <= 0:
            raise ValueError("refill_rate must be > 0")

        self._max_tokens = float(max_tokens)
        self._tokens = float(max_tokens)
        self._refill_rate = float(refill_rate)
        self._last_refill = time.monotonic()
        self._lock = Lock()

        # Quota tracking from response headers (best-effort)
        self._daily_remaining: int | None = None
        self._minute_remaining: int | None = None
        self._emergency_stop_threshold: int | None = int(emergency_stop_threshold) if emergency_stop_threshold is not None else None

    @property
    def tokens(self) -> float:
        with self._lock:
            self._refill_locked()
            return self._tokens

    @property
    def quota(self) -> QuotaSnapshot:
        with self._lock:
            return QuotaSnapshot(
                daily_remaining=self._daily_remaining,
                minute_remaining=self._minute_remaining,
            )

    def acquire_token(self) -> None:
        """
        Blocking call. Waits until at least 1 token is available, then consumes it.
        Thread-safe.
        """
        while True:
            with self._lock:
                self._raise_if_emergency_stop_locked()
                self._refill_locked()
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return

                # tokens needed to reach 1
                missing = 1.0 - self._tokens
                wait_seconds = missing / self._refill_rate

            # Sleep outside lock to allow other threads to progress/refill.
            time.sleep(max(0.0, wait_seconds))

    def update_from_headers(self, headers: dict[str, str]) -> None:
        """
        Update internal state from API-Football quota headers.

        Headers (case-insensitive in HTTP, but httpx provides a case-insensitive mapping):
        - x-ratelimit-requests-remaining : daily remaining
        - X-RateLimit-Remaining          : per-minute remaining
        """
        daily = _parse_int_header(headers, "x-ratelimit-requests-remaining")
        minute = _parse_int_header(headers, "X-RateLimit-Remaining")

        with self._lock:
            self._daily_remaining = daily
            self._minute_remaining = minute
            self._raise_if_emergency_stop_locked()

            # If API reports a lower minute remaining than our local tokens, clamp.
            if minute is not None:
                self._tokens = min(self._tokens, float(minute))
                self._tokens = max(0.0, self._tokens)

    def _raise_if_emergency_stop_locked(self) -> None:
        thr = self._emergency_stop_threshold
        if thr is None:
            return
        if self._daily_remaining is None:
            return
        if self._daily_remaining < thr:
            # Hard stop. Callers should catch this and stop scheduling / polling.
            raise EmergencyStopError(f"Emergency stop: daily_remaining={self._daily_remaining} < threshold={thr}")

    def _refill_locked(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        if elapsed <= 0:
            return

        self._tokens = min(self._max_tokens, self._tokens + elapsed * self._refill_rate)
        self._last_refill = now


def _parse_int_header(headers: dict[str, str], key: str) -> int | None:
    # Work with normal dicts and httpx.Headers (case-insensitive mapping).
    raw = headers.get(key) if hasattr(headers, "get") else None
    if raw is None:
        # fallback to case-insensitive scan
        for k, v in headers.items():
            if k.lower() == key.lower():
                raw = v
                break
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


