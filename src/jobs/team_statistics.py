from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import psycopg2.extras
import yaml

from src.collector.api_client import APIClient, APIClientError, APIResult, RateLimitError
from src.collector.rate_limiter import EmergencyStopError, RateLimiter
from src.transforms.team_statistics import transform_team_statistics
from src.utils.db import get_db_connection, upsert_core, upsert_mart_coverage, upsert_raw
from src.utils.logging import get_logger


logger = get_logger(component="jobs_team_statistics")


def _load_tracked_leagues(config_path: Path) -> list[dict[str, Any]]:
    cfg = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    tracked = cfg.get("tracked_leagues") or []
    if not isinstance(tracked, list) or not tracked:
        raise ValueError(f"Missing tracked_leagues in {config_path}")
    out: list[dict[str, Any]] = []
    for x in tracked:
        if not isinstance(x, dict) or x.get("id") is None:
            continue
        out.append(
            {
                "id": int(x["id"]),
                "season": (int(x["season"]) if x.get("season") is not None else None),
                "name": x.get("name"),
            }
        )
    if not out:
        raise ValueError(f"No valid tracked_leagues items in {config_path}")
    top_season = cfg.get("season")
    if top_season is None and any(i["season"] is None for i in out):
        raise ValueError(
            f"Missing season in {config_path}. Set top-level season or provide season per tracked league."
        )
    if top_season is not None:
        for i in out:
            if i["season"] is None:
                i["season"] = int(top_season)
    return out


async def _safe_get_envelope(
    *,
    client: APIClient,
    limiter: RateLimiter,
    endpoint: str,
    params: dict[str, Any],
    label: str,
    max_retries: int = 6,
) -> tuple[APIResult, dict[str, Any]]:
    backoff = 2.0
    for attempt in range(max_retries):
        limiter.acquire_token()
        res = await client.get(endpoint, params=params)
        limiter.update_from_headers(res.headers)
        env = res.data or {}
        errors = env.get("errors") or {}

        # API-Football may return 200 with errors.rateLimit
        if isinstance(errors, dict) and errors.get("rateLimit"):
            if attempt == max_retries - 1:
                raise RuntimeError(f"api_errors:{label}:{errors}")
            await asyncio.sleep(min(backoff, 30.0))
            backoff = min(backoff * 2.0, 30.0)
            continue
        if errors:
            raise RuntimeError(f"api_errors:{label}:{errors}")
        return res, env
    raise RuntimeError(f"api_errors:{label}:max_retries_exceeded")


def _ensure_progress_rows_for_tracked(*, league_ids: list[int], seasons: list[int]) -> int:
    """
    Insert missing (league_id, season, team_id) rows into core.team_statistics_progress.

    Team list source: core.fixtures (home/away team ids) scoped to tracked league_ids and seasons.
    This keeps the system FK-safe and avoids needing a separate "teams by league" table.
    """
    if not league_ids or not seasons:
        return 0

    sql = """
    WITH teams AS (
      SELECT league_id, season, home_team_id AS team_id
      FROM core.fixtures
      WHERE league_id = ANY(%s)
        AND season = ANY(%s)
        AND home_team_id IS NOT NULL
      UNION
      SELECT league_id, season, away_team_id AS team_id
      FROM core.fixtures
      WHERE league_id = ANY(%s)
        AND season = ANY(%s)
        AND away_team_id IS NOT NULL
    )
    SELECT DISTINCT league_id, season, team_id
    FROM teams
    """
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (league_ids, seasons, league_ids, seasons))
            rows = [(int(r[0]), int(r[1]), int(r[2])) for r in cur.fetchall() if r[1] is not None]

            if not rows:
                conn.commit()
                return 0

            psycopg2.extras.execute_values(
                cur,
                """
                INSERT INTO core.team_statistics_progress (league_id, season, team_id)
                VALUES %s
                ON CONFLICT (league_id, season, team_id) DO NOTHING
                """,
                rows,
            )
        conn.commit()
    return len(rows)


def _pick_due_tasks(*, league_ids: list[int], seasons: list[int], limit: int, refresh_hours: int) -> list[tuple[int, int, int]]:
    """
    Pick due (league_id, season, team_id) tasks to fetch.
    A task is due when last_fetched_at is NULL or older than refresh_hours.
    """
    sql = f"""
    SELECT league_id, season, team_id
    FROM core.team_statistics_progress
    WHERE league_id = ANY(%s)
      AND season = ANY(%s)
      AND (last_fetched_at IS NULL OR last_fetched_at < NOW() - make_interval(hours => %s))
    ORDER BY last_fetched_at ASC NULLS FIRST, league_id ASC, season ASC, team_id ASC
    LIMIT %s
    """
    out: list[tuple[int, int, int]] = []
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (league_ids, seasons, int(refresh_hours), int(limit)))
            for lid, s, tid in cur.fetchall():
                out.append((int(lid), int(s), int(tid)))
        conn.commit()
    return out


