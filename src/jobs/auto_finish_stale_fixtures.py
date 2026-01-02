from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from src.collector.api_client import APIClient, APIClientError, APIResult, RateLimitError
from src.collector.rate_limiter import EmergencyStopError, RateLimiter
from src.transforms.fixtures import transform_fixtures
from src.utils.db import get_transaction, upsert_core, upsert_raw
from src.utils.dependencies import ensure_fixtures_dependencies
from src.utils.logging import get_logger


logger = get_logger(component="jobs_auto_finish_stale_fixtures")

# Fixtures in "live" or intermediate states that should have finished.
# These can be safely auto-finished if they're stale.
STALE_STATUSES = ("NS", "HT", "2H", "1H", "LIVE", "BT", "ET", "P", "SUSP", "INT")

# Final statuses we won't auto-finish (already finished or abandoned)
FINAL_STATUSES = ("FT", "AET", "PEN", "AWD", "WO", "ABD", "CANC", "PST")


@dataclass(frozen=True)
class AutoFinishConfig:
    threshold_hours: int
    safety_lag_hours: int
    max_fixtures_per_run: int
    scoped_league_ids: set[int]
    dry_run: bool
    try_fetch_first: bool


def _load_daily_tracked_league_ids(cfg: dict[str, Any], *, config_path: Path) -> set[int]:
    tracked_raw = cfg.get("tracked_leagues") or []
    tracked: set[int] = set()
    if isinstance(tracked_raw, list):
        for x in tracked_raw:
            if not isinstance(x, dict) or x.get("id") is None:
                continue
            try:
                tracked.add(int(x["id"]))
            except Exception:
                continue
    if not tracked:
        raise ValueError(f"Missing tracked_leagues in {config_path}")
    return tracked


def _load_config(config_path: Path) -> AutoFinishConfig:
    cfg = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}

    # defaults (safe + conservative)
    threshold = 2  # 2 hours after kickoff, treat as stale
    safety_lag = 3  # 3 hours since last update (safety check)
    max_fixtures = 1000
    dry_run = False
    try_fetch_first = False  # Default: DB-only (no API calls) to maintain current behavior

    for j in cfg.get("jobs") or []:
        if not isinstance(j, dict):
            continue
        if str(j.get("job_id") or "") != "auto_finish_stale_fixtures":
            continue
        params = j.get("params") or {}
        if isinstance(params, dict):
            try:
                if params.get("threshold_hours") is not None:
                    threshold = int(params.get("threshold_hours"))
            except Exception:
                pass
            try:
                if params.get("safety_lag_hours") is not None:
                    safety_lag = int(params.get("safety_lag_hours"))
            except Exception:
                pass
            try:
                if params.get("max_fixtures_per_run") is not None:
                    max_fixtures = int(params.get("max_fixtures_per_run"))
            except Exception:
                pass
            try:
                if params.get("dry_run") is not None:
                    dry_run = bool(params.get("dry_run"))
            except Exception:
                pass
            try:
                if params.get("try_fetch_first") is not None:
                    try_fetch_first = bool(params.get("try_fetch_first"))
            except Exception:
                pass
        break

    # Guardrails
    threshold = max(1, min(int(threshold), 7 * 24))  # 1h .. 7d
    safety_lag = max(1, min(int(safety_lag), 7 * 24))  # 1h .. 7d
    max_fixtures = max(1, min(int(max_fixtures), 10000))

    scoped = _load_daily_tracked_league_ids(cfg, config_path=config_path)

    return AutoFinishConfig(
        threshold_hours=threshold,
        safety_lag_hours=safety_lag,
        max_fixtures_per_run=max_fixtures,
        scoped_league_ids=scoped,
        dry_run=dry_run,
        try_fetch_first=try_fetch_first,
    )


