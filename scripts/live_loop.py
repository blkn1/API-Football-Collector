from __future__ import annotations

import argparse
import asyncio
import signal
import sys
from dataclasses import dataclass
from datetime import timezone
from pathlib import Path
from typing import Any

import redis as redis_lib
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC_DIR))

from collector.api_client import APIClient, APIClientError, APIResult, RateLimitError, APIServerError  # noqa: E402
from collector.delta_detector import DeltaDetector, create_redis_client_from_env  # noqa: E402
from collector.rate_limiter import EmergencyStopError, RateLimiter  # noqa: E402
from transforms.fixtures import transform_fixtures  # noqa: E402
from utils.db import get_transaction, upsert_core, upsert_raw  # noqa: E402
from utils.logging import get_logger, setup_logging  # noqa: E402
from utils.venues_backfill import backfill_missing_venues_for_fixtures  # noqa: E402
import os
from utils.config import load_api_config, load_rate_limiter_config  # noqa: E402
from utils.dependencies import ensure_fixtures_dependencies  # noqa: E402


logger = get_logger(script="live_loop")


@dataclass(frozen=True)
class IterationStats:
    fixtures_live: int
    fixtures_tracked: int
    fixtures_changed: int
    fixtures_written: int
    api_daily_remaining: int | None
    api_minute_remaining: int | None


def extract_fixture_state(item: dict[str, Any]) -> dict[str, Any]:
    """
    Extract trackable state for delta detection from a /fixtures response item.
    """
    fx = item.get("fixture") or {}
    st = fx.get("status") or {}
    goals = item.get("goals") or {}
    return {
        "status": st.get("short"),
        "goals_home": goals.get("home"),
        "goals_away": goals.get("away"),
        "elapsed": st.get("elapsed"),
    }


def _fixture_id(item: dict[str, Any]) -> int | None:
    fx = item.get("fixture") or {}
    fid = fx.get("id")
    try:
        return int(fid) if fid is not None else None
    except Exception:
        return None


def _load_tracked_leagues_from_config() -> set[int] | None:
    """
    Optional config-driven tracked leagues for live loop.
    Reads from config/jobs/live.yaml -> jobs[].filters.tracked_leagues
    """
    cfg_path = PROJECT_ROOT / "config" / "jobs" / "live.yaml"
    try:
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return None

    jobs = cfg.get("jobs") or []
    for j in jobs:
        if not isinstance(j, dict):
            continue
        if j.get("endpoint") != "/fixtures":
            continue
        params = j.get("params") or {}
        if (params or {}).get("live") != "all":
            continue
        filters = j.get("filters") or {}
        tl = filters.get("tracked_leagues")
        if isinstance(tl, list) and tl:
            try:
                return {int(x) for x in tl}
            except Exception:
                return None
    return None


