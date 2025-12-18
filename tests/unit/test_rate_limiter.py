from __future__ import annotations

import threading
import time

from collector.rate_limiter import RateLimiter


def test_acquire_token_blocks_when_exhausted():
    limiter = RateLimiter(max_tokens=2, refill_rate=1.0)  # 1 token/sec

    limiter.acquire_token()
    limiter.acquire_token()

    start = time.monotonic()
    limiter.acquire_token()
    elapsed = time.monotonic() - start

    assert elapsed >= 0.9


def test_refill_mechanism():
    limiter = RateLimiter(max_tokens=1, refill_rate=10.0)  # fast refill

    limiter.acquire_token()
    assert limiter.tokens < 1.0

    time.sleep(0.2)
    assert limiter.tokens >= 1.0


def test_thread_safety():
    # Keep refill effectively disabled to make the assertion deterministic.
    # (Refill is tested separately in test_refill_mechanism.)
    # NOTE: RateLimiter defaults to initial_tokens=0 to avoid a startup burst in production.
    # For this unit test we want a full bucket so threads do not block.
    limiter = RateLimiter(max_tokens=50, refill_rate=0.0001, initial_tokens=50)

    start_barrier = threading.Barrier(11)

    def worker():
        start_barrier.wait()
        limiter.acquire_token()

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()

    start_barrier.wait()

    for t in threads:
        t.join(timeout=2.0)
        assert not t.is_alive()

    # 10 tokens consumed from 50 (refill negligible)
    remaining = limiter.tokens
    assert 39.0 <= remaining <= 40.1


