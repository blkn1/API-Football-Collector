from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import yaml

from src.collector.api_client import APIClient, APIClientError, APIResult, RateLimitError
from src.collector.rate_limiter import EmergencyStopError, RateLimiter
from src.transforms.top_scorers import transform_top_scorers
from src.utils.db import upsert_core, upsert_mart_coverage, upsert_raw
from src.utils.logging import get_logger


logger = get_logger(component="jobs_top_scorers")


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


async def run_top_scorers_daily(*, client: APIClient, limiter: RateLimiter, config_path: Path) -> None:
    """
    Daily leaderboard collection:
    - GET /players/topscorers?league=<id>&season=<season>
    - RAW archive always
    - CORE upsert into core.top_scorers
    """
    leagues = _load_tracked_leagues(config_path)

    total_rows = 0
    api_requests = 0

    for l in leagues:
        league_id = int(l["id"])
        season = int(l["season"])
        params = {"league": league_id, "season": season}

        try:
            res, env = await _safe_get_envelope(
                client=client,
                limiter=limiter,
                endpoint="/players/topscorers",
                params=params,
                label=f"/players/topscorers(league={league_id},season={season})",
            )
            api_requests += 1
        except EmergencyStopError as e:
            logger.error("emergency_stop_daily_quota_low", job="top_scorers_daily", league_id=league_id, err=str(e))
            break
        except RateLimitError as e:
            logger.warning("api_rate_limited_429", league_id=league_id, err=str(e), sleep_seconds=5)
            await asyncio.sleep(5)
            continue
        except APIClientError as e:
            logger.error("api_call_failed", league_id=league_id, season=season, err=str(e))
            continue
        except Exception as e:
            logger.error("top_scorers_fetch_failed", league_id=league_id, season=season, err=str(e))
            continue

        upsert_raw(
            endpoint="/players/topscorers",
            requested_params=params,
            status_code=res.status_code,
            response_headers=res.headers,
            body=env,
        )

        rows = transform_top_scorers(envelope=env, league_id=league_id, season=season)
        if rows:
            upsert_core(
                full_table_name="core.top_scorers",
                rows=rows,
                conflict_cols=["league_id", "season", "player_id"],
                update_cols=["rank", "team_id", "team_name", "goals", "assists", "raw"],
            )
            total_rows += len(rows)

        # MART coverage (presence+freshness)
        try:
            from src.coverage.calculator import CoverageCalculator

            calc = CoverageCalculator()
            cov = calc.calculate_top_scorers_coverage(league_id=league_id, season=season)
            upsert_mart_coverage(coverage_data=cov)
        except Exception as e:
            logger.warning("top_scorers_coverage_update_failed", league_id=league_id, season=season, err=str(e))

        logger.info(
            "top_scorers_synced_league",
            league_id=league_id,
            season=season,
            rows=len(rows),
            daily_remaining=limiter.quota.daily_remaining,
            minute_remaining=limiter.quota.minute_remaining,
        )

    logger.info(
        "top_scorers_daily_complete",
        leagues=len(leagues),
        api_requests=api_requests,
        core_rows=total_rows,
        daily_remaining=limiter.quota.daily_remaining,
        minute_remaining=limiter.quota.minute_remaining,
    )


