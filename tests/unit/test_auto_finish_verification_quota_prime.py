from __future__ import annotations

from pathlib import Path

import pytest

from src.collector.api_client import APIResult
from src.collector.rate_limiter import RateLimiter
from src.jobs.auto_finish_verification import run_auto_finish_verification


class FakeClient:
    async def get(self, endpoint: str, params=None):
        assert endpoint == "/status"
        # Provide quota headers so limiter.quota becomes non-None
        return APIResult(
            status_code=200,
            data={"response": []},
            headers={
                "x-ratelimit-requests-remaining": "70000",
                "X-RateLimit-Remaining": "200",
            },
        )


@pytest.mark.asyncio
async def test_auto_finish_verification_primes_quota_when_unknown(tmp_path: Path, monkeypatch) -> None:
    # Minimal daily config: tracked_leagues is required by _load_config
    cfg_path = tmp_path / "daily.yaml"
    cfg_path.write_text("tracked_leagues:\n  - id: 39\n    season: 2025\n", encoding="utf-8")

    limiter = RateLimiter(max_tokens=10, refill_rate=10.0, emergency_stop_threshold=1)
    client = FakeClient()

    # Avoid DB access in selector for this unit test.
    monkeypatch.setattr(
        "src.jobs.auto_finish_verification._select_verification_fixture_ids",
        lambda *, limit, tracked_league_ids: [],
    )

    # Should not raise TypeError when daily_remaining is None initially.
    await run_auto_finish_verification(client=client, limiter=limiter, config_path=cfg_path)