async def run_iteration(
    *,
    client: APIClient,
    limiter: RateLimiter,
    detector: DeltaDetector,
    tracked_leagues: set[int] | None,
    dry_run: bool,
) -> IterationStats:
    """
    Run one polling iteration:
    - /fixtures?live=all
    - filter tracked leagues
    - delta detect
    - write RAW always (unless dry-run)
    - UPSERT CORE only changed fixtures (and update delta cache only on successful write)
    """
    limiter.acquire_token()

    result: APIResult = await client.get("/fixtures", params={"live": "all"})
    limiter.update_from_headers(result.headers)

    envelope = result.data or {}
    all_items: list[dict[str, Any]] = envelope.get("response") or []
    # Snapshot live leagues for observability
    league_counts: dict[int, int] = {}
    for x in all_items:
        try:
            lid = int((x.get("league") or {}).get("id") or -1)
        except Exception:
            continue
        if lid <= 0:
            continue
        league_counts[lid] = league_counts.get(lid, 0) + 1

    track_all = tracked_leagues is None or len(tracked_leagues) == 0
    if track_all:
        tracked_items = list(all_items)
    else:
        tracked_items = [x for x in all_items if int((x.get("league") or {}).get("id") or -1) in tracked_leagues]

    fixtures_live = len(all_items)
    fixtures_tracked = len(tracked_items)

    if fixtures_live > 0:
        live_league_ids = sorted(league_counts.keys())
        if track_all:
            intersect = live_league_ids
        else:
            intersect = sorted(set(live_league_ids).intersection(tracked_leagues or set()))
        logger.info(
            "live_leagues_snapshot",
            live_league_ids=live_league_ids,
            tracked_league_ids=("ALL" if track_all else sorted(tracked_leagues or set())),
            tracked_live_league_ids=intersect,
            counts_by_league=league_counts,
        )

    # RAW archive (audit trail) - skipped in dry-run to avoid DB usage
    if not dry_run:
        upsert_raw(
            endpoint="/fixtures",
            requested_params={"live": "all"},
            status_code=result.status_code,
            response_headers=result.headers,
            body=envelope,
        )

    # Delta detection pass
    changed_items: list[dict[str, Any]] = []
    changed_meta: list[tuple[int, dict[str, Any], dict[str, Any], str | None]] = []
    for item in tracked_items:
        fid = _fixture_id(item)
        if fid is None:
            continue

        state = extract_fixture_state(item)
        if detector.has_changed(fid, state):
            diff = detector.get_diff(fid, state)
            league_name = (item.get("league") or {}).get("name")
            changed_items.append(item)
            changed_meta.append((fid, state, diff, league_name))

    fixtures_changed = len(changed_items)

    fixtures_written = 0
    if fixtures_changed == 0:
        q = limiter.quota
        return IterationStats(
            fixtures_live=fixtures_live,
            fixtures_tracked=fixtures_tracked,
            fixtures_changed=0,
            fixtures_written=0,
            api_daily_remaining=q.daily_remaining,
            api_minute_remaining=q.minute_remaining,
        )

    if dry_run:
        for fid, _state, diff, league_name in changed_meta:
            logger.info("fixture_would_update", fixture_id=fid, league=league_name, diff=diff)
        q = limiter.quota
        return IterationStats(
            fixtures_live=fixtures_live,
            fixtures_tracked=fixtures_tracked,
            fixtures_changed=fixtures_changed,
            fixtures_written=0,
            api_daily_remaining=q.daily_remaining,
            api_minute_remaining=q.minute_remaining,
        )

    # Write changed fixtures in a single transaction (reduce overhead)
    changed_envelope = {**envelope, "response": changed_items}
    fixtures_rows, details_rows = transform_fixtures(changed_envelope)

    # Ensure dependencies exist (league + teams) before inserting fixtures (FK integrity).
    try:
        # Prefer season from API envelope (league.season). This avoids guessing.
        seasons_by_league: dict[int, int] = {}
        for it in changed_items:
            try:
                lid = int((it.get("league") or {}).get("id") or -1)
                s = int((it.get("league") or {}).get("season") or 0)
            except Exception:
                continue
            if lid > 0 and s > 0:
                seasons_by_league[lid] = s

        # IMPORTANT: ensure_fixtures_dependencies must be called with a per-league envelope.
        # Passing a mixed-league envelope can cause incorrect team bootstrap attempts.
        grouped: dict[tuple[int, int], list[dict[str, Any]]] = {}
        for it in changed_items:
            try:
                lid = int((it.get("league") or {}).get("id") or -1)
                s = int((it.get("league") or {}).get("season") or 0)
            except Exception:
                continue
            if lid > 0 and s > 0:
                grouped.setdefault((lid, s), []).append(it)

        for (lid, s), items in sorted(grouped.items(), key=lambda x: (x[0][0], x[0][1])):
            await ensure_fixtures_dependencies(
                league_id=lid,
                season=s,
                fixtures_envelope={**envelope, "response": items},
                client=client,
                limiter=limiter,
            )
    except Exception as e:
        logger.error("dependency_bootstrap_failed", err=str(e))
        # best-effort: skip DB write this iteration to avoid FK errors
        q = limiter.quota
        return IterationStats(
            fixtures_live=fixtures_live,
            fixtures_tracked=fixtures_tracked,
            fixtures_changed=fixtures_changed,
            fixtures_written=0,
            api_daily_remaining=q.daily_remaining,
            api_minute_remaining=q.minute_remaining,
        )

    # Ensure referenced venues exist before inserting fixtures (prevents FK violations).
    venue_ids = [int(r["venue_id"]) for r in fixtures_rows if r.get("venue_id") is not None]
    try:
        max_venues = int(os.getenv("VENUES_BACKFILL_MAX_PER_RUN", "0"))
        if max_venues > 0:
            await backfill_missing_venues_for_fixtures(
                venue_ids=venue_ids,
                client=client,
                limiter=limiter,
                dry_run=False,
                max_to_fetch=max_venues,
            )
    except Exception as e:
        logger.warning("venues_backfill_failed", err=str(e))

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

    fixtures_written = len(fixtures_rows)

    # Update cache only after successful DB write
    for fid, state, diff, league_name in changed_meta:
        detector.update_cache(fid, state)
        logger.info("fixture_updated", fixture_id=fid, league=league_name, diff=diff)

    q = limiter.quota
    return IterationStats(
        fixtures_live=fixtures_live,
        fixtures_tracked=fixtures_tracked,
        fixtures_changed=fixtures_changed,
        fixtures_written=fixtures_written,
        api_daily_remaining=q.daily_remaining,
        api_minute_remaining=q.minute_remaining,
    )