def _select_stale_fixture_ids(
    *,
    threshold_hours: int,
    safety_lag_hours: int,
    limit: int,
    tracked_league_ids: set[int] | None = None,
) -> list[int]:
    """
    Select fixtures that are in stale intermediate states but haven't been updated recently.

    We use a double-threshold safety check:
    1. date < NOW() - threshold_hours: The fixture was scheduled to start N hours ago
    2. updated_at < NOW() - safety_lag_hours: The fixture hasn't been updated in M hours

    This prevents accidentally finishing a live match that's been recently updated.
    """
    sql = """
    SELECT f.id, f.league_id, f.status_short, f.date, f.updated_at
    FROM core.fixtures f
    WHERE f.league_id = ANY(%s)
      AND f.status_short = ANY(%s)
      AND f.date < NOW() - (%s::text || ' hours')::interval
      AND f.updated_at < NOW() - (%s::text || ' hours')::interval
    ORDER BY f.date ASC
    LIMIT %s
    """
    with get_transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                sql,
                (
                    sorted(list(tracked_league_ids or [])),
                    list(STALE_STATUSES),
                    str(threshold_hours),
                    str(safety_lag_hours),
                    int(limit),
                ),
            )
            rows = cur.fetchall()
            conn.commit()
    return [int(r[0]) for r in rows]


def _chunk(ids: list[int], *, size: int) -> list[list[int]]:
    """Split list into chunks of specified size."""
    if size <= 0:
        return [ids]
    return [ids[i : i + size] for i in range(0, len(ids), size)]


async def _try_fetch_fixtures_batch_from_api(
    *,
    fixture_ids: list[int],
    client: APIClient,
    limiter: RateLimiter,
) -> dict[int, dict[str, Any]]:
    """
    Batch fetch fixtures from API.
    
    Returns dict mapping fixture_id -> transformed_fixture_data for successful fetches.
    Returns empty dict if API call fails (quota, network, etc.).
    """
    fetched_data: dict[int, dict[str, Any]] = {}
    
    for batch in _chunk(fixture_ids, size=20):
        ids_param = "-".join(str(int(x)) for x in batch)
        params = {"ids": ids_param}
        label = f"/fixtures(ids={ids_param})"
        
        try:
            limiter.acquire_token()
            res = await client.get("/fixtures", params=params)
            limiter.update_from_headers(res.headers)
            env = res.data or {}
            errors = env.get("errors") or {}
            
            if errors:
                logger.warning("auto_finish_api_errors", label=label, errors=errors)
                continue
                
            # Transform and store by fixture_id
            fixtures_rows, _ = transform_fixtures(env)
            for row in fixtures_rows:
                fetched_data[row["id"]] = row
                
        except EmergencyStopError as e:
            logger.warning("auto_finish_emergency_stop", err=str(e))
            break
        except RateLimitError as e:
            logger.warning("auto_finish_rate_limited", err=str(e))
            await asyncio.sleep(5)
            continue
        except (APIClientError, RuntimeError) as e:
            logger.warning("auto_finish_api_failed", err=str(e), ids=len(batch))
            continue
    
    return fetched_data


