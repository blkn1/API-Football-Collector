from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg2.extras
import yaml

from src.collector.api_client import APIClient, APIClientError, APIResult, RateLimitError
from src.collector.rate_limiter import RateLimiter
from src.transforms.standings import transform_standings
from src.utils.db import get_transaction, query_scalar, upsert_raw
from src.utils.logging import get_logger
from src.utils.dependencies import ensure_standings_dependencies, get_missing_team_ids_in_core
from src.utils.scope_policy import decide_scope, get_league_types_map


logger = get_logger(component="standings_sync")


@dataclass(frozen=True)
class TrackedLeague:
    id: int
    name: str | None = None
    season: int | None = None


@dataclass(frozen=True)
class StandingsSyncSummary:
    leagues: list[int]
    api_requests: int
    total_rows: int
    daily_remaining: int | None
    minute_remaining: int | None


def _load_config(config_path: Path) -> list[TrackedLeague]:
    cfg = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    default_season = cfg.get("season")
    tracked_leagues = cfg.get("tracked_leagues")

    leagues: list[TrackedLeague] = []
    if isinstance(tracked_leagues, list):
        for x in tracked_leagues:
            if not isinstance(x, dict) or "id" not in x:
                continue
            s = x.get("season")
            if s is None:
                s = default_season
            leagues.append(TrackedLeague(id=int(x["id"]), name=x.get("name"), season=(int(s) if s is not None else None)))

    # fallback: infer from jobs config
    if not leagues:
        jobs = cfg.get("jobs") or []
        for j in jobs:
            if not isinstance(j, dict):
                continue
            if j.get("endpoint") != "/standings":
                continue
            params = j.get("params") or {}
            league_id = params.get("league")
            job_season = params.get("season")
            if league_id is None:
                continue
            s = job_season if job_season is not None else default_season
            leagues.append(TrackedLeague(id=int(league_id), name=j.get("job_id"), season=(int(s) if s is not None else None)))

    if not leagues:
        raise ValueError(f"No tracked leagues configured in {config_path}")

    missing = [l.id for l in leagues if l.season is None]
    if missing:
        raise ValueError(f"Missing season for leagues={missing} in config: {config_path}")

    return leagues


def _replace_standings(conn, *, league_id: int, season: int, rows: list[dict[str, Any]]) -> None:
    # Delete then insert inside one transaction
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM core.standings WHERE league_id = %s AND season = %s",
            (league_id, season),
        )
        if not rows:
            return

        cols = list(rows[0].keys())
        values_raw = [tuple(r[c] for c in cols) for r in rows]
        # Adapt JSONB dict/list for psycopg2
        values: list[tuple[Any, ...]] = []
        for row in values_raw:
            adapted: list[Any] = []
            for v in row:
                if isinstance(v, (dict, list)):
                    adapted.append(psycopg2.extras.Json(v))
                else:
                    adapted.append(v)
            values.append(tuple(adapted))

        insert_cols = ", ".join(cols)
        stmt = f"INSERT INTO core.standings ({insert_cols}) VALUES %s"
        psycopg2.extras.execute_values(cur, stmt, values)


