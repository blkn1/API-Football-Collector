from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg2.extras
import yaml

from collector.api_client import APIClient, APIClientError, APIResult, RateLimitError
from collector.rate_limiter import RateLimiter
from transforms.standings import transform_standings
from utils.db import get_transaction, query_scalar, upsert_raw
from utils.logging import get_logger
from utils.dependencies import ensure_standings_dependencies


logger = get_logger(component="standings_sync")


@dataclass(frozen=True)
class TrackedLeague:
    id: int
    name: str | None = None


@dataclass(frozen=True)
class StandingsSyncSummary:
    leagues: list[int]
    api_requests: int
    total_rows: int
    daily_remaining: int | None
    minute_remaining: int | None


def _load_config(config_path: Path) -> tuple[int, list[TrackedLeague]]:
    cfg = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    season = cfg.get("season")
    tracked_leagues = cfg.get("tracked_leagues")

    leagues: list[TrackedLeague] = []
    if isinstance(tracked_leagues, list):
        for x in tracked_leagues:
            if not isinstance(x, dict) or "id" not in x:
                continue
            leagues.append(TrackedLeague(id=int(x["id"]), name=x.get("name")))

    # fallback: infer from jobs config
    if not leagues:
        jobs = cfg.get("jobs") or []
        for j in jobs:
            if not isinstance(j, dict):
                continue
            if j.get("endpoint") != "/standings":
                continue
            params = j.get("params") or {}
            league_id = params.get("league")
            job_season = params.get("season")
            if league_id is None:
                continue
            leagues.append(TrackedLeague(id=int(league_id), name=j.get("job_id")))
            if season is None and job_season is not None:
                season = int(job_season)

    if season is None:
        raise ValueError(f"Missing season in config: {config_path}")
    if not leagues:
        raise ValueError(f"No tracked leagues configured in {config_path}")

    return int(season), leagues


def _replace_standings(conn, *, league_id: int, season: int, rows: list[dict[str, Any]]) -> None:
    # Delete then insert inside one transaction
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM core.standings WHERE league_id = %s AND season = %s",
            (league_id, season),
        )
        if not rows:
            return

        cols = list(rows[0].keys())
        values_raw = [tuple(r[c] for c in cols) for r in rows]
        # Adapt JSONB dict/list for psycopg2
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


async def sync_standings(
    *,
    league_filter: int | None = None,
    dry_run: bool = False,
    config_path: Path,
    client: APIClient | None = None,
    limiter: RateLimiter | None = None,
) -> StandingsSyncSummary:
    """
    Sync standings for tracked leagues from config:
      GET /standings?league={id}&season={season}

    - Rate limit enforced per call
    - RAW archived (unless dry-run)
    - CORE standings replaced fully per league+season (delete-then-insert in one transaction)
    """
    season, tracked = _load_config(config_path)

    leagues = tracked
    if league_filter is not None:
        leagues = [x for x in leagues if x.id == int(league_filter)]
        if not leagues:
            raise ValueError(f"League {league_filter} not found in config: {config_path}")

    limiter2 = limiter or RateLimiter(max_tokens=300, refill_rate=5.0)
    client2 = client or APIClient()

    api_requests = 0
    total_rows = 0

    logger.info("standings_sync_started", season=season, dry_run=dry_run, leagues=[l.id for l in leagues])

    try:
        for l in leagues:
            league_id = l.id
            league_name = l.name or f"League {league_id}"
            params = {"league": league_id, "season": season}
            logger.info("league_standings_started", league_id=league_id, league_name=league_name, season=season)

            try:
                limiter2.acquire_token()
                result: APIResult = await client2.get("/standings", params=params)
                api_requests += 1
                limiter2.update_from_headers(result.headers)
            except RateLimitError as e:
                logger.warning("api_rate_limited", league_id=league_id, err=str(e), sleep_seconds=5)
                await asyncio.sleep(5)
                continue
            except APIClientError as e:
                logger.error("api_call_failed", league_id=league_id, err=str(e))
                continue
            except Exception as e:
                logger.error("api_call_unexpected_error", league_id=league_id, err=str(e))
                continue

            envelope = result.data or {}

            # Ensure league+teams exist before FK writes into core.standings.
            try:
                await ensure_standings_dependencies(
                    league_id=league_id,
                    season=season,
                    standings_envelope=envelope,
                    client=client2,
                    limiter=limiter2,
                )
            except Exception as e:
                logger.error("dependency_bootstrap_failed", league_id=league_id, err=str(e))
                continue

            if not dry_run:
                upsert_raw(
                    endpoint="/standings",
                    requested_params=params,
                    status_code=result.status_code,
                    response_headers=result.headers,
                    body=envelope,
                )
                logger.info("raw_stored", league_id=league_id)

            rows = transform_standings(envelope)
            total_rows += len(rows)

            if dry_run:
                logger.info("core_skipped_dry_run", league_id=league_id, rows=len(rows))
                continue

            try:
                with get_transaction() as conn:
                    _replace_standings(conn, league_id=league_id, season=season, rows=rows)
                count = query_scalar(
                    "SELECT COUNT(*) FROM core.standings WHERE league_id=%s AND season=%s",
                    (league_id, season),
                )
                logger.info("core_replaced", league_id=league_id, rows_inserted=len(rows), rows_in_db=int(count or 0))
            except Exception as e:
                logger.error("db_replace_failed", league_id=league_id, err=str(e))
                continue

            q = limiter2.quota
            logger.info("quota_snapshot", league_id=league_id, daily_remaining=q.daily_remaining, minute_remaining=q.minute_remaining)

        q = limiter2.quota
        logger.info(
            "standings_sync_complete",
            api_requests=api_requests,
            total_rows=total_rows,
            daily_remaining=q.daily_remaining,
            minute_remaining=q.minute_remaining,
        )
        return StandingsSyncSummary(
            leagues=[l.id for l in leagues],
            api_requests=api_requests,
            total_rows=total_rows,
            daily_remaining=q.daily_remaining,
            minute_remaining=q.minute_remaining,
        )
    finally:
        if client is None:
            await client2.aclose()


