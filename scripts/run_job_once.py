from __future__ import annotations

import asyncio
import os
from pathlib import Path

from src.collector.api_client import APIClient
from src.collector.rate_limiter import RateLimiter
from src.utils.logging import get_logger

logger = get_logger(component="run_job_once")


def _daily_config_path() -> Path:
    p = os.getenv("API_FOOTBALL_DAILY_CONFIG") or "config/jobs/daily.yaml"
    return Path(p)


async def _run(job_id: str) -> None:
    limiter = RateLimiter.from_config("config/rate_limiter.yaml")
    client = APIClient.from_env()
    cfg = _daily_config_path()

    if job_id == "top_scorers_daily":
        from src.jobs.top_scorers import run_top_scorers_daily

        await run_top_scorers_daily(client=client, limiter=limiter, config_path=cfg)
        return

    if job_id == "team_statistics_refresh":
        from src.jobs.team_statistics import run_team_statistics_refresh

        await run_team_statistics_refresh(client=client, limiter=limiter, config_path=cfg)
        return

    raise SystemExit(f"Unsupported job_id: {job_id}")


def main() -> None:
    job_id = (os.getenv("JOB_ID") or "").strip()
    if not job_id:
        raise SystemExit("JOB_ID is required (e.g. JOB_ID=top_scorers_daily)")
    logger.info("run_job_once_start", job_id=job_id)
    asyncio.run(_run(job_id))
    logger.info("run_job_once_complete", job_id=job_id)


if __name__ == "__main__":
    main()