async def sync_standings(
    *,
    league_filter: int | None = None,
    dry_run: bool = False,
    config_path: Path,
    client: APIClient | None = None,
    limiter: RateLimiter | None = None,
    max_leagues_per_run: int | None = None,
    progress_job_id: str = "daily_standings",
) -> StandingsSyncSummary:
    """
    Sync standings for tracked leagues from config:
      GET /standings?league={id}&season={season}

    - Rate limit enforced per call
    - RAW archived (unless dry-run)
    - CORE standings replaced fully per league+season (delete-then-insert in one transaction)
    """
    tracked = _load_config(config_path)

    leagues = tracked
    if league_filter is not None:
        leagues = [x for x in leagues if x.id == int(league_filter)]
        if not leagues:
            raise ValueError(f"League {league_filter} not found in config: {config_path}")

    # Scope policy: cups are typically out-of-scope for standings. Skip out-of-scope league-season pairs.
    # Safety: if league type is unknown, we FAIL OPEN (will run standings) to avoid accidental data loss.
    scoped_in: list[TrackedLeague] = []
    skipped: list[dict[str, Any]] = []
    types_map = get_league_types_map([int(x.id) for x in leagues])
    for l in leagues:
        d = decide_scope(
            league_id=int(l.id),
            season=int(l.season or 0),
            endpoint="/standings",
            league_type_provider=lambda lid: types_map.get(int(lid)),
        )
        if d.in_scope:
            scoped_in.append(l)
        else:
            skipped.append({"league_id": int(l.id), "season": int(l.season or 0), "reason": d.reason, "policy_version": d.policy_version})
    if skipped:
        logger.info(
            "scope_policy_skipped_pairs",
            endpoint="/standings",
            skipped_count=len(skipped),
            examples=skipped[:10],
        )
    leagues = scoped_in

    if league_filter is None and not leagues:
        # If the entire tracked set is out-of-scope for standings, this is not an error.
        logger.info("standings_sync_skipped", reason="all_pairs_out_of_scope", endpoint="/standings")
        return StandingsSyncSummary(leagues=[], api_requests=0, total_rows=0, daily_remaining=None, minute_remaining=None)

    # Optional batching: process only N leagues per run and advance a cursor in CORE.
    # - If league_filter is set, batching is bypassed (explicit run for a single league).
    # - If max_leagues_per_run is None, process all (legacy behavior).
    # - Progress is stored in core.standings_refresh_progress keyed by progress_job_id.
    async def _load_progress() -> tuple[int, int]:
        try:
            row = query_scalar(
                "SELECT json_build_object('cursor', cursor, 'lap_count', lap_count) "
                "FROM core.standings_refresh_progress WHERE job_id=%s",
                (str(progress_job_id),),
            )
            # query_scalar may return a dict (psycopg2 JSON) or a string; be defensive
            if isinstance(row, dict):
                cursor = int(row.get("cursor") or 0)
                lap = int(row.get("lap_count") or 0)
                return cursor, lap
            return 0, 0
        except Exception:
            return 0, 0

    def _save_progress(
        *,
        cursor: int,
        total_pairs: int,
        cursor_before: int,
        lap_count: int,
        last_error: str | None = None,
    ) -> None:
        if dry_run:
            return
        # Wrap detection: if cursor moved "backwards" in modular space, we completed a full pass.
        wrapped = int(cursor) < int(cursor_before)
        lap_next = int(lap_count) + (1 if wrapped else 0)
        with get_transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO core.standings_refresh_progress
                      (job_id, cursor, total_pairs, last_run_at, last_error, lap_count, last_full_pass_at)
                    VALUES (%s, %s, %s, NOW(), %s, %s, CASE WHEN %s THEN NOW() ELSE NULL END)
                    ON CONFLICT (job_id) DO UPDATE
                      SET cursor = EXCLUDED.cursor,
                          total_pairs = EXCLUDED.total_pairs,
                          last_run_at = EXCLUDED.last_run_at,
                          last_error = EXCLUDED.last_error,
                          lap_count = EXCLUDED.lap_count,
                          last_full_pass_at = COALESCE(EXCLUDED.last_full_pass_at, core.standings_refresh_progress.last_full_pass_at)
                    """,
                    (str(progress_job_id), int(cursor), int(total_pairs), last_error, int(lap_next), bool(wrapped)),
                )

    def _batch(leagues_in: list[TrackedLeague]) -> tuple[list[TrackedLeague], dict[str, Any]]:
        # Stable order so cursor is deterministic across runs.
        ordered = sorted(leagues_in, key=lambda x: (int(x.season or 0), int(x.id)))
        total = len(ordered)
        if total == 0:
            return [], {"total_pairs": 0, "cursor_before": 0, "cursor_after": 0, "batch_size": 0}

        if max_leagues_per_run is None or max_leagues_per_run <= 0 or max_leagues_per_run >= total:
            return ordered, {"total_pairs": total, "cursor_before": 0, "cursor_after": 0, "batch_size": total, "mode": "all"}

        cursor_before = 0
        try:
            cursor_before = int(_cursor_value)
        except Exception:
            cursor_before = 0
        start = cursor_before % total
        n = int(max_leagues_per_run)
        batch_list = (ordered[start : start + n] + ordered[0 : max(0, (start + n) - total)])[:n]
        cursor_after = (start + len(batch_list)) % total
        return batch_list, {
            "total_pairs": total,
            "cursor_before": cursor_before,
            "cursor_after": cursor_after,
            "batch_size": len(batch_list),
            "mode": "cursor",
        }

    _cursor_value = 0
    _lap_count = 0
    batch_meta: dict[str, Any] = {"mode": "all"}
    if league_filter is None and max_leagues_per_run is not None:
        _cursor_value, _lap_count = await _load_progress()
        leagues, batch_meta = _batch(leagues)
        batch_meta["lap_count_before"] = int(_lap_count)

    limiter2 = limiter or RateLimiter(max_tokens=300, refill_rate=5.0)
    client2 = client or APIClient()

    api_requests = 0
    total_rows = 0

    logger.info(
        "standings_sync_started",
        dry_run=dry_run,
        leagues=[{"league_id": l.id, "season": int(l.season or 0)} for l in leagues],
        batch=batch_meta,
    )

    try:
        last_error: str | None = None
        for l in leagues:
            league_id = l.id
            league_name = l.name or f"League {league_id}"
            season = int(l.season or 0)
            params = {"league": league_id, "season": season}
            logger.info("league_standings_started", league_id=league_id, league_name=league_name, season=season)

            try:
                limiter2.acquire_token()
                result: APIResult = await client2.get("/standings", params=params)
                api_requests += 1
                limiter2.update_from_headers(result.headers)
            except RateLimitError as e:
                logger.warning("api_rate_limited", league_id=league_id, err=str(e), sleep_seconds=5)
                await asyncio.sleep(5)
                continue
            except APIClientError as e:
                logger.error("api_call_failed", league_id=league_id, err=str(e))
                last_error = str(e)
                continue
            except Exception as e:
                logger.error("api_call_unexpected_error", league_id=league_id, err=str(e))
                last_error = str(e)
                continue

            envelope = result.data or {}

            # Ensure league+teams exist before FK writes into core.standings.
            try:
                await ensure_standings_dependencies(
                    league_id=league_id,
                    season=season,
                    standings_envelope=envelope,
                    client=client2,
                    limiter=limiter2,
                )
            except Exception as e:
                logger.error("dependency_bootstrap_failed", league_id=league_id, err=str(e))
                last_error = str(e)
                continue

            if not dry_run:
                upsert_raw(
                    endpoint="/standings",
                    requested_params=params,
                    status_code=result.status_code,
                    response_headers=result.headers,
                    body=envelope,
                )
                logger.info("raw_stored", league_id=league_id)

            rows = transform_standings(envelope)
            total_rows += len(rows)

            if dry_run:
                logger.info("core_skipped_dry_run", league_id=league_id, rows=len(rows))
                continue

            # Safety guard: if FK targets (core.teams) are missing, skip replace to avoid
            # transaction failure (delete-then-insert would otherwise rollback).
            team_ids = {int(r["team_id"]) for r in rows if r.get("team_id") is not None}
            missing = get_missing_team_ids_in_core(team_ids)
            if missing:
                logger.error(
                    "standings_missing_teams_skip_replace",
                    league_id=league_id,
                    season=season,
                    missing_team_ids_count=len(missing),
                    missing_team_ids_sample=sorted(list(missing))[:25],
                )
                last_error = f"missing_teams:{len(missing)}"
                continue

            try:
                with get_transaction() as conn:
                    _replace_standings(conn, league_id=league_id, season=season, rows=rows)
                count = query_scalar(
                    "SELECT COUNT(*) FROM core.standings WHERE league_id=%s AND season=%s",
                    (league_id, season),
                )
                logger.info("core_replaced", league_id=league_id, rows_inserted=len(rows), rows_in_db=int(count or 0))
            except Exception as e:
                logger.error("db_replace_failed", league_id=league_id, err=str(e))
                last_error = str(e)
                continue

            q = limiter2.quota
            logger.info("quota_snapshot", league_id=league_id, daily_remaining=q.daily_remaining, minute_remaining=q.minute_remaining)

        q = limiter2.quota
        logger.info(
            "standings_sync_complete",
            api_requests=api_requests,
            total_rows=total_rows,
            daily_remaining=q.daily_remaining,
            minute_remaining=q.minute_remaining,
        )

        # Advance progress cursor after successful/attempted run (best-effort).
        try:
            if league_filter is None and max_leagues_per_run is not None and batch_meta.get("mode") == "cursor":
                _save_progress(
                    cursor=int(batch_meta.get("cursor_after") or 0),
                    total_pairs=int(batch_meta.get("total_pairs") or 0),
                    cursor_before=int(batch_meta.get("cursor_before") or 0),
                    lap_count=int(batch_meta.get("lap_count_before") or 0),
                    last_error=last_error,
                )
        except Exception:
            pass

        return StandingsSyncSummary(
            leagues=[l.id for l in leagues],
            api_requests=api_requests,
            total_rows=total_rows,
            daily_remaining=q.daily_remaining,
            minute_remaining=q.minute_remaining,
        )
    finally:
        if client is None:
            await client2.aclose()


