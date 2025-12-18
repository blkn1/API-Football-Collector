from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC_DIR))

from collector.api_client import APIClient, APIClientError, APIResult, RateLimitError  # noqa: E402
from collector.rate_limiter import EmergencyStopError, RateLimiter  # noqa: E402
from transforms.fixtures import transform_fixtures  # noqa: E402
from utils.db import get_transaction, query_scalar, upsert_core, upsert_mart_coverage, upsert_raw  # noqa: E402
from utils.logging import get_logger, setup_logging  # noqa: E402
from utils.standings import sync_standings  # noqa: E402
from coverage.calculator import CoverageCalculator  # noqa: E402
from utils.venues_backfill import backfill_missing_venues_for_fixtures  # noqa: E402
from utils.config import load_api_config, load_rate_limiter_config  # noqa: E402
from utils.dependencies import ensure_fixtures_dependencies  # noqa: E402


logger = get_logger(script="daily_sync")


@dataclass(frozen=True)
class TrackedLeague:
    id: int
    name: str | None = None
    season: int | None = None


@dataclass(frozen=True)
class DailySyncSummary:
    date_utc: str
    leagues: list[int]
    api_requests: int
    total_fixtures: int
    daily_remaining: int | None
    minute_remaining: int | None


def _utc_today_str() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _load_daily_config(config_path: Path) -> tuple[int, list[TrackedLeague], str]:
    cfg = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}

    fetch_mode = str(cfg.get("fixtures_fetch_mode") or "per_tracked_leagues").strip()
    if fetch_mode not in ("per_tracked_leagues", "global_by_date"):
        raise ValueError(f"Invalid fixtures_fetch_mode in {config_path}: {fetch_mode} (expected per_tracked_leagues|global_by_date)")

    # Preferred config shape (Phase 3):
    # season: 2024
    # tracked_leagues: [{id: 39, name: "..."}]
    season = cfg.get("season")
    tracked_leagues = cfg.get("tracked_leagues")

    leagues: list[TrackedLeague] = []
    if isinstance(tracked_leagues, list):
        for x in tracked_leagues:
            if not isinstance(x, dict) or "id" not in x:
                continue
            leagues.append(TrackedLeague(id=int(x["id"]), name=x.get("name"), season=(int(x["season"]) if x.get("season") is not None else None)))

    # Backward-compatible fallback: try to infer from jobs config if present
    if not leagues:
        jobs = cfg.get("jobs") or []
        for j in jobs:
            if not isinstance(j, dict):
                continue
            if j.get("endpoint") != "/fixtures":
                continue
            params = j.get("params") or {}
            league_id = params.get("league")
            job_season = params.get("season")
            if league_id is None:
                continue
            leagues.append(TrackedLeague(id=int(league_id), name=j.get("job_id")))
            if season is None and job_season is not None:
                season = int(job_season)

    # Allow omitting top-level season if every tracked league item provides a season (more flexible for multi-competition tracking).
    if season is None:
        if not leagues or any(l.season is None for l in leagues):
            raise ValueError(
                f"Missing season in config: {config_path}. Either set top-level 'season: <year>' or add 'season' for each tracked_leagues item."
            )
        # dummy season (won't be used because per-league season overrides)
        season = 0

    if not leagues:
        raise ValueError(
            f"No tracked leagues configured in {config_path}. Add 'tracked_leagues:' list or fill fixtures jobs with params.league."
        )

    return int(season), leagues, fetch_mode


def _fixture_group_key(item: dict[str, Any]) -> tuple[int, int] | None:
    """
    Determine (league_id, season) from a /fixtures response item.
    Returns None if the shape is unexpected.
    """
    try:
        league = (item or {}).get("league") or {}
        lid = league.get("id")
        season = league.get("season")
        if lid is None or season is None:
            return None
        return (int(lid), int(season))
    except Exception:
        return None


def _refresh_mart_views(conn) -> None:
    with conn.cursor() as cur:
        # No CONCURRENTLY (no unique index guarantee). This is safe for batch jobs.
        cur.execute("REFRESH MATERIALIZED VIEW mart.daily_fixtures_dashboard;")
        # NOTE: mart.coverage_status is a TABLE in Phase 3 (written by CoverageCalculator).


def _count_existing(conn, table: str, id_col: str, ids: list[int]) -> int:
    if not ids:
        return 0
    with conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) FROM {table} WHERE {id_col} = ANY(%s)", (ids,))
        return int(cur.fetchone()[0])


