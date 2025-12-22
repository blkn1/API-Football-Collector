from __future__ import annotations

from pathlib import Path

from src.collector.api_client import APIClient
from src.collector.rate_limiter import RateLimiter
from src.utils.logging import get_logger


logger = get_logger(component="jobs_incremental_daily")


async def run_daily_fixtures_by_date(
    *,
    target_date_utc: str,
    client: APIClient,
    limiter: RateLimiter,
    config_path: Path,
) -> None:
    # Reuse the existing Phase 3 implementation (keeps RAW/CORE/MART + coverage behavior consistent).
    from scripts.daily_sync import sync_daily_fixtures

    await sync_daily_fixtures(
        target_date_utc=target_date_utc,
        dry_run=False,
        config_path=config_path,
        client=client,
        limiter=limiter,
        with_standings=False,
    )
    logger.info("daily_fixtures_by_date_complete", date_utc=target_date_utc)


async def run_daily_standings(
    *, client: APIClient, limiter: RateLimiter, config_path: Path, max_leagues_per_run: int | None = None
) -> None:
    from src.utils.standings import sync_standings

    await sync_standings(
        dry_run=False,
        config_path=config_path,
        client=client,
        limiter=limiter,
        max_leagues_per_run=max_leagues_per_run,
        progress_job_id="daily_standings",
    )
    logger.info("daily_standings_complete")


