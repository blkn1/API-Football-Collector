from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from src.collector.api_client import APIClient, APIResult
from src.collector.rate_limiter import RateLimiter
from src.utils.db import upsert_raw
from src.utils.logging import get_logger


logger = get_logger(component="jobs_season_rollover")


@dataclass(frozen=True)
class TrackedLeague:
    id: int
    season: int
    name: str | None = None


def _extract_league_ids_from_leagues_response(env: dict[str, Any]) -> set[int]:
    """
    Extract league IDs from API-Football /leagues envelope.
    Expected shape:
      { "response": [ { "league": { "id": 39, ... }, ... }, ... ] }
    """
    out: set[int] = set()
    resp = env.get("response")
    if not isinstance(resp, list):
        return out
    for item in resp:
        if not isinstance(item, dict):
            continue
        league = item.get("league")
        if not isinstance(league, dict):
            continue
        lid = league.get("id")
        try:
            if lid is not None:
                out.add(int(lid))
        except Exception:
            continue
    return out


def _load_tracked_leagues(config_path: Path) -> list[TrackedLeague]:
    cfg = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    tracked = cfg.get("tracked_leagues") or []
    out: list[TrackedLeague] = []
    if not isinstance(tracked, list):
        return out
    for x in tracked:
        if not isinstance(x, dict) or x.get("id") is None or x.get("season") is None:
            continue
        try:
            out.append(TrackedLeague(id=int(x["id"]), season=int(x["season"]), name=(str(x["name"]) if x.get("name") else None)))
        except Exception:
            continue
    return out


async def _fetch_leagues_for_season(*, client: APIClient, limiter: RateLimiter, season: int) -> APIResult:
    limiter.acquire_token()
    res = await client.get("/leagues", params={"season": int(season)})
    limiter.update_from_headers(res.headers)
    # Archive RAW for audit/debug (read-only from MCP later).
    upsert_raw(
        endpoint="/leagues",
        requested_params={"season": int(season)},
        status_code=res.status_code,
        response_headers=res.headers,
        body=res.data or {},
    )
    return res


async def run_season_rollover_watch(*, client: APIClient, limiter: RateLimiter, config_path: Path) -> None:
    """
    Watch for season rollovers for tracked leagues.

    This job does NOT change config automatically.
    It logs an actionable warning when a tracked league's "next season" becomes available in /leagues.
    """
    tracked = _load_tracked_leagues(config_path)
    if not tracked:
        logger.info("season_rollover_watch_skipped", reason="no_tracked_leagues", config=str(config_path))
        return

    # Group by current season, because next season checks are per-season and we want to batch API calls.
    seasons = sorted({t.season for t in tracked})
    # For each unique season S in config, check S+1 availability.
    next_seasons = sorted({s + 1 for s in seasons})

    # Fetch /leagues once per next-season.
    available_by_next_season: dict[int, set[int]] = {}
    for ns in next_seasons:
        res = await _fetch_leagues_for_season(client=client, limiter=limiter, season=ns)
        env = res.data or {}
        available_by_next_season[ns] = _extract_league_ids_from_leagues_response(env)

    # Emit warnings per tracked league when next-season is now available.
    for t in tracked:
        ns = t.season + 1
        available = available_by_next_season.get(ns) or set()
        if t.id not in available:
            continue

        # Actionable guidance: exactly what to edit.
        # Keep it short and explicit. User can copy/paste.
        suggested_entry = {"id": t.id, "season": ns}
        if t.name:
            suggested_entry["name"] = t.name

        logger.warning(
            "season_rollover_available",
            league_id=int(t.id),
            league_name=t.name,
            current_season=int(t.season),
            next_season=int(ns),
            action_file="config/jobs/daily.yaml",
            action_instructions=f"Update tracked_leagues entry where id={t.id}: change season {t.season} -> {ns}",
            action_yaml_snippet=f"- id: {t.id}\\n  season: {ns}" + (f"\\n  name: {t.name}" if t.name else ""),
        )