async def run_live_loop(*, interval_seconds: int, once: bool, dry_run: bool) -> int:
    tracked = _load_tracked_leagues_from_config()
    # If tracked leagues are missing/empty, fall back to tracking ALL live fixtures.
    # This is production-safe because the API call is still a single /fixtures?live=all request;
    # it only affects which returned fixtures we write to CORE and show in live panels.
    if not tracked:
        logger.warning("live_loop_tracking_all_leagues_no_filter_configured")
        tracked = set()

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

    # Redis is required, but failures should not block the loop (detector is fail-open).
    try:
        redis_client = create_redis_client_from_env()
        # best-effort ping to surface connection issues early
        try:
            redis_client.ping()
        except redis_lib.exceptions.RedisError:
            logger.warning("redis_ping_failed_fail_open")
        detector = DeltaDetector(redis_client, ttl_seconds=7200)
    except Exception as e:
        # Extremely defensive fallback: create a "dead" client that always errors (detector fail-open anyway)
        logger.warning("redis_init_failed_fail_open", err=str(e))
        redis_client = create_redis_client_from_env()
        detector = DeltaDetector(redis_client, ttl_seconds=7200)

    running = True

    def _stop() -> None:
        nonlocal running
        logger.info("shutdown_signal_received")
        running = False

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop)
        except NotImplementedError:
            signal.signal(sig, lambda *_: _stop())

    backoff_seconds = 1.0

    try:
        while running:
            try:
                stats = await run_iteration(
                    client=client,
                    limiter=limiter,
                    detector=detector,
                    tracked_leagues=tracked,
                    dry_run=dry_run,
                )

                logger.info(
                    "live_loop_iteration",
                    fixtures_live=stats.fixtures_live,
                    fixtures_tracked=stats.fixtures_tracked,
                    changed=stats.fixtures_changed,
                    written=stats.fixtures_written,
                    daily_remaining=stats.api_daily_remaining,
                    minute_remaining=stats.api_minute_remaining,
                    interval_seconds=interval_seconds,
                )

                backoff_seconds = 1.0  # reset on success

                if once:
                    break

                await asyncio.sleep(interval_seconds)

            except EmergencyStopError as e:
                logger.error("emergency_stop_daily_quota_low", err=str(e))
                break
            except RateLimitError as e:
                # Exponential backoff on 429 (but keep within reasonable bounds)
                sleep_s = min(backoff_seconds, 60.0)
                logger.warning("api_rate_limited_backoff", err=str(e), sleep_seconds=sleep_s)
                await asyncio.sleep(sleep_s)
                backoff_seconds = min(backoff_seconds * 2.0, 60.0)
                if once:
                    break

            except APIServerError as e:
                logger.error("api_server_error", err=str(e))
                await asyncio.sleep(interval_seconds)
                if once:
                    break

            except APIClientError as e:
                logger.error("api_client_error", err=str(e))
                await asyncio.sleep(interval_seconds)
                if once:
                    break

            except Exception as e:
                logger.error("live_loop_unexpected_error", err=str(e))
                await asyncio.sleep(interval_seconds)
                if once:
                    break
    finally:
        await client.aclose()

    return 0


