from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from src.collector.api_client import APIClient, APIClientError, APIResult, RateLimitError
from src.collector.rate_limiter import EmergencyStopError, RateLimiter
from src.jobs.fixture_details import _fetch_and_store_fixture_details, _missing_detail_endpoints_for_fixture
from src.transforms.fixtures import transform_fixtures
from src.utils.db import get_transaction, upsert_core, upsert_raw
from src.utils.dependencies import ensure_fixtures_dependencies
from src.utils.logging import get_logger


logger = get_logger(component="jobs_auto_finish_verification")


@dataclass(frozen=True)
class AutoFinishVerificationConfig:
    min_daily_quota: int
    batch_size: int
    max_fixtures_per_run: int
    scoped_league_ids: set[int]


def _chunk(ids: list[int], *, size: int) -> list[list[int]]:
    """Split list into chunks of specified size."""
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


def _load_config(config_path: Path) -> AutoFinishVerificationConfig:
    cfg = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}

    # defaults
    min_daily_quota = 50000
    batch_size = 20
    max_fixtures = 200

    for j in cfg.get("jobs") or []:
        if not isinstance(j, dict):
            continue
        if str(j.get("job_id") or "") != "auto_finish_verification":
            continue
        params = j.get("params") or {}
        if isinstance(params, dict):
            try:
                if params.get("min_daily_quota") is not None:
                    min_daily_quota = int(params.get("min_daily_quota"))
            except Exception:
                pass
            try:
                if params.get("batch_size") is not None:
                    batch_size = int(params.get("batch_size"))
            except Exception:
                pass
            try:
                if params.get("max_fixtures_per_run") is not None:
                    max_fixtures = int(params.get("max_fixtures_per_run"))
            except Exception:
                pass
        break

    # Guardrails
    min_daily_quota = max(1000, min(int(min_daily_quota), 100000))
    batch_size = max(1, min(int(batch_size), 20))
    max_fixtures = max(1, min(int(max_fixtures), 10000))

    scoped = _load_daily_tracked_league_ids(cfg, config_path=config_path)

    return AutoFinishVerificationConfig(
        min_daily_quota=min_daily_quota,
        batch_size=batch_size,
        max_fixtures_per_run=max_fixtures,
        scoped_league_ids=scoped,
    )


def _select_verification_fixture_ids(
    *,
    limit: int,
    tracked_league_ids: set[int],
) -> list[int]:
    """
    Select fixtures that need score verification.
    These are auto-finished matches that were marked with needs_score_verification = TRUE.
    
    Includes:
    - Fixtures never attempted (updated_at is old, from auto-finish time)
    - Fixtures attempted 24+ hours ago (retry after cooldown)
    
    Excludes fixtures attempted in last 24 hours (cooldown period to avoid
    clearing flag too aggressively for temporary API issues).
    """
    sql = """
    SELECT f.id
    FROM core.fixtures f
    WHERE f.league_id = ANY(%s)
      AND f.needs_score_verification = TRUE
      AND f.status_short = 'FT'
      -- Include: never attempted (old updated_at) OR attempted 24+ hours ago
      -- Exclude: attempted in last 24 hours (cooldown)
      AND (f.updated_at < NOW() - INTERVAL '24 hours')
    ORDER BY f.date DESC
    LIMIT %s
    """
    with get_transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                sql,
                (sorted(list(tracked_league_ids)), int(limit)),
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
    """Safe API call with retry logic."""
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


