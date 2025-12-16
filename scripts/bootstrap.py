from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC_DIR))

from collector.api_client import APIClient
from collector.rate_limiter import EmergencyStopError, RateLimiter
from transforms.countries import transform_countries
from transforms.leagues import transform_leagues
from transforms.teams import transform_teams
from transforms.timezones import transform_timezones
from transforms.venues import transform_venues_from_teams
from utils.db import query_scalar, upsert_core, upsert_raw
from utils.logging import setup_logging
from utils.config import load_api_config, load_rate_limiter_config

def _load_bootstrap_plan_from_static_config() -> tuple[int, set[int]]:
    """
    Config-driven defaults (no code-side hard-coding):
    - season: from config/jobs/static.yaml -> bootstrap_leagues.params.season (or bootstrap_teams.params.season)
    - tracked leagues: from config/jobs/static.yaml -> bootstrap_leagues.filters.tracked_leagues (or bootstrap_teams.mode.tracked_leagues)
    """
    cfg_path = PROJECT_ROOT / "config" / "jobs" / "static.yaml"
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    jobs = cfg.get("jobs") or []

    season: int | None = None
    tracked: set[int] = set()
    for j in jobs:
        if not isinstance(j, dict):
            continue
        jid = j.get("job_id")
        if jid not in ("bootstrap_leagues", "bootstrap_teams"):
            continue
        params = j.get("params") or {}
        if season is None and params.get("season") is not None:
            season = int(params.get("season"))
        filters = j.get("filters") or {}
        mode = j.get("mode") or {}
        tl = filters.get("tracked_leagues") or mode.get("tracked_leagues")
        if isinstance(tl, list) and tl:
            tracked = {int(x) for x in tl}

    if season is None:
        raise ValueError(f"Missing season in {cfg_path} (bootstrap_leagues/teams params.season)")
    if not tracked:
        raise ValueError(f"Missing tracked leagues in {cfg_path} (bootstrap_leagues.filters.tracked_leagues or bootstrap_teams.mode.tracked_leagues)")
    return int(season), tracked


