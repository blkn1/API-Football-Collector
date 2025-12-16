from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

import psycopg2.extras
import yaml

from src.collector.api_client import APIClient, APIClientError, APIResult, RateLimitError
from src.collector.rate_limiter import EmergencyStopError, RateLimiter
from src.transforms.fixtures import transform_fixtures
from src.transforms.standings import transform_standings
from src.utils.db import get_db_connection, get_transaction, query_scalar, upsert_core, upsert_raw
from src.utils.logging import get_logger
from src.utils.dependencies import ensure_fixtures_dependencies, ensure_league_exists, ensure_standings_dependencies


logger = get_logger(component="jobs_backfill")


FIXTURES_JOB_ID = "fixtures_backfill_league_season"
STANDINGS_JOB_ID = "standings_backfill_league_season"


@dataclass(frozen=True)
class TrackedLeague:
    id: int
    name: str | None = None
    season: int | None = None


def _load_tracked_leagues(config_path: Path) -> list[TrackedLeague]:
    cfg = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    default_season = cfg.get("season")
    tracked = cfg.get("tracked_leagues") or []
    if not isinstance(tracked, list) or not tracked:
        raise ValueError(f"Missing tracked_leagues in {config_path}")
    out: list[TrackedLeague] = []
    for x in tracked:
        if not isinstance(x, dict) or x.get("id") is None:
            continue
        s = x.get("season")
        if s is None:
            s = default_season
        out.append(TrackedLeague(id=int(x["id"]), name=x.get("name"), season=(int(s) if s is not None else None)))
    if not out:
        raise ValueError(f"No valid tracked_leagues items in {config_path}")
    missing = [l.id for l in out if l.season is None]
    if missing:
        raise ValueError(f"Missing season for leagues={missing} in {config_path}")
    return out


def _load_backfill_seasons(config_path: Path) -> list[int] | None:
    cfg = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    backfill = cfg.get("backfill") or {}
    seasons = backfill.get("seasons")
    if not isinstance(seasons, list) or not seasons:
        return None
    out: list[int] = []
    for s in seasons:
        try:
            out.append(int(s))
        except Exception:
            continue
    if not out:
        return None
    return out


def _derive_backfill_pairs(*, config_path: Path, leagues: list[TrackedLeague]) -> list[tuple[int, int]]:
    """
    SeçenekB:
    - If backfill.seasons exists -> treat as an explicit override (cross-product with all leagues).
    - Else derive per-league seasons as [current, current-1] where current = tracked_leagues[].season.
    """
    override_seasons = _load_backfill_seasons(config_path)
    pairs: set[tuple[int, int]] = set()
    if override_seasons:
        for l in leagues:
            for s in override_seasons:
                pairs.add((int(l.id), int(s)))
    else:
        for l in leagues:
            cur = int(l.season or 0)
            if cur <= 0:
                continue
            pairs.add((int(l.id), cur))
            if cur - 1 > 0:
                pairs.add((int(l.id), cur - 1))
    return sorted(pairs, key=lambda x: (x[0], x[1]))


def _parse_iso_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            return date.fromisoformat(s)
        except Exception:
            return None
    return None


def _get_league_season_date_range(*, league_id: int, season: int) -> tuple[date, date] | None:
    """
    Get season start/end from core.leagues.seasons JSONB (sourced from /leagues).
    Expected shape:
      seasons: [{year: 2024, start: "2024-08-16", end: "2025-05-18", ...}, ...]
    """
    seasons = query_scalar("SELECT seasons FROM core.leagues WHERE id=%s", (int(league_id),))
    if not isinstance(seasons, list):
        return None
    for item in seasons:
        if not isinstance(item, dict):
            continue
        try:
            year = int(item.get("year")) if item.get("year") is not None else None
        except Exception:
            year = None
        if year != int(season):
            continue
        start = _parse_iso_date(item.get("start"))
        end = _parse_iso_date(item.get("end"))
        if start and end:
            return (start, end)
    return None


def _window_bounds(*, start: date, end: date, window_days: int, index_1_based: int) -> tuple[date, date] | None:
    """
    Deterministic windowing for resume:
      idx=1 => [start, start+window_days-1]
      idx=2 => [start+window_days, start+2*window_days-1]
    """
    if window_days <= 0:
        raise ValueError("window_days must be > 0")
    if index_1_based <= 0:
        raise ValueError("index_1_based must be >= 1")
    offset_days = (index_1_based - 1) * window_days
    w_start = start + timedelta(days=offset_days)
    if w_start > end:
        return None
    w_end = min(end, w_start + timedelta(days=window_days - 1))
    return (w_start, w_end)


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


