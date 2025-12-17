from __future__ import annotations

import json
from typing import Any

try:
    # scripts/ context (scripts add src/ to sys.path)
    from collector.api_client import APIClient, APIResult  # type: ignore
    from collector.rate_limiter import RateLimiter  # type: ignore
    from transforms.leagues import transform_leagues  # type: ignore
    from transforms.teams import transform_teams  # type: ignore
    from transforms.venues import transform_venues_from_teams  # type: ignore
    from utils.db import get_db_connection, query_scalar, upsert_core, upsert_raw  # type: ignore
    from utils.logging import get_logger  # type: ignore
except Exception:  # pragma: no cover
    # package context
    from src.collector.api_client import APIClient, APIResult
    from src.collector.rate_limiter import RateLimiter
    from src.transforms.leagues import transform_leagues
    from src.transforms.teams import transform_teams
    from src.transforms.venues import transform_venues_from_teams
    from src.utils.db import get_db_connection, query_scalar, upsert_core, upsert_raw
    from src.utils.logging import get_logger


logger = get_logger(component="dependencies")


def _coerce_json_list(value: Any) -> list[Any] | None:
    """
    core.leagues.seasons is JSONB. Depending on driver/typecasters, it may come back as:
    - list (decoded) or
    - str (JSON text)
    """
    if value is None:
        return None
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            obj = json.loads(s)
        except (json.JSONDecodeError, TypeError, ValueError):
            return None
        return obj if isinstance(obj, list) else None
    return None


def _league_has_season_metadata(seasons_value: Any, season: int) -> bool:
    seasons = _coerce_json_list(seasons_value)
    if not seasons:
        return False
    for item in seasons:
        if not isinstance(item, dict):
            continue
        try:
            year = int(item.get("year")) if item.get("year") is not None else None
        except Exception:
            year = None
        if year != int(season):
            continue
        # Prefer having start/end so backfill can window deterministically.
        if item.get("start") and item.get("end"):
            return True
        # Even without dates, presence of the year is still useful (but we keep it strict here).
    return False


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


def get_missing_team_ids_in_core(team_ids: set[int]) -> set[int]:
    """
    Return team IDs that are NOT present in core.teams.
    This is used as a safety check before writing FK-constrained tables (e.g. core.standings).
    """
    if not team_ids:
        return set()

    ids = sorted({int(x) for x in team_ids if x is not None})
    if not ids:
        return set()

    placeholders = ", ".join(["%s"] * len(ids))
    q = f"SELECT id FROM core.teams WHERE id IN ({placeholders})"

    existing: set[int] = set()
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(q, tuple(ids))
            for (tid,) in cur.fetchall() or []:
                try:
                    existing.add(int(tid))
                except Exception:
                    continue
        conn.commit()

    return set(ids) - existing