async def fetch_and_store(
    *,
    client: APIClient,
    limiter: RateLimiter,
    endpoint: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    limiter.acquire_token()
    result = await client.get(endpoint, params=params or {})
    limiter.update_from_headers(result.headers)

    # Raw insert (archive full envelope)
    upsert_raw(
        endpoint=endpoint,
        requested_params=params or {},
        status_code=result.status_code,
        response_headers=result.headers,
        body=result.data or {},
    )

    return result.data or {}


def _h(s: str) -> str:
    return f"[INFO] {s}"


async def main() -> int:
    setup_logging()
    parser = argparse.ArgumentParser(description="Phase 2 bootstrap (static data)")
    parser.add_argument("--season", type=int, default=None, help="Season year for /leagues and /teams (if omitted, loads from config/jobs/static.yaml)")
    parser.add_argument(
        "--tracked-leagues",
        type=str,
        default=None,
        help="Comma-separated league IDs to process into CORE (if omitted, loads from config/jobs/static.yaml)",
    )
    args = parser.parse_args()

    if args.season is None and args.tracked_leagues is None:
        season, tracked = _load_bootstrap_plan_from_static_config()
    else:
        if args.season is None:
            raise ValueError("Missing --season (or omit both --season and --tracked-leagues to use config/jobs/static.yaml)")
        if args.tracked_leagues is None:
            raise ValueError("Missing --tracked-leagues (or omit both --season and --tracked-leagues to use config/jobs/static.yaml)")
        season = int(args.season)
        tracked = {int(x.strip()) for x in str(args.tracked_leagues).split(",") if x.strip()}

    rl_cfg = load_rate_limiter_config()
    api_cfg = load_api_config()
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
    total_api_requests = 0
    daily_remaining: int | None = None

    print(_h("Starting Phase 2 Bootstrap..."))

    try:
        # 1) countries
        print()
        print(_h("Step 1/4: Fetching countries..."))
        countries_env = await fetch_and_store(client=client, limiter=limiter, endpoint="/countries")
        total_api_requests += 1
        daily_remaining = limiter.quota.daily_remaining
        print("  ✅ API call successful (1 request used)")
        countries_rows = transform_countries(countries_env)
        upsert_core(
            full_table_name="core.countries",
            rows=countries_rows,
            conflict_cols=["code"],
            update_cols=["name", "flag"],
        )
        c_count = query_scalar("SELECT COUNT(*) FROM core.countries")
        raw_results = countries_env.get("results")
        print(f"  ✅ RAW: Stored {raw_results or '200+'} countries")
        print(f"  ✅ CORE: Upserted {c_count} countries")

        # 2) timezones
        print()
        print(_h("Step 2/4: Fetching timezones..."))
        tz_env = await fetch_and_store(client=client, limiter=limiter, endpoint="/timezone")
        total_api_requests += 1
        daily_remaining = limiter.quota.daily_remaining
        print("  ✅ API call successful (1 request used)")
        tz_rows = transform_timezones(tz_env)
        upsert_core(
            full_table_name="core.timezones",
            rows=tz_rows,
            conflict_cols=["name"],
            update_cols=["name"],
        )
        tz_count = query_scalar("SELECT COUNT(*) FROM core.timezones")
        raw_results = tz_env.get("results")
        print(f"  ✅ RAW: Stored {raw_results or '100+'} timezones")
        print(f"  ✅ CORE: Upserted {tz_count} timezones")

        # 3) leagues (RAW stores all; CORE stores only tracked leagues per requirement)
        print()
        print(_h(f"Step 3/4: Fetching leagues (season={season})..."))
        leagues_env = await fetch_and_store(
            client=client, limiter=limiter, endpoint="/leagues", params={"season": season}
        )
        total_api_requests += 1
        daily_remaining = limiter.quota.daily_remaining
        print("  ✅ API call successful (1 request used)")
        leagues_rows = transform_leagues(leagues_env, tracked_league_ids=tracked)
        upsert_core(
            full_table_name="core.leagues",
            rows=leagues_rows,
            conflict_cols=["id"],
            update_cols=[
                "name",
                "type",
                "logo",
                "country_name",
                "country_code",
                "country_flag",
                "seasons",
            ],
        )
        l_count = query_scalar("SELECT COUNT(*) FROM core.leagues")
        raw_results = leagues_env.get("results")
        print(f"  ✅ RAW: Stored {raw_results or '900+'} leagues (all)")
        print(f"  ✅ CORE: Upserted {l_count} leagues (tracked only: {', '.join(map(str, sorted(tracked)))})")

        league_name_by_id = {row["id"]: row["name"] for row in leagues_rows}

        # 4) teams (per tracked league)
        print()
        print(_h("Step 4/4: Fetching teams (tracked leagues)..."))
        total_teams = 0
        teams_raw_calls = 0
        for league_id in sorted(tracked):
            league_name = league_name_by_id.get(league_id, f"League {league_id}")
            teams_env = await fetch_and_store(
                client=client,
                limiter=limiter,
                endpoint="/teams",
                params={"league": league_id, "season": season},
            )
            total_api_requests += 1
            teams_raw_calls += 1
            daily_remaining = limiter.quota.daily_remaining

            team_count = len(teams_env.get("response") or [])
            print(f"  ✅ League {league_id} ({league_name}): {team_count} teams")

            venue_rows = transform_venues_from_teams(teams_env)
            if venue_rows:
                upsert_core(
                    full_table_name="core.venues",
                    rows=venue_rows,
                    conflict_cols=["id"],
                    update_cols=["name", "address", "city", "country", "capacity", "surface", "image"],
                )

            team_rows = transform_teams(teams_env)
            upsert_core(
                full_table_name="core.teams",
                rows=team_rows,
                conflict_cols=["id"],
                update_cols=["name", "code", "country", "founded", "national", "logo", "venue_id"],
            )

            teams_count = query_scalar("SELECT COUNT(*) FROM core.teams")
            total_teams = int(teams_count or 0)
        print(f"  ✅ RAW: Stored {teams_raw_calls} API responses")
        print(f"  ✅ CORE: Upserted {total_teams} teams")

        print()
        print(_h("Bootstrap complete!"))
        if daily_remaining is not None:
            print(f"Total API requests: {total_api_requests} (daily remaining reported by API: {daily_remaining})")
        else:
            print(f"Total API requests: {total_api_requests}")
        print("Coverage:")
        print(f"  - Countries: {c_count} records")
        print(f"  - Timezones: {tz_count} records")
        print(f"  - Leagues: {l_count} records (tracked)")
        print(f"  - Teams: {total_teams} records")
        return 0
    except EmergencyStopError as e:
        print(_h(f"Emergency stop triggered: {e}"))
        return 2
    finally:
        await client.aclose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))


