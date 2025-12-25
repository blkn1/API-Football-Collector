from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timezone
from pathlib import Path
from typing import Any

import yaml

from src.collector.api_client import APIClient, APIClientError, APIResult, RateLimitError
from src.collector.rate_limiter import EmergencyStopError, RateLimiter
from src.transforms.fixtures import transform_fixtures
from src.utils.db import get_db_connection, get_transaction, upsert_core, upsert_raw
from src.utils.dependencies import ensure_fixtures_dependencies
from src.utils.logging import get_logger


logger = get_logger(component="jobs_stale_scheduled_finalize")

# Fixtures that are "scheduled" but should have transitioned to a final status.
SCHEDULED_STATUSES = ("NS", "TBD")


@dataclass(frozen=True)
class StaleScheduledConfig:
    threshold_minutes: int
    lookback_days: int
    batch_size: int
    max_fixtures_per_run: int
    scoped_league_ids: set[int]


def _chunk(ids: list[int], *, size: int) -> list[list[int]]:
    if size <= 0:
        return [ids]
    return [ids[i : i + size] for i in range(0, len(ids), size)]


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


def _load_config(config_path: Path) -> StaleScheduledConfig:
    cfg = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}

    # defaults (safe + conservative)
    threshold = 180  # 3 hours after scheduled kickoff window, treat NS/TBD as stale
    lookback_days = 3
    batch = 20
    max_fixtures = 200

    for j in cfg.get("jobs") or []:
        if not isinstance(j, dict):
            continue
        if str(j.get("job_id") or "") != "stale_scheduled_finalize":
            continue
        params = j.get("params") or {}
        if isinstance(params, dict):
            try:
                if params.get("stale_threshold_minutes") is not None:
                    threshold = int(params.get("stale_threshold_minutes"))
            except Exception:
                pass
            try:
                if params.get("lookback_days") is not None:
                    lookback_days = int(params.get("lookback_days"))
            except Exception:
                pass
            try:
                if params.get("batch_size") is not None:
                    batch = int(params.get("batch_size"))
            except Exception:
                pass
            try:
                if params.get("max_fixtures_per_run") is not None:
                    max_fixtures = int(params.get("max_fixtures_per_run"))
            except Exception:
                pass
        break

    # Guardrails
    threshold = max(30, min(int(threshold), 7 * 24 * 60))  # 30m .. 7d
    lookback_days = max(1, min(int(lookback_days), 14))
    batch = max(1, min(int(batch), 20))  # API-Football /fixtures ids max 20
    max_fixtures = max(1, min(int(max_fixtures), 2000))

    scoped = _load_daily_tracked_league_ids(cfg, config_path=config_path)

    return StaleScheduledConfig(
        threshold_minutes=threshold,
        lookback_days=lookback_days,
        batch_size=batch,
        max_fixtures_per_run=max_fixtures,
        scoped_league_ids=scoped,
    )


def _select_stale_scheduled_fixture_ids(
    *,
    threshold_minutes: int,
    lookback_days: int,
    limit: int,
    tracked_league_ids: set[int],
) -> list[int]:
    """
    Select fixtures that are still NS/TBD even though their kickoff time is in the past.
    We scope to tracked leagues to keep quota bounded.
    """
    sql = """
    SELECT f.id
    FROM core.fixtures f
    WHERE f.league_id = ANY(%s)
      AND f.status_short = ANY(%s)
      AND f.date < NOW() - make_interval(mins => %s)
      AND f.date >= NOW() - (%s::text || ' days')::interval
    ORDER BY f.date ASC
    LIMIT %s
    """
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                sql,
                (sorted(list(tracked_league_ids)), list(SCHEDULED_STATUSES), int(threshold_minutes), int(lookback_days), int(limit)),
            )
            rows = cur.fetchall()
        conn.commit()
    return [int(r[0]) for r in rows]


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


async def run_stale_scheduled_finalize(*, client: APIClient, limiter: RateLimiter, config_path: Path) -> None:
    """
    Maintenance job:
    - Find fixtures that are still NS/TBD but whose fixture.date is in the past (stale scheduled).
    - Refetch them in batches using GET /fixtures?ids=<id1>-<id2>-... (max 20).
    - Write RAW and UPSERT CORE fixtures (status_short/status_long/goals/elapsed) and fixture_details snapshot.
    """
    cfg = _load_config(config_path)
    stale_ids = _select_stale_scheduled_fixture_ids(
        threshold_minutes=cfg.threshold_minutes,
        lookback_days=cfg.lookback_days,
        limit=cfg.max_fixtures_per_run,
        tracked_league_ids=cfg.scoped_league_ids,
    )
    if not stale_ids:
        logger.info(
            "stale_scheduled_finalize_no_work",
            threshold_minutes=cfg.threshold_minutes,
            lookback_days=cfg.lookback_days,
            scoped_leagues=len(cfg.scoped_league_ids),
        )
        return

    total_requests = 0
    fixtures_upserted = 0

    for batch in _chunk(stale_ids, size=cfg.batch_size):
        ids_param = "-".join(str(int(x)) for x in batch)
        params = {"ids": ids_param}
        label = f"/fixtures(ids={ids_param})"

        try:
            res, env = await _safe_get_envelope(client=client, limiter=limiter, endpoint="/fixtures", params=params, label=label)
            total_requests += 1
        except EmergencyStopError as e:
            logger.error("emergency_stop_daily_quota_low", job="stale_scheduled_finalize", err=str(e))
            break
        except RateLimitError as e:
            logger.warning("api_rate_limited_429", job="stale_scheduled_finalize", err=str(e), sleep_seconds=5)
            await asyncio.sleep(5)
            continue
        except (APIClientError, RuntimeError) as e:
            logger.error("stale_scheduled_finalize_api_failed", err=str(e), ids=len(batch))
            continue

        upsert_raw(
            endpoint="/fixtures",
            requested_params=params,
            status_code=res.status_code,
            response_headers=res.headers,
            body=env,
        )

        # FK guard: ensure leagues/teams/venues exist before upserting fixtures.
        # Group by (league_id, season) because ensure_fixtures_dependencies is league+season scoped.
        try:
            grouped: dict[tuple[int, int], list[dict[str, Any]]] = {}
            for it in env.get("response") or []:
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
                    fixtures_envelope={**env, "response": items},
                    client=client,
                    limiter=limiter,
                    log_venues=False,
                )
        except Exception as e:
            logger.error("stale_scheduled_finalize_dependency_failed", err=str(e), ids=len(batch))
            continue

        try:
            fixtures_rows, details_rows = transform_fixtures(env)
        except Exception as e:
            logger.error("stale_scheduled_finalize_transform_failed", err=str(e), ids=len(batch))
            continue

        try:
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
                        update_cols=["events", "lineups", "statistics", "players", "updated_at"],
                        conn=conn,
                    )
            fixtures_upserted += len(fixtures_rows)
        except Exception as e:
            logger.error("stale_scheduled_finalize_db_failed", err=str(e), ids=len(batch))
            continue

    logger.info(
        "stale_scheduled_finalize_complete",
        selected=len(stale_ids),
        requests=int(total_requests),
        fixtures_upserted=int(fixtures_upserted),
        threshold_minutes=cfg.threshold_minutes,
        lookback_days=cfg.lookback_days,
    )