def _ensure_progress_rows(job_id: str, pairs: list[tuple[int, int]]) -> None:
    """
    Create missing progress rows so backfill can resume deterministically.
    """
    if not pairs:
        return
    # Insert all combos; ON CONFLICT do nothing.
    rows: list[tuple[Any, ...]] = []
    for lid, s in pairs:
        rows.append((job_id, int(lid), int(s), 1, False))
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            psycopg2.extras.execute_values(
                cur,
                """
                INSERT INTO core.backfill_progress (job_id, league_id, season, next_page, completed)
                VALUES %s
                ON CONFLICT (job_id, league_id, season) DO NOTHING
                """,
                rows,
            )
        conn.commit()


def _pick_progress_tasks(job_id: str, limit: int) -> list[tuple[int, int, int]]:
    """
    Returns list of (league_id, season, next_page) for incomplete tasks.
    """
    sql = """
    SELECT league_id, season, next_page
    FROM core.backfill_progress
    WHERE job_id = %s AND completed = FALSE
    ORDER BY updated_at ASC NULLS FIRST, league_id ASC, season ASC
    LIMIT %s
    """
    out: list[tuple[int, int, int]] = []
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (str(job_id), int(limit)))
            for lid, season, next_page in cur.fetchall():
                out.append((int(lid), int(season), int(next_page)))
        conn.commit()
    return out


def _update_progress(
    *,
    job_id: str,
    league_id: int,
    season: int,
    next_page: int | None = None,
    completed: bool | None = None,
    last_error: str | None = None,
) -> None:
    sets: list[str] = ["last_run_at = NOW()", "updated_at = NOW()"]
    params: list[Any] = []
    if next_page is not None:
        sets.append("next_page = %s")
        params.append(int(next_page))
    if completed is not None:
        sets.append("completed = %s")
        params.append(bool(completed))
    if last_error is not None:
        sets.append("last_error = %s")
        params.append(str(last_error))
    else:
        # clear error on success path
        sets.append("last_error = NULL")
    params.extend([str(job_id), int(league_id), int(season)])
    sql = f"UPDATE core.backfill_progress SET {', '.join(sets)} WHERE job_id=%s AND league_id=%s AND season=%s"
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params))
        conn.commit()