def _extract_venue_rows_from_fixtures_envelope(envelope: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Build minimal core.venues rows from /fixtures response payload.
    This avoids extra API calls and prevents FK violations on core.fixtures.venue_id.
    """
    venues_by_id: dict[int, dict[str, Any]] = {}
    for item in envelope.get("response") or []:
        fixture = (item or {}).get("fixture") or {}
        venue = fixture.get("venue") or {}
        vid = venue.get("id")
        try:
            if vid is None:
                continue
            vid_int = int(vid)
        except Exception:
            continue
        if vid_int <= 0:
            # API uses 0 to mean "unknown"
            continue
        # name/city are typically present; other columns remain NULL
        venues_by_id[vid_int] = {
            "id": vid_int,
            "name": venue.get("name"),
            "city": venue.get("city"),
        }
    # deterministic
    return [venues_by_id[k] for k in sorted(venues_by_id)]


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
    existing_seasons = query_scalar("SELECT seasons FROM core.leagues WHERE id=%s", (int(league_id),))
    if existing_seasons is not None:
        # If season is not specified, existence is enough.
        if season is None or int(season) <= 0:
            return
        # If the league exists but doesn't contain the requested season metadata, refresh.
        if _league_has_season_metadata(existing_seasons, int(season)):
            return

    # Refresh (or create): fetch full league object by id.
    # IMPORTANT: do NOT season-scope this request; we want the full seasons array
    # so backfill windowing can work for current+prev without extra API calls.
    params: dict[str, Any] = {"id": int(league_id)}
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
    logger.info("league_upserted_dependency", league_id=int(league_id), season=(int(season) if season is not None else None))


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

    # DB-backed cache: once /teams is fetched successfully for (league_id, season),
    # skip future /teams calls in this season to avoid per-minute rateLimit errors.
    cached = query_scalar(
        "SELECT completed FROM core.team_bootstrap_progress WHERE league_id=%s AND season=%s",
        (int(league_id), int(season)),
    )
    if cached is True:
        missing = get_missing_team_ids_in_core(team_ids)
        if not missing:
            return
        logger.warning(
            "teams_bootstrap_cache_incomplete_refreshing",
            league_id=int(league_id),
            season=int(season),
            missing_team_ids_count=len(missing),
            missing_team_ids_sample=sorted(list(missing))[:25],
        )
        # Flip cache to incomplete before attempting refresh, for observability.
        try:
            upsert_core(
                full_table_name="core.team_bootstrap_progress",
                rows=[
                    {
                        "league_id": int(league_id),
                        "season": int(season),
                        "completed": False,
                        "last_error": f"cache_incomplete_missing_teams:{len(missing)}",
                    }
                ],
                conflict_cols=["league_id", "season"],
                update_cols=["completed", "last_error"],
            )
        except Exception:
            pass

    # Create progress row if missing (idempotent).
    try:
        upsert_core(
            full_table_name="core.team_bootstrap_progress",
            rows=[
                {
                    "league_id": int(league_id),
                    "season": int(season),
                    "completed": False,
                    "last_error": None,
                }
            ],
            conflict_cols=["league_id", "season"],
            update_cols=["completed", "last_error"],
        )
    except Exception:
        # Best-effort; proceed with /teams call.
        pass

    params = {"league": int(league_id), "season": int(season)}
    try:
        res = await _fetch_and_store(client=client, limiter=limiter, endpoint="/teams", params=params)
        env = res.data or {}
        if env.get("errors"):
            raise RuntimeError(f"api_errors:/teams:{env.get('errors')}")
    except Exception as e:
        # Persist error for observability; caller will decide how to proceed.
        try:
            upsert_core(
                full_table_name="core.team_bootstrap_progress",
                rows=[
                    {
                        "league_id": int(league_id),
                        "season": int(season),
                        "completed": False,
                        "last_error": str(e),
                    }
                ],
                conflict_cols=["league_id", "season"],
                update_cols=["completed", "last_error"],
            )
        except Exception:
            pass
        raise

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

    # Mark completed (cache hit for future windows).
    missing_after = get_missing_team_ids_in_core(team_ids)
    try:
        upsert_core(
            full_table_name="core.team_bootstrap_progress",
            rows=[
                {
                    "league_id": int(league_id),
                    "season": int(season),
                    "completed": True,
                    "last_error": (
                        None
                        if not missing_after
                        else f"teams_still_missing_after_refresh:{len(missing_after)}"
                    ),
                }
            ],
            conflict_cols=["league_id", "season"],
            update_cols=["completed", "last_error"],
        )
    except Exception:
        pass

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
    log_venues: bool = True,
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

    # Venues referenced by fixtures must exist before inserting core.fixtures (FK).
    venue_rows = _extract_venue_rows_from_fixtures_envelope(fixtures_envelope)
    if venue_rows:
        upsert_core(
            full_table_name="core.venues",
            rows=venue_rows,
            conflict_cols=["id"],
            update_cols=["name", "city"],
        )
        if log_venues:
            logger.info(
                "venues_upserted_dependency",
                league_id=int(league_id),
                season=int(season),
                venues_upserted=len(venue_rows),
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