def _update_progress(*, league_id: int, season: int, team_id: int, ok: bool, err: str | None = None) -> None:
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            if ok:
                cur.execute(
                    """
                    UPDATE core.team_statistics_progress
                    SET last_fetched_at = NOW(), last_error = NULL
                    WHERE league_id=%s AND season=%s AND team_id=%s
                    """,
                    (int(league_id), int(season), int(team_id)),
                )
            else:
                cur.execute(
                    """
                    UPDATE core.team_statistics_progress
                    SET last_error = %s
                    WHERE league_id=%s AND season=%s AND team_id=%s
                    """,
                    (str(err or "unknown_error"), int(league_id), int(season), int(team_id)),
                )
        conn.commit()


async def run_team_statistics_refresh(*, client: APIClient, limiter: RateLimiter, config_path: Path) -> None:
    """
    Distributed team statistics refresh:
    - Discover teams per tracked (league_id, season) from core.fixtures
    - Insert missing progress rows
    - Pick N due teams and call:
        GET /teams/statistics?league=<id>&season=<season>&team=<team_id>
    - RAW archive always
    - CORE upsert into core.team_statistics
    - Update MART coverage per league/season
    """
    leagues = _load_tracked_leagues(config_path)
    league_ids = sorted({int(x["id"]) for x in leagues})
    seasons = sorted({int(x["season"]) for x in leagues})

    refresh_hours = int(os.getenv("TEAM_STATS_REFRESH_HOURS", "24"))
    max_tasks = int(os.getenv("TEAM_STATS_MAX_TASKS_PER_RUN", "50"))

    inserted = _ensure_progress_rows_for_tracked(league_ids=league_ids, seasons=seasons)
    tasks = _pick_due_tasks(league_ids=league_ids, seasons=seasons, limit=max_tasks, refresh_hours=refresh_hours)
    if not tasks:
        logger.info("team_statistics_no_work", progress_rows_inserted=inserted)
        return

    logger.info(
        "team_statistics_run_start",
        tasks=len(tasks),
        progress_rows_inserted=inserted,
        refresh_hours=refresh_hours,
        max_tasks=max_tasks,
    )

    total_rows = 0
    api_requests = 0

    for league_id, season, team_id in tasks:
        params = {"league": int(league_id), "season": int(season), "team": int(team_id)}
        label = f"/teams/statistics(league={league_id},season={season},team={team_id})"

        try:
            res, env = await _safe_get_envelope(
                client=client,
                limiter=limiter,
                endpoint="/teams/statistics",
                params=params,
                label=label,
            )
            api_requests += 1
        except EmergencyStopError as e:
            logger.error("emergency_stop_daily_quota_low", job="team_statistics_refresh", err=str(e))
            break
        except RateLimitError as e:
            logger.warning("api_rate_limited_429", league_id=league_id, season=season, team_id=team_id, err=str(e), sleep_seconds=5)
            await asyncio.sleep(5)
            continue
        except APIClientError as e:
            logger.error("api_call_failed", league_id=league_id, season=season, team_id=team_id, err=str(e))
            _update_progress(league_id=league_id, season=season, team_id=team_id, ok=False, err=str(e))
            continue
        except Exception as e:
            logger.error("team_statistics_fetch_failed", league_id=league_id, season=season, team_id=team_id, err=str(e))
            _update_progress(league_id=league_id, season=season, team_id=team_id, ok=False, err=str(e))
            continue

        upsert_raw(
            endpoint="/teams/statistics",
            requested_params=params,
            status_code=res.status_code,
            response_headers=res.headers,
            body=env,
        )

        row = transform_team_statistics(envelope=env, league_id=league_id, season=season, team_id=team_id)
        if row:
            upsert_core(
                full_table_name="core.team_statistics",
                rows=[row],
                conflict_cols=["league_id", "season", "team_id"],
                update_cols=["form", "raw"],
            )
            total_rows += 1

        _update_progress(league_id=league_id, season=season, team_id=team_id, ok=True)

        # Coverage
        try:
            from src.coverage.calculator import CoverageCalculator

            calc = CoverageCalculator()
            cov = calc.calculate_team_statistics_coverage(league_id=league_id, season=season)
            upsert_mart_coverage(coverage_data=cov)
        except Exception as e:
            logger.warning("team_statistics_coverage_update_failed", league_id=league_id, season=season, err=str(e))

    logger.info(
        "team_statistics_run_complete",
        tasks=len(tasks),
        api_requests=api_requests,
        core_rows=total_rows,
        daily_remaining=limiter.quota.daily_remaining,
        minute_remaining=limiter.quota.minute_remaining,
    )