def _auto_finish_fixtures(
    *,
    fixture_ids: list[int],
    dry_run: bool,
    fetched_data: dict[int, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """
    Update stale fixtures to FT status.
    
    If fetched_data is provided (from API), UPSERT with fresh data and set needs_score_verification = FALSE.
    Otherwise, update status directly and set needs_score_verification = TRUE.

    Returns summary statistics including leagues affected.
    """
    if not fixture_ids:
        return {"updated_count": 0, "leagues_affected": 0}

    if dry_run:
        logger.info("auto_finish_dry_run", fixture_count=len(fixture_ids))
        return {"updated_count": 0, "leagues_affected": 0, "dry_run": True}

    fetched_ids = set(fetched_data.keys()) if fetched_data else set()
    missing_ids = [fid for fid in fixture_ids if fid not in fetched_ids]

    updated_count = 0
    leagues_affected_set: set[int] = set()

    # Update fixtures with fresh API data
    if fetched_data:
        for fixture_id, row in fetched_data.items():
            try:
                with get_transaction() as conn:
                    upsert_core(
                        full_table_name="core.fixtures",
                        rows=[row],
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
                    # Set needs_score_verification = FALSE for successfully fetched fixtures
                    with conn.cursor() as cur:
                        cur.execute(
                            "UPDATE core.fixtures SET needs_score_verification = FALSE WHERE id = %s",
                            (fixture_id,),
                        )
                    conn.commit()
                updated_count += 1
                leagues_affected_set.add(row["league_id"])
            except Exception as e:
                logger.error("auto_finish_upsert_failed", fixture_id=fixture_id, err=str(e))
                # Fallback: mark as needing verification
                missing_ids.append(fixture_id)

    # Update missing/failed fixtures with current score + verification flag
    if missing_ids:
        sql = """
        UPDATE core.fixtures
        SET status_short = 'FT',
            status_long = 'Match Finished (Auto-finished)',
            elapsed = 90,
            score = jsonb_set(
              COALESCE(score, '{}'::jsonb),
              '{fulltime}',
              jsonb_build_object('home', goals_home, 'away', goals_away),
              true
            ),
            needs_score_verification = TRUE,
            updated_at = NOW()
        WHERE id = ANY(%s)
        RETURNING id, league_id, season, status_short, date, updated_at
        """
        with get_transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (list(missing_ids),))
                rows = cur.fetchall()
                conn.commit()
        updated_count += len(rows)
        leagues_affected_set.update(int(r[1]) for r in rows)

    return {
        "updated_count": updated_count,
        "leagues_affected": len(leagues_affected_set),
        "dry_run": False,
        "fetched_from_api": len(fetched_ids),
        "marked_for_verification": len(missing_ids),
    }


async def run_auto_finish_stale_fixtures(
    *,
    config_path: Path,
    client: APIClient | None = None,
    limiter: RateLimiter | None = None,
) -> None:
    """
    Maintenance job:
    - Find fixtures in stale intermediate states (NS, HT, 2H, 1H, LIVE, BT, ET, P, SUSP, INT)
    - Apply double-threshold safety check (date < N hours ago AND updated_at < M hours ago)
    - If try_fetch_first=True and API available: batch fetch fresh data, UPSERT with verification flag = FALSE
    - Otherwise: Update status to FT directly in database, set verification flag = TRUE
    - Log statistics

    This job is safe to run because:
    1. It only affects tracked leagues
    2. It uses two independent time thresholds
    3. It's transaction-wrapped (rollback on error)
    4. It respects max_fixtures_per_run limit
    5. API fetch is opt-in via config (default: False)
    """
    cfg = _load_config(config_path)

    stale_ids = _select_stale_fixture_ids(
        threshold_hours=cfg.threshold_hours,
        safety_lag_hours=cfg.safety_lag_hours,
        limit=cfg.max_fixtures_per_run,
        tracked_league_ids=cfg.scoped_league_ids,
    )

    if not stale_ids:
        logger.info(
            "auto_finish_no_work",
            threshold_hours=cfg.threshold_hours,
            safety_lag_hours=cfg.safety_lag_hours,
            scoped_leagues=len(cfg.scoped_league_ids),
            dry_run=cfg.dry_run,
        )
        return

    fetched_data: dict[int, dict[str, Any]] = {}
    
    # Try to fetch fresh data if enabled and API available
    if cfg.try_fetch_first and client is not None and limiter is not None:
        try:
            fetched_data = await _try_fetch_fixtures_batch_from_api(
                fixture_ids=stale_ids,
                client=client,
                limiter=limiter,
            )
            logger.info(
                "auto_finish_fetch_attempt",
                total=len(stale_ids),
                fetched=len(fetched_data),
                failed=len(stale_ids) - len(fetched_data),
            )
        except Exception as e:
            logger.warning("auto_finish_fetch_error", err=str(e))
            # Continue with DB-only update for all fixtures

    result = _auto_finish_fixtures(
        fixture_ids=stale_ids,
        dry_run=cfg.dry_run,
        fetched_data=fetched_data if fetched_data else None,
    )

    logger.info(
        "auto_finish_complete",
        threshold_hours=cfg.threshold_hours,
        safety_lag_hours=cfg.safety_lag_hours,
        selected=len(stale_ids),
        updated_count=result["updated_count"],
        leagues_affected=result["leagues_affected"],
        fetched_from_api=result.get("fetched_from_api", 0),
        marked_for_verification=result.get("marked_for_verification", 0),
        dry_run=cfg.dry_run,
    )
