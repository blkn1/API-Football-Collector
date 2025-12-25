from __future__ import annotations

import asyncio
import os
import signal
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Awaitable

import yaml
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from zoneinfo import ZoneInfo

from src.collector.api_client import APIClient
from src.collector.rate_limiter import EmergencyStopError, RateLimiter
from src.jobs.incremental_daily import run_daily_fixtures_by_date, run_daily_standings
from src.jobs.injuries import run_injuries_hourly
from src.jobs.fixture_details import (
    run_fixture_details_backfill_90d,
    run_fixture_details_recent_finalize,
    run_fixture_details_backfill_season,
)
from src.jobs.stale_live_refresh import run_stale_live_refresh
from src.jobs.stale_scheduled_finalize import run_stale_scheduled_finalize
from src.jobs.backfill import (
    run_fixtures_backfill_league_season,
    run_standings_backfill_league_season,
)
from src.jobs.season_rollover import run_season_rollover_watch
from src.jobs.top_scorers import run_top_scorers_daily
from src.jobs.team_statistics import run_team_statistics_refresh
from src.jobs.static_bootstrap import (
    run_bootstrap_countries,
    run_bootstrap_leagues,
    run_bootstrap_teams,
    run_bootstrap_timezones,
)
from src.utils.config import load_api_config, load_rate_limiter_config
from src.utils.db import query_scalar
from src.utils.job_config import apply_bootstrap_scope_inheritance
from src.utils.logging import get_logger, setup_logging


logger = get_logger(component="collector_scheduler")


@dataclass(frozen=True)
class Job:
    job_id: str
    enabled: bool
    type: str
    endpoint: str | None
    params: dict[str, Any]
    interval: dict[str, Any] | None
    dependencies: list[str]
    filters: dict[str, Any]
    mode: dict[str, Any]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)

def _scheduler_tz() -> ZoneInfo:
    """
    Scheduler timezone for cron evaluation.
    - DB timestamps remain UTC (non-negotiable)
    - This only affects when cron triggers fire
    """
    tz_name = os.getenv("SCHEDULER_TIMEZONE", "UTC")
    try:
        return ZoneInfo(tz_name)
    except Exception:
        logger.warning("invalid_scheduler_timezone_fallback_utc", tz_name=tz_name)
        return ZoneInfo("UTC")


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]



def _load_jobs_from_yaml(path: Path) -> list[Job]:
    cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raw = cfg.get("jobs") or []
    out: list[Job] = []
    jobs_dir = path.parent
    for j in raw:
        if not isinstance(j, dict):
            continue
        j = apply_bootstrap_scope_inheritance(j, jobs_dir=jobs_dir)
        job = Job(
            job_id=str(j.get("job_id") or ""),
            enabled=bool(j.get("enabled", False)),
            type=str(j.get("type") or ""),
            endpoint=(str(j.get("endpoint")) if j.get("endpoint") is not None else None),
            params=(j.get("params") or {}),
            interval=(j.get("interval") or None),
            dependencies=[str(x) for x in (j.get("dependencies") or []) if x],
            filters=(j.get("filters") or {}),
            mode=(j.get("mode") or {}),
        )
        out.append(job)
    return [x for x in out if x.job_id and x.type]


def _job_files() -> list[Path]:
    root = _project_root()
    jobs_dir = Path(os.getenv("API_FOOTBALL_JOBS_DIR", str(root / "config" / "jobs")))
    files = [jobs_dir / "static.yaml", jobs_dir / "daily.yaml"]
    return [p for p in files if p.exists()]


def _to_trigger(interval_cfg: dict[str, Any], tz: ZoneInfo) -> CronTrigger | IntervalTrigger:
    t = str(interval_cfg.get("type") or "").strip().lower()
    if t == "cron":
        cron = interval_cfg.get("cron")
        if not cron:
            raise ValueError("Missing interval.cron")
        # Project uses 5-field cron (min hour day month weekday) in config.
        return CronTrigger.from_crontab(str(cron), timezone=tz)
    if t == "interval":
        seconds = interval_cfg.get("seconds")
        if seconds is None:
            raise ValueError("Missing interval.seconds")
        return IntervalTrigger(seconds=int(seconds), timezone=tz)
    raise ValueError(f"Unsupported interval.type: {t}")


def _tracked_leagues(job: Job) -> set[int]:
    tl = job.filters.get("tracked_leagues") or job.mode.get("tracked_leagues") or []
    if not isinstance(tl, list) or not tl:
        return set()
    return {int(x) for x in tl}


def _season(job: Job) -> int | None:
    params = job.params or {}
    s = params.get("season")
    if s is None:
        return None
    return int(s)


