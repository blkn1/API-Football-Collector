from __future__ import annotations

from typing import Any

from src.collector.api_client import APIClient, APIResult
from src.collector.rate_limiter import RateLimiter
from src.transforms.countries import transform_countries
from src.transforms.leagues import transform_leagues
from src.transforms.teams import transform_teams
from src.transforms.timezones import transform_timezones
from src.transforms.venues import transform_venues_from_teams
from src.utils.db import upsert_core, upsert_raw
from src.utils.logging import get_logger


logger = get_logger(component="jobs_static_bootstrap")


async def _fetch_and_store(
    *,
    client: APIClient,
    limiter: RateLimiter,
    endpoint: str,
    params: dict[str, Any] | None = None,
) -> APIResult:
    limiter.acquire_token()
    result = await client.get(endpoint, params=params or {})
    limiter.update_from_headers(result.headers)
    upsert_raw(
        endpoint=endpoint,
        requested_params=params or {},
        status_code=result.status_code,
        response_headers=result.headers,
        body=result.data or {},
    )
    return result


async def run_bootstrap_countries(*, client: APIClient, limiter: RateLimiter) -> None:
    res = await _fetch_and_store(client=client, limiter=limiter, endpoint="/countries")
    rows = transform_countries(res.data or {})
    upsert_core(full_table_name="core.countries", rows=rows, conflict_cols=["code"], update_cols=["name", "flag"])
    logger.info("bootstrap_countries_complete", rows=len(rows))


async def run_bootstrap_timezones(*, client: APIClient, limiter: RateLimiter) -> None:
    res = await _fetch_and_store(client=client, limiter=limiter, endpoint="/timezone")
    rows = transform_timezones(res.data or {})
    upsert_core(full_table_name="core.timezones", rows=rows, conflict_cols=["name"], update_cols=["name"])
    logger.info("bootstrap_timezones_complete", rows=len(rows))


async def run_bootstrap_leagues(*, client: APIClient, limiter: RateLimiter, season: int, tracked_leagues: set[int]) -> None:
    res = await _fetch_and_store(client=client, limiter=limiter, endpoint="/leagues", params={"season": int(season)})
    rows = transform_leagues(res.data or {}, tracked_league_ids=tracked_leagues)
    upsert_core(
        full_table_name="core.leagues",
        rows=rows,
        conflict_cols=["id"],
        update_cols=["name", "type", "logo", "country_name", "country_code", "country_flag", "seasons"],
    )
    logger.info("bootstrap_leagues_complete", season=int(season), tracked_leagues=sorted(tracked_leagues), rows=len(rows))


async def run_bootstrap_teams(*, client: APIClient, limiter: RateLimiter, season: int, tracked_leagues: set[int]) -> None:
    total_teams = 0
    total_venues = 0
    for league_id in sorted(tracked_leagues):
        res = await _fetch_and_store(
            client=client,
            limiter=limiter,
            endpoint="/teams",
            params={"league": int(league_id), "season": int(season)},
        )
        teams_env = res.data or {}
        venue_rows = transform_venues_from_teams(teams_env)
        if venue_rows:
            upsert_core(
                full_table_name="core.venues",
                rows=venue_rows,
                conflict_cols=["id"],
                update_cols=["name", "address", "city", "country", "capacity", "surface", "image"],
            )
            total_venues += len(venue_rows)

        team_rows = transform_teams(teams_env)
        if team_rows:
            upsert_core(
                full_table_name="core.teams",
                rows=team_rows,
                conflict_cols=["id"],
                update_cols=["name", "code", "country", "founded", "national", "logo", "venue_id"],
            )
            total_teams += len(team_rows)

    logger.info(
        "bootstrap_teams_complete",
        season=int(season),
        tracked_leagues=sorted(tracked_leagues),
        teams_rows=total_teams,
        venues_rows=total_venues,
    )


