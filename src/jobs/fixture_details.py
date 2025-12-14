from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

from src.collector.api_client import APIClient, APIClientError, APIResult, RateLimitError
from src.collector.rate_limiter import EmergencyStopError, RateLimiter
from src.transforms.fixture_endpoints import (
    transform_fixture_events,
    transform_fixture_lineups,
    transform_fixture_players,
    transform_fixture_statistics,
)
from src.utils.db import get_db_connection, upsert_core, upsert_mart_coverage, upsert_raw
from src.utils.logging import get_logger


logger = get_logger(component="jobs_fixture_details")


FINAL_STATUSES = {"FT", "AET", "PEN"}


@dataclass(frozen=True)
class FixtureWorkItem:
    fixture_id: int
    date_utc: datetime | None
    status_short: str | None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


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


def _select_backfill_fixtures(*, days: int, limit: int) -> list[FixtureWorkItem]:
    """
    Choose fixtures within the last N days that are completed and missing /fixtures/players RAW records.
    We use RAW existence as the work marker to avoid extra schema/state.
    """
    sql = """
    SELECT f.id, f.date, f.status_short
    FROM core.fixtures f
    WHERE f.date >= NOW() - (%s::text || ' days')::interval
      AND f.status_short = ANY(%s)
      AND NOT EXISTS (
        SELECT 1
        FROM raw.api_responses r
        WHERE r.endpoint = '/fixtures/players'
          AND (r.requested_params->>'fixture')::bigint = f.id
      )
    ORDER BY f.date ASC
    LIMIT %s
    """
    out: list[FixtureWorkItem] = []
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (int(days), list(FINAL_STATUSES), int(limit)))
            for fid, dt, st in cur.fetchall():
                out.append(FixtureWorkItem(fixture_id=int(fid), date_utc=dt, status_short=st))
        conn.commit()
    return out


def _select_recent_finalize_fixtures(*, hours: int, limit: int) -> list[FixtureWorkItem]:
    sql = """
    SELECT f.id, f.date, f.status_short
    FROM core.fixtures f
    WHERE f.date >= NOW() - (%s::text || ' hours')::interval
      AND f.status_short = ANY(%s)
      AND NOT EXISTS (
        SELECT 1
        FROM raw.api_responses r
        WHERE r.endpoint = '/fixtures/players'
          AND (r.requested_params->>'fixture')::bigint = f.id
      )
    ORDER BY f.date DESC
    LIMIT %s
    """
    out: list[FixtureWorkItem] = []
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (int(hours), list(FINAL_STATUSES), int(limit)))
            for fid, dt, st in cur.fetchall():
                out.append(FixtureWorkItem(fixture_id=int(fid), date_utc=dt, status_short=st))
        conn.commit()
    return out


def _select_today_lineups_window(*, lookback_hours: int, lookahead_hours: int, limit: int) -> list[FixtureWorkItem]:
    """
    Fetch lineups in a short window around kickoff:
    - from kickoff - lookback_hours
    - to   kickoff + lookahead_hours
    Only for non-final statuses, and only if we haven't stored RAW for /fixtures/lineups yet.
    """
    sql = """
    SELECT f.id, f.date, f.status_short
    FROM core.fixtures f
    WHERE f.date BETWEEN NOW() - (%s::text || ' hours')::interval AND NOW() + (%s::text || ' hours')::interval
      AND (f.status_short IS NULL OR f.status_short <> ALL(%s))
      AND NOT EXISTS (
        SELECT 1
        FROM raw.api_responses r
        WHERE r.endpoint = '/fixtures/lineups'
          AND (r.requested_params->>'fixture')::bigint = f.id
      )
    ORDER BY f.date ASC
    LIMIT %s
    """
    out: list[FixtureWorkItem] = []
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (int(lookback_hours), int(lookahead_hours), list(FINAL_STATUSES), int(limit)))
            for fid, dt, st in cur.fetchall():
                out.append(FixtureWorkItem(fixture_id=int(fid), date_utc=dt, status_short=st))
        conn.commit()
    return out