def _build_runner(
    job: Job,
    *,
    client: APIClient,
    limiter: RateLimiter,
) -> Callable[[], Awaitable[None]]:
    """
    Map job configs to concrete job runner coroutines.

    Supported:
    - static_bootstrap: bootstrap_timezones, bootstrap_countries, bootstrap_leagues, bootstrap_teams
    - incremental_daily: daily_fixtures_by_date, daily_standings, injuries_hourly, fixture_details_recent_finalize, fixture_details_backfill_90d,
      fixtures_backfill_league_season, standings_backfill_league_season
    """

    async def _run() -> None:
        logger.info("job_started", job_id=job.job_id, type=job.type, endpoint=job.endpoint, ts_utc=_utc_now().isoformat())
        if job.type == "static_bootstrap":
            if job.job_id == "bootstrap_timezones":
                await run_bootstrap_timezones(client=client, limiter=limiter)
            elif job.job_id == "bootstrap_countries":
                await run_bootstrap_countries(client=client, limiter=limiter)
            elif job.job_id == "bootstrap_leagues":
                season = _season(job)
                tracked = _tracked_leagues(job)
                if season is None or not tracked:
                    raise ValueError("bootstrap_leagues requires params.season and filters.tracked_leagues")
                await run_bootstrap_leagues(client=client, limiter=limiter, season=season, tracked_leagues=tracked)
            elif job.job_id == "bootstrap_teams":
                season = _season(job)
                tracked = _tracked_leagues(job)
                if season is None or not tracked:
                    raise ValueError("bootstrap_teams requires params.season and mode.tracked_leagues")
                await run_bootstrap_teams(client=client, limiter=limiter, season=season, tracked_leagues=tracked)
            else:
                raise ValueError(f"Unknown static_bootstrap job_id: {job.job_id}")

        elif job.type == "incremental_daily":
            if job.job_id == "daily_fixtures_by_date":
                # Config prohibits assuming date in YAML; we compute UTC "today" at runtime.
                date_utc = _utc_now().date().isoformat()
                await run_daily_fixtures_by_date(
                    target_date_utc=date_utc,
                    client=client,
                    limiter=limiter,
                    config_path=_project_root() / "config" / "jobs" / "daily.yaml",
                )
            elif job.job_id == "daily_standings":
                max_leagues = None
                try:
                    v = (job.mode or {}).get("max_leagues_per_run")
                    if v is not None and str(v).strip() != "":
                        max_leagues = int(v)
                except Exception:
                    max_leagues = None
                await run_daily_standings(
                    client=client,
                    limiter=limiter,
                    config_path=_project_root() / "config" / "jobs" / "daily.yaml",
                    max_leagues_per_run=max_leagues,
                )
            elif job.job_id == "injuries_hourly":
                await run_injuries_hourly(
                    client=client,
                    limiter=limiter,
                    config_path=_project_root() / "config" / "jobs" / "daily.yaml",
                )
            elif job.job_id == "fixture_details_recent_finalize":
                await run_fixture_details_recent_finalize(
                    client=client,
                    limiter=limiter,
                    config_path=_project_root() / "config" / "jobs" / "daily.yaml",
                )
            elif job.job_id == "fixture_details_backfill_90d":
                await run_fixture_details_backfill_90d(
                    client=client,
                    limiter=limiter,
                    config_path=_project_root() / "config" / "jobs" / "daily.yaml",
                )
            elif job.job_id == "fixture_details_backfill_season":
                await run_fixture_details_backfill_season(
                    client=client,
                    limiter=limiter,
                    config_path=_project_root() / "config" / "jobs" / "daily.yaml",
                )
            elif job.job_id == "fixtures_backfill_league_season":
                await run_fixtures_backfill_league_season(
                    client=client,
                    limiter=limiter,
                    config_path=_project_root() / "config" / "jobs" / "daily.yaml",
                )
            elif job.job_id == "standings_backfill_league_season":
                await run_standings_backfill_league_season(
                    client=client,
                    limiter=limiter,
                    config_path=_project_root() / "config" / "jobs" / "daily.yaml",
                )
            elif job.job_id == "season_rollover_watch":
                await run_season_rollover_watch(
                    client=client,
                    limiter=limiter,
                    config_path=_project_root() / "config" / "jobs" / "daily.yaml",
                )
            elif job.job_id == "stale_live_refresh":
                await run_stale_live_refresh(
                    client=client,
                    limiter=limiter,
                    config_path=_project_root() / "config" / "jobs" / "daily.yaml",
                )
            elif job.job_id == "stale_scheduled_finalize":
                await run_stale_scheduled_finalize(
                    client=client,
                    limiter=limiter,
                    config_path=_project_root() / "config" / "jobs" / "daily.yaml",
                )
            elif job.job_id == "top_scorers_daily":
                await run_top_scorers_daily(
                    client=client,
                    limiter=limiter,
                    config_path=_project_root() / "config" / "jobs" / "daily.yaml",
                )
            elif job.job_id == "team_statistics_refresh":
                await run_team_statistics_refresh(
                    client=client,
                    limiter=limiter,
                    config_path=_project_root() / "config" / "jobs" / "daily.yaml",
                )
            else:
                raise ValueError(f"Unknown incremental_daily job_id: {job.job_id}")
        else:
            raise ValueError(f"Unsupported job type for scheduler: {job.type}")

        logger.info("job_complete", job_id=job.job_id, ts_utc=_utc_now().isoformat())

    return _run