async def sync_daily_fixtures(
    *,
    target_date_utc: str,
    league_filter: int | None = None,
    dry_run: bool = False,
    config_path: Path | None = None,
    client: APIClient | None = None,
    limiter: RateLimiter | None = None,
    with_standings: bool = False,
) -> DailySyncSummary:
    """
    Daily sync for /fixtures?league={id}&season={season}&date={YYYY-MM-DD} (UTC)

    - Stores RAW envelope
    - Transforms (fixtures + fixture_details)
    - UPSERTs CORE atomically (fixtures + fixture_details)
    - Refreshes MART coverage/dashboard views
    """
    cfg_path = config_path or (PROJECT_ROOT / "config" / "jobs" / "daily.yaml")
    season, tracked, fetch_mode = _load_daily_config(cfg_path)

    leagues = tracked
    if league_filter is not None:
        leagues = [x for x in leagues if x.id == int(league_filter)]
        if not leagues:
            raise ValueError(f"League {league_filter} not found in tracked leagues config: {cfg_path}")

    # Dry-run must NOT consume quota and must NOT require API key / DB.
    if dry_run:
        planned_requests: list[dict[str, Any]] = []
        if league_filter is not None or fetch_mode == "per_tracked_leagues":
            planned_requests = [{"endpoint": "/fixtures", "params": {"league": l.id, "season": season, "date": target_date_utc}} for l in leagues]
        else:
            planned_requests = [{"endpoint": "/fixtures", "params": {"date": target_date_utc, "page": 1}, "note": "global_by_date_paging"}]

        logger.info(
            "daily_sync_dry_run_planned",
            date=target_date_utc,
            season=season,
            fixtures_fetch_mode=fetch_mode,
            leagues=[l.id for l in leagues],
            requests=planned_requests,
        )
        return DailySyncSummary(
            date_utc=target_date_utc,
            leagues=[l.id for l in leagues],
            api_requests=0,
            total_fixtures=0,
            daily_remaining=None,
            minute_remaining=None,
        )

    rl_cfg = load_rate_limiter_config()
    api_cfg = load_api_config()
    limiter2 = limiter or RateLimiter(
        max_tokens=rl_cfg.minute_soft_limit,
        refill_rate=float(rl_cfg.minute_soft_limit) / 60.0,
        emergency_stop_threshold=rl_cfg.emergency_stop_threshold,
    )
    client2 = client or APIClient(
        base_url=api_cfg.base_url,
        timeout_seconds=api_cfg.timeout_seconds,
        api_key_env=api_cfg.api_key_env,
    )

    api_requests = 0
    total_fixtures = 0

    logger.info(
        "daily_sync_started",
        date=target_date_utc,
        season=season,
        dry_run=dry_run,
        fixtures_fetch_mode=fetch_mode,
        leagues=[l.id for l in leagues],
    )

    try:
        # If a specific league is requested, force per-tracked-leagues behavior for determinism/tests.
        effective_mode = "per_tracked_leagues" if league_filter is not None else fetch_mode

        if effective_mode == "per_tracked_leagues":
            for l in leagues:
                league_id = l.id
                league_name = l.name or f"League {league_id}"
                logger.info("league_sync_started", league_id=league_id, league_name=league_name, date=target_date_utc, season=season)

                league_season = int(l.season) if l.season is not None else int(season)
                params = {"league": league_id, "season": league_season, "date": target_date_utc}

                try:
                    limiter2.acquire_token()
                    result: APIResult = await client2.get("/fixtures", params=params)
                    api_requests += 1
                    limiter2.update_from_headers(result.headers)
                except EmergencyStopError as e:
                    logger.error("emergency_stop_daily_quota_low", league_id=league_id, err=str(e))
                    break
                except RateLimitError as e:
                    # Respect rate limits: wait a bit, then continue to next league.
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
                resp_count = len(envelope.get("response") or [])
                total_fixtures += resp_count

                # RAW insert
                upsert_raw(
                    endpoint="/fixtures",
                    requested_params=params,
                    status_code=result.status_code,
                    response_headers=result.headers,
                    body=envelope,
                )
                logger.info("raw_stored", league_id=league_id, fixtures=resp_count)

                # Dependencies
                try:
                    await ensure_fixtures_dependencies(
                        league_id=league_id,
                        season=league_season,
                        fixtures_envelope=envelope,
                        client=client2,
                        limiter=limiter2,
                    )
                except Exception as e:
                    logger.error("dependency_bootstrap_failed", league_id=league_id, err=str(e))
                    continue

                # Transform
                fixtures_rows, details_rows = transform_fixtures(envelope)
                fixture_ids = [int(r["id"]) for r in fixtures_rows]
                details_ids = [int(r["fixture_id"]) for r in details_rows]
                venue_ids = [int(r["venue_id"]) for r in fixtures_rows if r.get("venue_id") is not None]

                # Optional expensive venue backfill
                try:
                    max_venues = int(os.getenv("VENUES_BACKFILL_MAX_PER_RUN", "0"))
                    if max_venues <= 0:
                        upserted_venues = 0
                    else:
                        upserted_venues = await backfill_missing_venues_for_fixtures(
                            venue_ids=venue_ids,
                            client=client2,
                            limiter=limiter2,
                            dry_run=False,
                            max_to_fetch=max_venues,
                        )
                    if upserted_venues:
                        logger.info("venues_backfilled", league_id=league_id, upserted=upserted_venues)
                except Exception as e:
                    logger.warning("venues_backfill_failed", league_id=league_id, err=str(e))

                # CORE UPSERT atomically
                try:
                    with get_transaction() as conn:
                        existing_f = _count_existing(conn, "core.fixtures", "id", fixture_ids)
                        existing_d = _count_existing(conn, "core.fixture_details", "fixture_id", details_ids)

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

                    new_f = max(0, len(fixture_ids) - existing_f)
                    upd_f = existing_f
                    new_d = max(0, len(details_ids) - existing_d)
                    upd_d = existing_d

                    logger.info(
                        "core_upserted",
                        league_id=league_id,
                        fixtures_total=len(fixture_ids),
                        fixtures_new=new_f,
                        fixtures_updated=upd_f,
                        fixture_details_total=len(details_ids),
                        fixture_details_new=new_d,
                        fixture_details_updated=upd_d,
                    )
                except Exception as e:
                    logger.error("db_upsert_failed", league_id=league_id, err=str(e))
                    continue

                q = limiter2.quota
                logger.info(
                    "quota_snapshot",
                    league_id=league_id,
                    daily_remaining=q.daily_remaining,
                    minute_remaining=q.minute_remaining,
                )
        else:
            # Global-by-date mode: fetch ALL fixtures for the date (paging) then group by (league_id, season).
            logger.info("global_date_sync_started", date=target_date_utc)

            max_pages = int(os.getenv("DAILY_FIXTURES_GLOBAL_MAX_PAGES", "250"))
            page = 1
            total_pages: int | None = None
            dedup_by_fixture_id: dict[int, dict[str, Any]] = {}

            while True:
                # NOTE:
                # - Use config-driven default timezone to avoid date boundary ambiguity.
                # - Do NOT pass page=1 explicitly; some API stacks behave differently when page is provided.
                #   We call page 1 without the param, then page>=2 with explicit paging if needed.
                params: dict[str, Any] = {"date": target_date_utc, "timezone": getattr(api_cfg, "default_timezone", "UTC")}
                if page > 1:
                    params["page"] = int(page)
                try:
                    limiter2.acquire_token()
                    result: APIResult = await client2.get("/fixtures", params=params)
                    api_requests += 1
                    limiter2.update_from_headers(result.headers)
                except EmergencyStopError as e:
                    logger.error("emergency_stop_daily_quota_low", mode="global_by_date", err=str(e))
                    break
                except RateLimitError as e:
                    logger.warning("api_rate_limited", mode="global_by_date", err=str(e), sleep_seconds=5)
                    await asyncio.sleep(5)
                    continue
                except APIClientError as e:
                    logger.error("api_call_failed", mode="global_by_date", page=page, err=str(e))
                    break
                except Exception as e:
                    logger.error("api_call_unexpected_error", mode="global_by_date", page=page, err=str(e))
                    break

                envelope = result.data or {}
                items = envelope.get("response") or []
                # Some API stacks behave unexpectedly with timezone filters on date-only fixture requests.
                # If we get an empty page-1 response (and no API-reported errors), retry once without timezone.
                if (
                    page == 1
                    and not items
                    and not envelope.get("errors")
                    and isinstance(params.get("timezone"), str)
                    and params.get("timezone")
                ):
                    retry_params: dict[str, Any] = {"date": target_date_utc}
                    try:
                        limiter2.acquire_token()
                        retry_result: APIResult = await client2.get("/fixtures", params=retry_params)
                        api_requests += 1
                        limiter2.update_from_headers(retry_result.headers)
                        retry_env = retry_result.data or {}
                        retry_items = retry_env.get("response") or []
                        if retry_env.get("errors"):
                            logger.warning(
                                "global_by_date_retry_api_errors",
                                date=target_date_utc,
                                errors=retry_env.get("errors"),
                            )
                        logger.info(
                            "global_by_date_retry_without_timezone",
                            date=target_date_utc,
                            prev_timezone=params.get("timezone"),
                            prev_results=(envelope.get("results") or 0),
                            retry_results=(retry_env.get("results") or 0),
                            retry_fixtures_in_page=len(retry_items),
                        )
                        # Prefer retry response if it yields data
                        if retry_items:
                            params = retry_params
                            envelope = retry_env
                            items = retry_items
                    except (EmergencyStopError, RateLimitError, APIClientError) as e:
                        logger.warning("global_by_date_retry_failed", date=target_date_utc, err=str(e))
                if envelope.get("errors"):
                    logger.warning(
                        "global_by_date_api_errors",
                        date=target_date_utc,
                        page=page,
                        errors=envelope.get("errors"),
                    )

                # RAW insert per page
                upsert_raw(
                    endpoint="/fixtures",
                    requested_params=params,
                    status_code=result.status_code,
                    response_headers=result.headers,
                    body=envelope,
                )

                # Dedup by fixture id across pages (keep last seen)
                for it in items:
                    try:
                        fid = int(((it or {}).get("fixture") or {}).get("id"))
                    except Exception:
                        continue
                    dedup_by_fixture_id[fid] = it

                if total_pages is None:
                    try:
                        total_pages = int(((envelope.get("paging") or {}).get("total")) or 1)
                    except Exception:
                        total_pages = 1

                logger.info(
                    "global_page_stored",
                    page=page,
                    total_pages=total_pages,
                    fixtures_in_page=len(items),
                    fixtures_dedup_total=len(dedup_by_fixture_id),
                    results=(envelope.get("results") or 0),
                )

                if page >= (total_pages or 1):
                    break
                if page >= max_pages:
                    logger.warning("global_by_date_page_cap_reached", max_pages=max_pages, observed_total_pages=total_pages)
                    break

                page += 1

            total_fixtures += len(dedup_by_fixture_id)

            # Group response items by (league_id, season) to preserve dependency order and FK safety.
            grouped_items: dict[tuple[int, int], list[dict[str, Any]]] = {}
            for it in dedup_by_fixture_id.values():
                k = _fixture_group_key(it)
                if k is None:
                    continue
                grouped_items.setdefault(k, []).append(it)

            logger.info("global_grouping_complete", groups=len(grouped_items), fixtures=len(dedup_by_fixture_id))

            # Process each group (dependencies -> transform -> upsert)
            for (league_id, league_season), items in sorted(grouped_items.items(), key=lambda x: (x[0][0], x[0][1])):
                league_name = f"League {league_id}"
                logger.info(
                    "league_sync_started",
                    league_id=league_id,
                    league_name=league_name,
                    date=target_date_utc,
                    season=league_season,
                    mode="global_by_date",
                    fixtures=len(items),
                )

                group_env = {"response": items}

                try:
                    await ensure_fixtures_dependencies(
                        league_id=league_id,
                        season=league_season,
                        fixtures_envelope=group_env,
                        client=client2,
                        limiter=limiter2,
                    )
                except Exception as e:
                    logger.error("dependency_bootstrap_failed", league_id=league_id, season=league_season, err=str(e))
                    continue

                fixtures_rows, details_rows = transform_fixtures(group_env)
                fixture_ids = [int(r["id"]) for r in fixtures_rows]
                details_ids = [int(r["fixture_id"]) for r in details_rows]
                venue_ids = [int(r["venue_id"]) for r in fixtures_rows if r.get("venue_id") is not None]

                try:
                    max_venues = int(os.getenv("VENUES_BACKFILL_MAX_PER_RUN", "0"))
                    if max_venues <= 0:
                        upserted_venues = 0
                    else:
                        upserted_venues = await backfill_missing_venues_for_fixtures(
                            venue_ids=venue_ids,
                            client=client2,
                            limiter=limiter2,
                            dry_run=False,
                            max_to_fetch=max_venues,
                        )
                    if upserted_venues:
                        logger.info("venues_backfilled", league_id=league_id, upserted=upserted_venues)
                except Exception as e:
                    logger.warning("venues_backfill_failed", league_id=league_id, err=str(e))

                try:
                    with get_transaction() as conn:
                        existing_f = _count_existing(conn, "core.fixtures", "id", fixture_ids)
                        existing_d = _count_existing(conn, "core.fixture_details", "fixture_id", details_ids)

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

                    new_f = max(0, len(fixture_ids) - existing_f)
                    upd_f = existing_f
                    new_d = max(0, len(details_ids) - existing_d)
                    upd_d = existing_d

                    logger.info(
                        "core_upserted",
                        league_id=league_id,
                        fixtures_total=len(fixture_ids),
                        fixtures_new=new_f,
                        fixtures_updated=upd_f,
                        fixture_details_total=len(details_ids),
                        fixture_details_new=new_d,
                        fixture_details_updated=upd_d,
                    )
                except Exception as e:
                    logger.error("db_upsert_failed", league_id=league_id, err=str(e))
                    continue

            q = limiter2.quota
            logger.info(
                "quota_snapshot",
                mode="global_by_date",
                daily_remaining=q.daily_remaining,
                minute_remaining=q.minute_remaining,
            )

        # Coverage metrics: refresh mart views once at end (if not dry-run)
        try:
            with get_transaction() as conn:
                _refresh_mart_views(conn)
            logger.info("mart_refreshed", views=["mart.daily_fixtures_dashboard", "mart.coverage_status"])
        except Exception as e:
            logger.error("mart_refresh_failed", err=str(e))

        # Coverage calculator (Phase 3): write per-league coverage rows into mart.coverage_status
        try:
            calc = CoverageCalculator()
            # Keep existing coverage behavior: compute coverage for configured tracked leagues.
            # In global_by_date mode we also ingest cups/UEFA etc, but we intentionally do not
            # expand coverage rows for every competition to avoid unbounded mart growth.
            for l in tracked:
                cov_season = int(l.season) if l.season is not None else int(season)
                cov = calc.calculate_fixtures_coverage(l.id, cov_season)
                upsert_mart_coverage(coverage_data=cov)
                logger.info("coverage_calculated", league_id=l.id, season=season, endpoint="/fixtures", overall=cov.get("overall_coverage"))
        except Exception as e:
            logger.error("coverage_calculation_failed", err=str(e))

        if with_standings:
            try:
                await sync_standings(
                    league_filter=league_filter,
                    dry_run=False,
                    config_path=cfg_path,
                )
            except Exception as e:
                logger.error("with_standings_failed", err=str(e))

        q = limiter2.quota
        logger.info(
            "daily_sync_complete",
            date=target_date_utc,
            api_requests=api_requests,
            total_fixtures=total_fixtures,
            daily_remaining=q.daily_remaining,
            minute_remaining=q.minute_remaining,
        )

        return DailySyncSummary(
            date_utc=target_date_utc,
            leagues=[l.id for l in leagues],
            api_requests=api_requests,
            total_fixtures=total_fixtures,
            daily_remaining=q.daily_remaining,
            minute_remaining=q.minute_remaining,
        )
    finally:
        if client is None:
            await client2.aclose()


async def _amain() -> int:
    setup_logging()

    parser = argparse.ArgumentParser(description="Phase 3 - Daily fixtures sync")
    parser.add_argument("--date", type=str, default=None, help="Override UTC date (YYYY-MM-DD)")
    parser.add_argument("--league", type=int, default=None, help="Sync a single league id")
    parser.add_argument("--dry-run", action="store_true", help="Don't write to DB")
    parser.add_argument("--with-standings", action="store_true", help="Also sync standings (/standings) after fixtures")
    args = parser.parse_args()

    target_date = args.date or _utc_today_str()
    summary = await sync_daily_fixtures(
        target_date_utc=target_date,
        league_filter=args.league,
        dry_run=args.dry_run,
        with_standings=args.with_standings,
    )

    # Human-friendly summary line (logs are JSON via structlog)
    print(f"[INFO] Daily sync complete (date={summary.date_utc}, leagues={summary.leagues}, fixtures={summary.total_fixtures}, api_requests={summary.api_requests}, daily_remaining={summary.daily_remaining})")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_amain()))