async def _fetch_and_store_fixture_details(*, client: APIClient, limiter: RateLimiter, fixture_id: int) -> None:
    """
    For a fixture, fetch and persist all four per-fixture endpoints:
    - /fixtures/players
    - /fixtures/events
    - /fixtures/statistics
    - /fixtures/lineups
    """
    endpoints = [
        ("/fixtures/players", transform_fixture_players, "core.fixture_players", ["fixture_id", "team_id", "player_id"], ["player_name", "statistics", "update_utc"]),
        ("/fixtures/events", transform_fixture_events, "core.fixture_events", ["fixture_id", "event_key"], ["time_elapsed", "time_extra", "team_id", "player_id", "assist_id", "type", "detail", "comments", "raw"]),
        ("/fixtures/statistics", transform_fixture_statistics, "core.fixture_statistics", ["fixture_id", "team_id"], ["statistics", "update_utc"]),
        ("/fixtures/lineups", transform_fixture_lineups, "core.fixture_lineups", ["fixture_id", "team_id"], ["formation", "start_xi", "substitutes", "coach", "colors"]),
    ]

    for endpoint, transform_fn, table, conflict_cols, update_cols in endpoints:
        params = {"fixture": int(fixture_id)}
        label = f"{endpoint}(fixture={fixture_id})"

        try:
            res, env = await _safe_get_envelope(
                client=client, limiter=limiter, endpoint=endpoint, params=params, label=label
            )
        except EmergencyStopError:
            raise
        except (RateLimitError, APIClientError) as e:
            logger.warning("fixture_detail_fetch_failed", fixture_id=fixture_id, endpoint=endpoint, err=str(e))
            # Do not spam retries per endpoint here; caller loop continues.
            return

        upsert_raw(
            endpoint=endpoint,
            requested_params=params,
            status_code=res.status_code,
            response_headers=res.headers,
            body=env,
        )

        rows = transform_fn(envelope=env, fixture_id=int(fixture_id))
        if rows:
            upsert_core(
                full_table_name=table,
                rows=rows,
                conflict_cols=conflict_cols,
                update_cols=update_cols,
            )


async def run_fixture_details_backfill_90d(*, client: APIClient, limiter: RateLimiter) -> None:
    """
    Rolling backfill for the last 90 days, completed fixtures only (quota-safe).
    Bounded by env:
      - FIXTURE_DETAILS_BACKFILL_BATCH (default 25 fixtures/run)
    """
    batch = int(os.getenv("FIXTURE_DETAILS_BACKFILL_BATCH", "25"))
    items = _select_backfill_fixtures(days=90, limit=batch)
    if not items:
        logger.info("fixture_details_backfill_no_work")
        return

    ok = 0
    processed: list[int] = []
    for it in items:
        try:
            await _fetch_and_store_fixture_details(client=client, limiter=limiter, fixture_id=it.fixture_id)
            ok += 1
            processed.append(int(it.fixture_id))
        except EmergencyStopError as e:
            logger.error("emergency_stop_daily_quota_low", job="fixture_details_backfill_90d", err=str(e))
            break

    # MART coverage for league/seasons touched
    _update_mart_coverage_for_fixtures(processed_fixture_ids=processed)

    logger.info(
        "fixture_details_backfill_90d_complete",
        fixtures_selected=len(items),
        fixtures_processed=ok,
        daily_remaining=limiter.quota.daily_remaining,
        minute_remaining=limiter.quota.minute_remaining,
    )