async def run_fixtures_backfill_league_season(
    *,
    client: APIClient,
    limiter: RateLimiter,
    config_path: Path,
) -> None:
    """
    Resumeable fixtures backfill (NO 'page' param on /fixtures):
    - Prefer: GET /fixtures?league=<id>&season=<season>&from=YYYY-MM-DD&to=YYYY-MM-DD (windowed, resumeable)
    - Fallback: GET /fixtures?league=<id>&season=<season> (single call) if season date range is unknown
    - Store RAW per request
    - UPSERT core.fixtures (+ optional core.fixture_details JSONB)
    - Update core.backfill_progress.next_page / completed (next_page used as window index)
    """
    leagues = _load_tracked_leagues(config_path)
    pairs = _derive_backfill_pairs(config_path=config_path, leagues=leagues)

    # Defaults tuned for 75k/day quota while staying below 300/min hard cap (shared rate limiter).
    # Override via Coolify env if you want slower/faster.
    max_tasks = int(os.getenv("BACKFILL_FIXTURES_MAX_TASKS_PER_RUN", "6"))
    max_windows_per_task = int(os.getenv("BACKFILL_FIXTURES_MAX_PAGES_PER_TASK", "6"))
    window_days = int(os.getenv("BACKFILL_FIXTURES_WINDOW_DAYS", "30"))

    _ensure_progress_rows(FIXTURES_JOB_ID, pairs)
    tasks = _pick_progress_tasks(FIXTURES_JOB_ID, max_tasks)
    if not tasks:
        logger.info("fixtures_backfill_no_work")
        return

    logger.info(
        "fixtures_backfill_run_start",
        tasks=len(tasks),
        max_windows_per_task=max_windows_per_task,
        window_days=window_days,
    )

    for league_id, season, next_page in tasks:
        logger.info("fixtures_backfill_task_start", league_id=league_id, season=season, next_page=next_page)
        window_index = int(next_page)  # stored in core.backfill_progress.next_page
        windows_done = 0
        try:
            # Ensure league exists so seasons metadata is available for deterministic windowing.
            try:
                await ensure_league_exists(league_id=league_id, season=season, client=client, limiter=limiter)
            except Exception as e:
                # best effort; fallback path may still succeed
                logger.warning(
                    "fixtures_backfill_ensure_league_failed",
                    league_id=league_id,
                    season=season,
                    err=str(e),
                )

            rng = _get_league_season_date_range(league_id=league_id, season=season)
            if rng is None:
                # Bug 1 fix: fallback path MUST be fully error-handled and persist last_error.
                label = f"/fixtures(league={league_id},season={season})"
                params = {"league": int(league_id), "season": int(season)}

                try:
                    res, env = await _safe_get_envelope(
                        client=client, limiter=limiter, endpoint="/fixtures", params=params, label=label
                    )
                except EmergencyStopError:
                    raise
                except RateLimitError:
                    logger.warning("fixtures_backfill_429", league_id=league_id, season=season, sleep_seconds=5)
                    await asyncio.sleep(5)
                    continue
                except (APIClientError, RuntimeError) as e:
                    logger.error("fixtures_backfill_api_failed", league_id=league_id, season=season, err=str(e))
                    _update_progress(job_id=FIXTURES_JOB_ID, league_id=league_id, season=season, last_error=str(e))
                    continue
                except Exception as e:
                    logger.error("fixtures_backfill_unexpected_error", league_id=league_id, season=season, err=str(e))
                    _update_progress(job_id=FIXTURES_JOB_ID, league_id=league_id, season=season, last_error=str(e))
                    continue

                upsert_raw(
                    endpoint="/fixtures",
                    requested_params=params,
                    status_code=res.status_code,
                    response_headers=res.headers,
                    body=env,
                )

                # Dependencies (league+teams must exist before fixture FK inserts)
                try:
                    await ensure_fixtures_dependencies(
                        league_id=league_id,
                        season=season,
                        fixtures_envelope=env,
                        client=client,
                        limiter=limiter,
                    )
                except Exception as e:
                    logger.error("fixtures_backfill_dependency_failed", league_id=league_id, season=season, err=str(e))
                    _update_progress(job_id=FIXTURES_JOB_ID, league_id=league_id, season=season, last_error=str(e))
                    continue

                fixtures_rows, details_rows = transform_fixtures(env)
                fixture_ids = [int(r["id"]) for r in fixtures_rows]
                details_ids = [int(r["fixture_id"]) for r in details_rows]

                # Upsert atomically
                try:
                    with get_transaction() as conn:
                        upsert_core(
                            full_table_name="core.fixtures",
                            rows=fixtures_rows,
                            conflict_cols=["id"],
                            update_cols=[
                                "league_id",
                                "season",
                                "round",
                                "date",
                                "api_timestamp",
                                "referee",
                                "timezone",
                                "venue_id",
                                "home_team_id",
                                "away_team_id",
                                "status_short",
                                "status_long",
                                "elapsed",
                                "goals_home",
                                "goals_away",
                                "score",
                            ],
                            conn=conn,
                        )
                        if details_rows:
                            upsert_core(
                                full_table_name="core.fixture_details",
                                rows=details_rows,
                                conflict_cols=["fixture_id"],
                                update_cols=["events", "lineups", "statistics", "players"],
                                conn=conn,
                            )
                    _update_progress(job_id=FIXTURES_JOB_ID, league_id=league_id, season=season, completed=True, last_error=None)
                    logger.info(
                        "fixtures_backfill_core_upserted",
                        league_id=league_id,
                        season=season,
                        mode="unbounded",
                        fixtures=len(fixture_ids),
                        details=len(details_ids),
                    )
                except Exception as e:
                    logger.error("fixtures_backfill_db_failed", league_id=league_id, season=season, err=str(e))
                    _update_progress(job_id=FIXTURES_JOB_ID, league_id=league_id, season=season, last_error=str(e))
                    continue

                continue

            season_start, season_end = rng

            while windows_done < max_windows_per_task:
                bounds = _window_bounds(
                    start=season_start,
                    end=season_end,
                    window_days=window_days,
                    index_1_based=window_index,
                )
                if bounds is None:
                    _update_progress(job_id=FIXTURES_JOB_ID, league_id=league_id, season=season, completed=True, last_error=None)
                    logger.info("fixtures_backfill_completed_all_windows", league_id=league_id, season=season)
                    break

                w_start, w_end = bounds
                label = (
                    f"/fixtures(league={league_id},season={season},from={w_start.isoformat()},to={w_end.isoformat()})"
                )
                params = {
                    "league": int(league_id),
                    "season": int(season),
                    "from": w_start.isoformat(),
                    "to": w_end.isoformat(),
                }

                try:
                    res, env = await _safe_get_envelope(
                        client=client, limiter=limiter, endpoint="/fixtures", params=params, label=label
                    )
                except EmergencyStopError:
                    raise
                except RateLimitError:
                    logger.warning(
                        "fixtures_backfill_429",
                        league_id=league_id,
                        season=season,
                        window_index=window_index,
                        sleep_seconds=5,
                    )
                    await asyncio.sleep(5)
                    continue
                except (APIClientError, RuntimeError) as e:
                    logger.error(
                        "fixtures_backfill_api_failed",
                        league_id=league_id,
                        season=season,
                        window_index=window_index,
                        err=str(e),
                    )
                    _update_progress(job_id=FIXTURES_JOB_ID, league_id=league_id, season=season, last_error=str(e))
                    break
                except Exception as e:
                    logger.error(
                        "fixtures_backfill_unexpected_error",
                        league_id=league_id,
                        season=season,
                        window_index=window_index,
                        err=str(e),
                    )
                    _update_progress(job_id=FIXTURES_JOB_ID, league_id=league_id, season=season, last_error=str(e))
                    break

                upsert_raw(
                    endpoint="/fixtures",
                    requested_params=params,
                    status_code=res.status_code,
                    response_headers=res.headers,
                    body=env,
                )

                try:
                    await ensure_fixtures_dependencies(
                        league_id=league_id,
                        season=season,
                        fixtures_envelope=env,
                        client=client,
                        limiter=limiter,
                        log_venues=(windows_done == 0),
                    )
                except Exception as e:
                    logger.error("fixtures_backfill_dependency_failed", league_id=league_id, season=season, err=str(e))
                    _update_progress(job_id=FIXTURES_JOB_ID, league_id=league_id, season=season, last_error=str(e))
                    break

                fixtures_rows, details_rows = transform_fixtures(env)
                fixture_ids = [int(r["id"]) for r in fixtures_rows]
                details_ids = [int(r["fixture_id"]) for r in details_rows]

                try:
                    with get_transaction() as conn:
                        upsert_core(
                            full_table_name="core.fixtures",
                            rows=fixtures_rows,
                            conflict_cols=["id"],
                            update_cols=[
                                "league_id",
                                "season",
                                "round",
                                "date",
                                "api_timestamp",
                                "referee",
                                "timezone",
                                "venue_id",
                                "home_team_id",
                                "away_team_id",
                                "status_short",
                                "status_long",
                                "elapsed",
                                "goals_home",
                                "goals_away",
                                "score",
                            ],
                            conn=conn,
                        )
                        if details_rows:
                            upsert_core(
                                full_table_name="core.fixture_details",
                                rows=details_rows,
                                conflict_cols=["fixture_id"],
                                update_cols=["events", "lineups", "statistics", "players"],
                                conn=conn,
                            )

                    logger.info(
                        "fixtures_backfill_core_upserted",
                        league_id=league_id,
                        season=season,
                        window_index=window_index,
                        window_from=w_start.isoformat(),
                        window_to=w_end.isoformat(),
                        fixtures=len(fixture_ids),
                        details=len(details_ids),
                    )
                except Exception as e:
                    logger.error(
                        "fixtures_backfill_db_failed",
                        league_id=league_id,
                        season=season,
                        window_index=window_index,
                        err=str(e),
                    )
                    _update_progress(job_id=FIXTURES_JOB_ID, league_id=league_id, season=season, last_error=str(e))
                    break

                windows_done += 1
                window_index += 1
                _update_progress(job_id=FIXTURES_JOB_ID, league_id=league_id, season=season, next_page=window_index, last_error=None)

            # end while windows
        except EmergencyStopError as e:
            logger.error("fixtures_backfill_emergency_stop", league_id=league_id, season=season, err=str(e))
            break
        except Exception as e:
            logger.error("fixtures_backfill_task_unhandled_exception", league_id=league_id, season=season, err=str(e))
            _update_progress(job_id=FIXTURES_JOB_ID, league_id=league_id, season=season, last_error=str(e))
            continue

    q = limiter.quota
    logger.info(
        "fixtures_backfill_run_complete",
        tasks=len(tasks),
        daily_remaining=q.daily_remaining,
        minute_remaining=q.minute_remaining,
    )