async def amain() -> int:
    setup_logging()

    api_cfg = load_api_config()
    rl_cfg = load_rate_limiter_config()
    sched_tz = _scheduler_tz()

    limiter = RateLimiter(
        max_tokens=rl_cfg.minute_soft_limit,
        refill_rate=float(rl_cfg.minute_soft_limit) / 60.0,
        emergency_stop_threshold=rl_cfg.emergency_stop_threshold,
    )
    client = APIClient(
        base_url=api_cfg.base_url,
        timeout_seconds=api_cfg.timeout_seconds,
        api_key_env=api_cfg.api_key_env,
    )

    def _env_flag(name: str, default: str = "1") -> bool:
        v = str(os.getenv(name, default)).strip().lower()
        return v in {"1", "true", "yes", "y", "on"}

    async def _maybe_bootstrap_countries_timezones_if_empty() -> None:
        """
        One-off guardrail:
        - If core.countries or core.timezones are empty, populate them immediately.
        - Idempotent: only runs when count == 0.
        - Controlled by env BOOTSTRAP_STATIC_ON_START (default enabled).
        """
        if not _env_flag("BOOTSTRAP_STATIC_ON_START", "1"):
            logger.info("bootstrap_static_on_start_disabled")
            return

        try:
            countries_cnt = int(await asyncio.to_thread(lambda: query_scalar("SELECT COUNT(*) FROM core.countries") or 0))
            timezones_cnt = int(await asyncio.to_thread(lambda: query_scalar("SELECT COUNT(*) FROM core.timezones") or 0))
        except Exception as e:
            # If schemas aren't applied yet, later jobs will fail anyway; log but don't crash scheduler here.
            logger.warning("bootstrap_static_on_start_db_check_failed", err=str(e))
            return

        if countries_cnt <= 0:
            logger.info("bootstrap_countries_on_start", reason="core.countries_empty")
            await run_bootstrap_countries(client=client, limiter=limiter)
        else:
            logger.info("bootstrap_countries_skipped", reason="core.countries_nonempty", count=countries_cnt)

        if timezones_cnt <= 0:
            logger.info("bootstrap_timezones_on_start", reason="core.timezones_empty")
            await run_bootstrap_timezones(client=client, limiter=limiter)
        else:
            logger.info("bootstrap_timezones_skipped", reason="core.timezones_nonempty", count=timezones_cnt)

    # Aşama 1: Don't wait for cron if tables are empty.
    await _maybe_bootstrap_countries_timezones_if_empty()

    # Load all enabled jobs (excluding live_loop which should run as a dedicated service).
    jobs: list[Job] = []
    for p in _job_files():
        jobs.extend(_load_jobs_from_yaml(p))

    enabled = [j for j in jobs if j.enabled and j.type != "live_loop"]
    if not enabled:
        logger.warning("no_enabled_jobs", job_files=[str(x) for x in _job_files()])

    scheduler = AsyncIOScheduler(timezone=sched_tz)

    # Add jobs
    for j in enabled:
        if not j.interval:
            logger.warning("job_missing_interval_skipped", job_id=j.job_id)
            continue
        try:
            trigger = _to_trigger(j.interval, sched_tz)
        except Exception as e:
            logger.error("job_invalid_interval_skipped", job_id=j.job_id, err=str(e))
            continue

        runner = _build_runner(j, client=client, limiter=limiter)

        scheduler.add_job(
            runner,
            trigger=trigger,
            id=j.job_id,
            name=j.job_id,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=60,
        )
        logger.info("job_scheduled", job_id=j.job_id, trigger=str(trigger))

    # Shutdown handling
    stop_event = asyncio.Event()

    def _stop(*_args: Any) -> None:
        logger.info("shutdown_signal_received")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop)
        except NotImplementedError:
            signal.signal(sig, lambda *_: _stop())

    try:
        scheduler.start()
        logger.info("scheduler_started")
        while not stop_event.is_set():
            # Emergency stop check: if quota already observed and too low, stop.
            try:
                _ = limiter.quota  # no-op, but keeps interface
            except EmergencyStopError as e:
                logger.error("emergency_stop_daily_quota_low", err=str(e))
                break
            await asyncio.sleep(1.0)
    finally:
        try:
            scheduler.shutdown(wait=False)
        except Exception:
            pass
        await client.aclose()
        logger.info("scheduler_stopped")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(amain()))