async def run_fixture_details_recent_finalize(*, client: APIClient, limiter: RateLimiter) -> None:
    """
    Operational job:
    - Finalize fixtures that completed in the last 24h (fetch all 4 endpoints once)
    - Fetch lineups for fixtures near kickoff (kickoff-2h..kickoff+1h) if not already fetched
    Bounded by env:
      - FIXTURE_DETAILS_FINALIZE_BATCH (default 50)
      - FIXTURE_LINEUPS_WINDOW_BATCH (default 50)
    """
    finalize_batch = int(os.getenv("FIXTURE_DETAILS_FINALIZE_BATCH", "50"))
    lineups_batch = int(os.getenv("FIXTURE_LINEUPS_WINDOW_BATCH", "50"))

    finalized = _select_recent_finalize_fixtures(hours=24, limit=finalize_batch)
    lineup_items = _select_today_lineups_window(lookback_hours=2, lookahead_hours=1, limit=lineups_batch)

    ok_finalize = 0
    processed_finalize: list[int] = []
    for it in finalized:
        try:
            await _fetch_and_store_fixture_details(client=client, limiter=limiter, fixture_id=it.fixture_id)
            ok_finalize += 1
            processed_finalize.append(int(it.fixture_id))
        except EmergencyStopError as e:
            logger.error("emergency_stop_daily_quota_low", job="fixture_details_recent_finalize", err=str(e))
            break

    ok_lineups = 0
    processed_lineups: list[int] = []
    for it in lineup_items:
        params = {"fixture": int(it.fixture_id)}
        label = f"/fixtures/lineups(fixture={it.fixture_id})"
        try:
            res, env = await _safe_get_envelope(client=client, limiter=limiter, endpoint="/fixtures/lineups", params=params, label=label)
        except EmergencyStopError as e:
            logger.error("emergency_stop_daily_quota_low", job="fixture_lineups_window", err=str(e))
            break
        except (RateLimitError, APIClientError) as e:
            logger.warning("lineups_fetch_failed", fixture_id=it.fixture_id, err=str(e))
            continue

        upsert_raw(
            endpoint="/fixtures/lineups",
            requested_params=params,
            status_code=res.status_code,
            response_headers=res.headers,
            body=env,
        )
        rows = transform_fixture_lineups(envelope=env, fixture_id=int(it.fixture_id))
        if rows:
            upsert_core(
                full_table_name="core.fixture_lineups",
                rows=rows,
                conflict_cols=["fixture_id", "team_id"],
                update_cols=["formation", "start_xi", "substitutes", "coach", "colors"],
            )
        ok_lineups += 1
        processed_lineups.append(int(it.fixture_id))

    _update_mart_coverage_for_fixtures(processed_fixture_ids=list(set(processed_finalize + processed_lineups)))

    logger.info(
        "fixture_details_recent_finalize_complete",
        finalize_selected=len(finalized),
        finalize_processed=ok_finalize,
        lineups_selected=len(lineup_items),
        lineups_processed=ok_lineups,
        daily_remaining=limiter.quota.daily_remaining,
        minute_remaining=limiter.quota.minute_remaining,
    )


def _update_mart_coverage_for_fixtures(*, processed_fixture_ids: list[int]) -> None:
    if not processed_fixture_ids:
        return
    league_seasons: list[tuple[int, int]] = []
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT league_id, season
                FROM core.fixtures
                WHERE id = ANY(%s)
                  AND league_id IS NOT NULL
                  AND season IS NOT NULL
                """,
                (processed_fixture_ids,),
            )
            for lid, season in cur.fetchall():
                try:
                    league_seasons.append((int(lid), int(season)))
                except Exception:
                    continue
        conn.commit()

    if not league_seasons:
        return

    try:
        from src.coverage.calculator import CoverageCalculator

        calc = CoverageCalculator()
    except Exception as e:
        logger.warning("coverage_calculator_import_failed", err=str(e))
        return

    endpoint_map = [
        ("/fixtures/players", "core.fixture_players"),
        ("/fixtures/events", "core.fixture_events"),
        ("/fixtures/statistics", "core.fixture_statistics"),
        ("/fixtures/lineups", "core.fixture_lineups"),
    ]

    for league_id, season in league_seasons:
        for endpoint, table in endpoint_map:
            try:
                cov = calc.calculate_fixture_endpoint_coverage(
                    league_id=league_id,
                    season=season,
                    endpoint=endpoint,
                    core_table=table,
                    days=90,
                )
                upsert_mart_coverage(coverage_data=cov)
            except Exception as e:
                logger.warning(
                    "fixture_endpoint_coverage_update_failed",
                    league_id=league_id,
                    season=season,
                    endpoint=endpoint,
                    err=str(e),
                )