async def run_standings_backfill_league_season(
    *,
    client: APIClient,
    limiter: RateLimiter,
    config_path: Path,
) -> None:
    """
    Resumeable standings backfill:
    - For each (league, season) in derived pairs:
      - backfill.seasons override => (tracked_leagues x seasons)
      - else => per-league [current, current-1] where current = tracked_leagues[].season
      - GET /standings?league=<id>&season=<season>
      - Store RAW
      - Replace core.standings per league+season (delete then insert in one transaction)
      - Mark completed
    """
    leagues = _load_tracked_leagues(config_path)
    pairs = _derive_backfill_pairs(config_path=config_path, leagues=leagues)

    # Standings is much cheaper than fixtures paging; keep it modest.
    max_tasks = int(os.getenv("BACKFILL_STANDINGS_MAX_TASKS_PER_RUN", "2"))

    _ensure_progress_rows(STANDINGS_JOB_ID, pairs)
    tasks = _pick_progress_tasks(STANDINGS_JOB_ID, max_tasks)
    if not tasks:
        logger.info("standings_backfill_no_work")
        return

    logger.info("standings_backfill_run_start", tasks=len(tasks))

    for league_id, season, _next_page in tasks:
        params = {"league": int(league_id), "season": int(season)}
        label = f"/standings(league={league_id},season={season})"
        try:
            try:
                res, env = await _safe_get_envelope(
                    client=client, limiter=limiter, endpoint="/standings", params=params, label=label
                )
            except EmergencyStopError:
                raise
            except RateLimitError:
                logger.warning("standings_backfill_429", league_id=league_id, season=season, sleep_seconds=5)
                await asyncio.sleep(5)
                continue
            except APIClientError as e:
                logger.error("standings_backfill_api_failed", league_id=league_id, season=season, err=str(e))
                _update_progress(job_id=STANDINGS_JOB_ID, league_id=league_id, season=season, last_error=str(e))
                continue

            upsert_raw(
                endpoint="/standings",
                requested_params=params,
                status_code=res.status_code,
                response_headers=res.headers,
                body=env,
            )

            # Ensure league+teams exist before FK writes
            try:
                await ensure_standings_dependencies(
                    league_id=league_id,
                    season=season,
                    standings_envelope=env,
                    client=client,
                    limiter=limiter,
                )
            except Exception as e:
                logger.error("standings_backfill_dependency_failed", league_id=league_id, season=season, err=str(e))
                _update_progress(job_id=STANDINGS_JOB_ID, league_id=league_id, season=season, last_error=str(e))
                continue

            rows = transform_standings(env)

            # Replace inside one transaction
            try:
                with get_transaction() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            "DELETE FROM core.standings WHERE league_id = %s AND season = %s",
                            (int(league_id), int(season)),
                        )
                        if rows:
                            cols = list(rows[0].keys())
                            values_raw = [tuple(r[c] for c in cols) for r in rows]
                            values: list[tuple[Any, ...]] = []
                            for row in values_raw:
                                adapted: list[Any] = []
                                for v in row:
                                    if isinstance(v, (dict, list)):
                                        adapted.append(psycopg2.extras.Json(v))
                                    else:
                                        adapted.append(v)
                                values.append(tuple(adapted))
                            insert_cols = ", ".join(cols)
                            stmt = f"INSERT INTO core.standings ({insert_cols}) VALUES %s"
                            psycopg2.extras.execute_values(cur, stmt, values)
                _update_progress(job_id=STANDINGS_JOB_ID, league_id=league_id, season=season, completed=True, last_error=None)
                logger.info("standings_backfill_completed", league_id=league_id, season=season, rows=len(rows))
            except Exception as e:
                logger.error("standings_backfill_db_failed", league_id=league_id, season=season, err=str(e))
                _update_progress(job_id=STANDINGS_JOB_ID, league_id=league_id, season=season, last_error=str(e))
                continue

        except EmergencyStopError as e:
            logger.error("standings_backfill_emergency_stop", league_id=league_id, season=season, err=str(e))
            break

    q = limiter.quota
    logger.info(
        "standings_backfill_run_complete",
        tasks=len(tasks),
        daily_remaining=q.daily_remaining,
        minute_remaining=q.minute_remaining,
    )
