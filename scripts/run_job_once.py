from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

import yaml

from src.collector.api_client import APIClient
from src.collector.rate_limiter import RateLimiter
from src.utils.logging import get_logger

logger = get_logger(component="run_job_once")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    # When running as /app/scripts/run_job_once.py, sys.path[0] is /app/scripts,
    # so `import src.*` fails unless /app is on sys.path.
    sys.path.insert(0, str(PROJECT_ROOT))


def _daily_config_path() -> Path:
    p = os.getenv("API_FOOTBALL_DAILY_CONFIG") or "config/jobs/daily.yaml"
    return Path(p)

def _maybe_filter_daily_config(cfg_path: Path) -> Path:
    """
    Optional: limit the run to a single league to keep manual tests cheap.

    Env:
      ONLY_LEAGUE_ID=39
    """
    raw = (os.getenv("ONLY_LEAGUE_ID") or "").strip()
    if not raw:
        return cfg_path
    try:
        only_id = int(raw)
    except Exception:
        raise SystemExit("ONLY_LEAGUE_ID must be an int")

    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    tracked = cfg.get("tracked_leagues") or []
    if not isinstance(tracked, list):
        raise SystemExit("daily config tracked_leagues must be a list")
    filtered = [x for x in tracked if isinstance(x, dict) and int(x.get("id") or -1) == only_id]
    if not filtered:
        raise SystemExit(f"ONLY_LEAGUE_ID={only_id} not found in tracked_leagues")

    out_cfg = {"tracked_leagues": filtered}
    # Preserve top-level season if present (helps when league items omit season)
    if cfg.get("season") is not None:
        out_cfg["season"] = cfg.get("season")

    tmpdir = Path(tempfile.gettempdir())
    tmp_path = tmpdir / f"daily_only_league_{only_id}.yaml"
    tmp_path.write_text(yaml.safe_dump(out_cfg, sort_keys=False), encoding="utf-8")
    logger.info("filtered_daily_config_written", only_league_id=only_id, path=str(tmp_path))
    return tmp_path


async def _run(job_id: str) -> None:
    limiter = RateLimiter.from_config("config/rate_limiter.yaml")
    client = APIClient.from_env()
    cfg = _maybe_filter_daily_config(_daily_config_path())

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


