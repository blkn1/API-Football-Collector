from __future__ import annotations

from typing import Any

from collector.api_client import APIClient, APIResult
from collector.rate_limiter import RateLimiter
from transforms.leagues import transform_leagues
from transforms.teams import transform_teams
from transforms.venues import transform_venues_from_teams
from utils.db import query_scalar, upsert_core, upsert_raw
from utils.logging import get_logger


logger = get_logger(component="dependencies")


def _extract_team_ids_from_fixtures_envelope(envelope: dict[str, Any]) -> set[int]:
    ids: set[int] = set()
    for item in envelope.get("response") or []:
        teams = item.get("teams") or {}
        for side in ("home", "away"):
            tid = (teams.get(side) or {}).get("id")
            try:
                if tid is not None:
                    ids.add(int(tid))
            except Exception:
                continue
    return ids


def _extract_team_ids_from_standings_envelope(envelope: dict[str, Any]) -> set[int]:
    ids: set[int] = set()
    for item in envelope.get("response") or []:
        league = item.get("league") or {}
        standings = league.get("standings") or []
        # standings is usually [ [ {team: {id,...}, ...}, ... ] ]
        for group in standings:
            if not isinstance(group, list):
                continue
            for row in group:
                team = (row or {}).get("team") or {}
                tid = team.get("id")
                try:
                    if tid is not None:
                        ids.add(int(tid))
                except Exception:
                    continue
    return ids


async def _fetch_and_store(
    *,
    client: APIClient,
    limiter: RateLimiter,
    endpoint: str,
    params: dict[str, Any],
) -> APIResult:
    limiter.acquire_token()
    result: APIResult = await client.get(endpoint, params=params)
    limiter.update_from_headers(result.headers)
    upsert_raw(
        endpoint=endpoint,
        requested_params=params,
        status_code=result.status_code,
        response_headers=result.headers,
        body=result.data or {},
    )
    return result


async def ensure_league_exists(*, league_id: int, season: int | None, client: APIClient, limiter: RateLimiter) -> None:
    exists = query_scalar("SELECT 1 FROM core.leagues WHERE id=%s", (int(league_id),))
    if exists:
        return

    # Use the smallest safe call: /leagues?id=... (optionally season scoped)
    params: dict[str, Any] = {"id": int(league_id)}
    if season is not None and int(season) > 0:
        params["season"] = int(season)
    res = await _fetch_and_store(client=client, limiter=limiter, endpoint="/leagues", params=params)
    env = res.data or {}
    if env.get("errors"):
        raise RuntimeError(f"api_errors:/leagues:{env.get('errors')}")

    rows = transform_leagues(env, tracked_league_ids={int(league_id)})
    if not rows:
        raise RuntimeError(f"league_not_found:league_id={league_id} season={season}")

    upsert_core(
        full_table_name="core.leagues",
        rows=rows,
        conflict_cols=["id"],
        update_cols=["name", "type", "logo", "country_name", "country_code", "country_flag", "seasons"],
    )
    logger.info("league_upserted_dependency", league_id=int(league_id), season=int(season))


async def ensure_teams_exist_for_league(
    *,
    league_id: int,
    season: int,
    team_ids: set[int],
    client: APIClient,
    limiter: RateLimiter,
) -> None:
    if not team_ids:
        return

    existing = int(query_scalar("SELECT COUNT(*) FROM core.teams WHERE id = ANY(%s)", (list(team_ids),)) or 0)
    if existing == len(team_ids):
        return

    params = {"league": int(league_id), "season": int(season)}
    res = await _fetch_and_store(client=client, limiter=limiter, endpoint="/teams", params=params)
    env = res.data or {}
    if env.get("errors"):
        raise RuntimeError(f"api_errors:/teams:{env.get('errors')}")

    venue_rows = transform_venues_from_teams(env)
    if venue_rows:
        upsert_core(
            full_table_name="core.venues",
            rows=venue_rows,
            conflict_cols=["id"],
            update_cols=["name", "address", "city", "country", "capacity", "surface", "image"],
        )

    team_rows = transform_teams(env)
    if team_rows:
        upsert_core(
            full_table_name="core.teams",
            rows=team_rows,
            conflict_cols=["id"],
            update_cols=["name", "code", "country", "founded", "national", "logo", "venue_id"],
        )

    logger.info(
        "teams_upserted_dependency",
        league_id=int(league_id),
        season=int(season),
        requested_team_ids=len(team_ids),
        teams_upserted=len(team_rows),
    )


async def ensure_fixtures_dependencies(
    *,
    league_id: int,
    season: int | None,
    fixtures_envelope: dict[str, Any],
    client: APIClient,
    limiter: RateLimiter,
) -> None:
    await ensure_league_exists(league_id=league_id, season=season, client=client, limiter=limiter)
    team_ids = _extract_team_ids_from_fixtures_envelope(fixtures_envelope)
    if season is None:
        raise RuntimeError("season_required_for_teams_bootstrap")
    await ensure_teams_exist_for_league(
        league_id=league_id,
        season=season,
        team_ids=team_ids,
        client=client,
        limiter=limiter,
    )


async def ensure_standings_dependencies(
    *,
    league_id: int,
    season: int,
    standings_envelope: dict[str, Any],
    client: APIClient,
    limiter: RateLimiter,
) -> None:
    await ensure_league_exists(league_id=league_id, season=season, client=client, limiter=limiter)
    team_ids = _extract_team_ids_from_standings_envelope(standings_envelope)
    await ensure_teams_exist_for_league(
        league_id=league_id,
        season=season,
        team_ids=team_ids,
        client=client,
        limiter=limiter,
    )