async def _amain() -> int:
    setup_logging()
    parser = argparse.ArgumentParser(description="Phase 3 - Live fixtures loop (/fixtures?live=all)")
    parser.add_argument("--interval", type=int, default=15, help="Polling interval in seconds (default: 15)")
    parser.add_argument("--once", action="store_true", help="Run once and exit (for testing)")
    parser.add_argument("--dry-run", action="store_true", help="No DB writes (still calls API)")
    parser.add_argument(
        "--tracked-leagues",
        type=str,
        default=None,
        help="Comma-separated league IDs to track (overrides config/jobs/live.yaml)",
    )
    args = parser.parse_args()

    # Respect API update frequency: never less than 15 seconds
    interval = max(15, int(args.interval))
    if args.tracked_leagues:
        tracked = {int(x.strip()) for x in args.tracked_leagues.split(",") if x.strip()}
        logger.info("tracked_leagues_override", tracked_league_ids=sorted(tracked))
        # small hack: reuse run_live_loop but with config override by temporarily monkey-patching default loader
        # simplest: run one loop with custom tracked list by calling run_iteration via an inline wrapper
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
        try:
            redis_client = create_redis_client_from_env()
            try:
                redis_client.ping()
            except redis_lib.exceptions.RedisError:
                logger.warning("redis_ping_failed_fail_open")
            detector = DeltaDetector(redis_client, ttl_seconds=7200)
        except Exception as e:
            logger.warning("redis_init_failed_fail_open", err=str(e))
            redis_client = create_redis_client_from_env()
            detector = DeltaDetector(redis_client, ttl_seconds=7200)

        running = True

        def _stop() -> None:
            nonlocal running
            logger.info("shutdown_signal_received")
            running = False

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _stop)
            except NotImplementedError:
                signal.signal(sig, lambda *_: _stop())

        backoff_seconds = 1.0
        try:
            while running:
                try:
                    stats = await run_iteration(
                        client=client,
                        limiter=limiter,
                        detector=detector,
                        tracked_leagues=tracked,
                        dry_run=args.dry_run,
                    )
                    logger.info(
                        "live_loop_iteration",
                        fixtures_live=stats.fixtures_live,
                        fixtures_tracked=stats.fixtures_tracked,
                        changed=stats.fixtures_changed,
                        written=stats.fixtures_written,
                        daily_remaining=stats.api_daily_remaining,
                        minute_remaining=stats.api_minute_remaining,
                        interval_seconds=interval,
                    )
                    backoff_seconds = 1.0
                    if args.once:
                        break
                    await asyncio.sleep(interval)
                except EmergencyStopError as e:
                    logger.error("emergency_stop_daily_quota_low", err=str(e))
                    break
                except RateLimitError as e:
                    sleep_s = min(backoff_seconds, 60.0)
                    logger.warning("api_rate_limited_backoff", err=str(e), sleep_seconds=sleep_s)
                    await asyncio.sleep(sleep_s)
                    backoff_seconds = min(backoff_seconds * 2.0, 60.0)
                    if args.once:
                        break
                except APIServerError as e:
                    logger.error("api_server_error", err=str(e))
                    await asyncio.sleep(interval)
                    if args.once:
                        break
                except APIClientError as e:
                    logger.error("api_client_error", err=str(e))
                    await asyncio.sleep(interval)
                    if args.once:
                        break
                except Exception as e:
                    logger.error("live_loop_unexpected_error", err=str(e))
                    await asyncio.sleep(interval)
                    if args.once:
                        break
        finally:
            await client.aclose()
        return 0

    return await run_live_loop(interval_seconds=interval, once=args.once, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_amain()))