async def run_auto_finish_verification(
    *,
    client: APIClient,
    limiter: RateLimiter,
    config_path: Path,
) -> None:
    """
    Verification job:
    - Find fixtures where needs_score_verification = TRUE and status_short = 'FT'
    - Batch fetch via GET /fixtures?ids=<id1>-<id2>-... (max 20)
    - UPSERT CORE with fresh data
    - Set needs_score_verification = FALSE on success
    - Only runs when daily_remaining >= min_daily_quota (quota guard)
    """
    cfg = _load_config(config_path)

    # Quota guard: only run when quota is healthy
    quota = limiter.quota
    if quota.daily_remaining < cfg.min_daily_quota:
        logger.info(
            "auto_finish_verification_quota_guard",
            daily_remaining=quota.daily_remaining,
            min_required=cfg.min_daily_quota,
        )
        return

    verification_ids = _select_verification_fixture_ids(
        limit=cfg.max_fixtures_per_run,
        tracked_league_ids=cfg.scoped_league_ids,
    )

    if not verification_ids:
        logger.info(
            "auto_finish_verification_no_work",
            scoped_leagues=len(cfg.scoped_league_ids),
        )
        return

    total_requests = 0
    fixtures_verified = 0
    details_fetched_count = 0
    details_endpoints_fetched = 0

    for batch in _chunk(verification_ids, size=cfg.batch_size):
        ids_param = "-".join(str(int(x)) for x in batch)
        params = {"ids": ids_param}
        label = f"/fixtures(ids={ids_param})"

        try:
            res, env = await _safe_get_envelope(
                client=client,
                limiter=limiter,
                endpoint="/fixtures",
                params=params,
                label=label,
            )
            total_requests += 1
        except EmergencyStopError as e:
            logger.error("emergency_stop_daily_quota_low", job="auto_finish_verification", err=str(e))
            break
        except RateLimitError as e:
            logger.warning("api_rate_limited_429", job="auto_finish_verification", err=str(e), sleep_seconds=5)
            await asyncio.sleep(5)
            continue
        except (APIClientError, RuntimeError) as e:
            logger.error("auto_finish_verification_api_failed", err=str(e), ids=len(batch))
            continue

        upsert_raw(
            endpoint="/fixtures",
            requested_params=params,
            status_code=res.status_code,
            response_headers=res.headers,
            body=env,
        )

        # Log batch fetch result for observability
        response_count = len(env.get("response") or [])
        logger.info(
            "auto_finish_verification_batch_fetched",
            fixture_ids=batch,
            response_count=response_count,
            api_status_code=res.status_code,
        )

        # Ensure dependencies exist (FK integrity) grouped per league+season
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
            logger.error("auto_finish_verification_dependency_failed", err=str(e), ids=len(batch))
            continue

        fixtures_rows, _ = transform_fixtures(env)

        # Handle empty response (fixture not found in API or invalid)
        if not fixtures_rows:
            response_count = len(env.get("response") or [])
            logger.warning(
                "auto_finish_verification_empty_response",
                fixture_ids=batch,
                response_count=response_count,
                api_status_code=res.status_code,
            )
            # Handle empty response with cooldown logic
            # Strategy:
            # 1. Get current updated_at to check if this is a retry (24+ hours since last attempt)
            # 2. Always update updated_at to track this attempt
            # 3. If this is a retry (24+ hours since last attempt), clear flag
            try:
                with get_transaction() as conn:
                    with conn.cursor() as cur:
                        # Get current updated_at for these fixtures to check if this is a retry
                        cur.execute(
                            """
                            SELECT id, updated_at
                            FROM core.fixtures
                            WHERE id = ANY(%s)
                              AND needs_score_verification = TRUE
                            """,
                            (batch,),
                        )
                        fixture_states = {int(r[0]): r[1] for r in cur.fetchall()}
                        
                        # Check which fixtures were attempted 24+ hours ago (retry after cooldown)
                        now_utc = datetime.now(timezone.utc)
                        old_attempts = []
                        for fid in batch:
                            if fid in fixture_states:
                                last_updated = fixture_states[fid]
                                if isinstance(last_updated, datetime):
                                    hours_since = (now_utc - last_updated.replace(tzinfo=timezone.utc)).total_seconds() / 3600
                                else:
                                    # Handle timezone-aware datetime from DB
                                    hours_since = (now_utc - last_updated).total_seconds() / 3600
                                
                                if hours_since >= 24:
                                    old_attempts.append(fid)
                        
                        # Always update updated_at to track this attempt
                        cur.execute(
                            """
                            UPDATE core.fixtures
                            SET updated_at = NOW()
                            WHERE id = ANY(%s)
                              AND needs_score_verification = TRUE
                            """,
                            (batch,),
                        )
                        
                        if old_attempts:
                            # Clear flag for persistent not-found (24+ hours since last attempt)
                            cur.execute(
                                """
                                UPDATE core.fixtures
                                SET needs_score_verification = FALSE
                                WHERE id = ANY(%s)
                                """,
                                (old_attempts,),
                            )
                            logger.info(
                                "auto_finish_verification_flag_cleared_not_found",
                                fixture_ids=old_attempts,
                                reason="persistent_not_found_after_24h_cooldown",
                            )
                        else:
                            # First attempt or within cooldown - keep flag, just track attempt
                            logger.info(
                                "auto_finish_verification_attempt_tracked",
                                fixture_ids=batch,
                                reason="empty_response_cooldown_24h",
                            )
                        
                        conn.commit()
            except Exception as e:
                logger.error(
                    "auto_finish_verification_empty_response_handling_failed",
                    fixture_ids=batch,
                    err=str(e),
                )
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
                # Set needs_score_verification = FALSE for successfully verified fixtures
                verified_ids = [row["id"] for row in fixtures_rows]
                if verified_ids:
                    with conn.cursor() as cur:
                        cur.execute(
                            "UPDATE core.fixtures SET needs_score_verification = FALSE WHERE id = ANY(%s)",
                            (verified_ids,),
                        )
                conn.commit()
            fixtures_verified += len(fixtures_rows)

            # Fetch details for verified fixtures (only missing endpoints)
            verified_fixture_ids = [row["id"] for row in fixtures_rows]
            for fixture_id in verified_fixture_ids:
                try:
                    # Check which endpoints are missing before fetching
                    missing_endpoints = _missing_detail_endpoints_for_fixture(fixture_id=fixture_id)
                    if not missing_endpoints:
                        # All endpoints already present, skip
                        continue

                    # _fetch_and_store_fixture_details already checks for missing endpoints
                    # and skips if all endpoints are present
                    await _fetch_and_store_fixture_details(
                        client=client,
                        limiter=limiter,
                        fixture_id=fixture_id,
                    )
                    # Count fixtures that had details fetched (at least one endpoint was missing)
                    details_fetched_count += 1
                    # Count exact number of endpoints fetched
                    details_endpoints_fetched += len(missing_endpoints)
                except EmergencyStopError:
                    # Quota exhausted, stop processing
                    raise
                except Exception as e:
                    # Details fetch failed, but verification succeeded
                    # Log warning but don't fail the verification
                    logger.warning(
                        "auto_finish_verification_details_fetch_failed",
                        fixture_id=fixture_id,
                        err=str(e),
                    )
                    continue

        except Exception as e:
            logger.error("auto_finish_verification_db_failed", err=str(e), ids=len(batch))
            continue

    q = limiter.quota
    logger.info(
        "auto_finish_verification_complete",
        selected=len(verification_ids),
        api_requests=total_requests,
        fixtures_verified=fixtures_verified,
        details_fetched_count=details_fetched_count,
        details_endpoints_fetched=details_endpoints_fetched,
        daily_remaining=q.daily_remaining,
        minute_remaining=q.minute_remaining,
    )

