#!/usr/bin/env python3
"""
One-time script to fix existing auto-finished matches with incorrect scores.

Purpose:
- Identify fixtures where status_long LIKE '%Auto-finished%' and date >= '2025-12-28'
- Batch fetch via GET /fixtures?ids=<id1>-<id2>-... (max 20 per request)
- Compare API score with DB score for each fixture
- If mismatch: UPSERT CORE with fresh data, log correction
- Set needs_score_verification = FALSE after correction

Usage:
    python scripts/fix_auto_finished_scores.py [--dry-run] [--date-from YYYY-MM-DD]
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC_DIR))

from collector.api_client import APIClient, APIClientError, APIResult, RateLimitError  # noqa: E402
from collector.rate_limiter import EmergencyStopError, RateLimiter  # noqa: E402
from transforms.fixtures import transform_fixtures  # noqa: E402
from utils.db import get_transaction, upsert_core, upsert_raw  # noqa: E402
from utils.logging import get_logger, setup_logging  # noqa: E402
from utils.dependencies import ensure_fixtures_dependencies  # noqa: E402
from utils.config import load_api_config, load_rate_limiter_config  # noqa: E402


logger = get_logger(script="fix_auto_finished_scores")


def _chunk(ids: list[int], *, size: int) -> list[list[int]]:
    """Split list into chunks of specified size."""
    if size <= 0:
        return [ids]
    return [ids[i : i + size] for i in range(0, len(ids), size)]


def _select_auto_finished_fixtures(*, date_from: str) -> list[dict[str, Any]]:
    """
    Select auto-finished fixtures that may have incorrect scores.
    Returns list of dicts with id, goals_home, goals_away, status_long.
    """
    sql = """
    SELECT id, goals_home, goals_away, status_long, home_team_id, away_team_id, league_id, season
    FROM core.fixtures
    WHERE status_long LIKE '%Auto-finished%'
      AND date >= %s
    ORDER BY date DESC
    """
    with get_transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (date_from,))
            rows = cur.fetchall()
            conn.commit()
    return [
        {
            "id": int(r[0]),
            "goals_home": r[1],
            "goals_away": r[2],
            "status_long": r[3],
            "home_team_id": int(r[4]),
            "away_team_id": int(r[5]),
            "league_id": int(r[6]),
            "season": r[7],
        }
        for r in rows
    ]


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


async def main(*, dry_run: bool, date_from: str) -> int:
    """Main correction logic."""
    setup_logging()

    api_cfg = load_api_config()
    rate_cfg = load_rate_limiter_config()

    client = APIClient(base_url=api_cfg.base_url, api_key=api_cfg.api_key)
    limiter = RateLimiter(config=rate_cfg)

    try:
        # Select auto-finished fixtures
        fixtures = _select_auto_finished_fixtures(date_from=date_from)
        if not fixtures:
            logger.info("fix_auto_finished_no_work", date_from=date_from)
            return 0

        logger.info("fix_auto_finished_start", total=len(fixtures), date_from=date_from, dry_run=dry_run)

        fixture_ids = [f["id"] for f in fixtures]
        fixture_by_id = {f["id"]: f for f in fixtures}

        total_requests = 0
        fixtures_corrected = 0
        fixtures_unchanged = 0
        fixtures_failed = 0

        # Batch fetch from API
        for batch in _chunk(fixture_ids, size=20):
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
                logger.error("emergency_stop_daily_quota_low", err=str(e))
                break
            except RateLimitError as e:
                logger.warning("api_rate_limited_429", err=str(e), sleep_seconds=5)
                await asyncio.sleep(5)
                continue
            except (APIClientError, RuntimeError) as e:
                logger.error("fix_auto_finished_api_failed", err=str(e), ids=len(batch))
                fixtures_failed += len(batch)
                continue

            if not dry_run:
                upsert_raw(
                    endpoint="/fixtures",
                    requested_params=params,
                    status_code=res.status_code,
                    response_headers=res.headers,
                    body=env,
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

                if not dry_run:
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
                logger.error("fix_auto_finished_dependency_failed", err=str(e), ids=len(batch))
                fixtures_failed += len(batch)
                continue

            fixtures_rows, _ = transform_fixtures(env)

            # Compare scores and update if different
            for row in fixtures_rows:
                fixture_id = row["id"]
                db_fixture = fixture_by_id.get(fixture_id)
                if not db_fixture:
                    continue

                api_goals_home = row.get("goals_home")
                api_goals_away = row.get("goals_away")
                db_goals_home = db_fixture.get("goals_home")
                db_goals_away = db_fixture.get("goals_away")

                score_mismatch = (
                    api_goals_home != db_goals_home or api_goals_away != db_goals_away
                )

                if score_mismatch:
                    logger.info(
                        "fix_auto_finished_score_mismatch",
                        fixture_id=fixture_id,
                        db_score=f"{db_goals_home}-{db_goals_away}",
                        api_score=f"{api_goals_home}-{api_goals_away}",
                    )

                    if not dry_run:
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
                                # Set needs_score_verification = FALSE after correction
                                with conn.cursor() as cur:
                                    cur.execute(
                                        "UPDATE core.fixtures SET needs_score_verification = FALSE WHERE id = %s",
                                        (fixture_id,),
                                    )
                                conn.commit()
                            fixtures_corrected += 1
                        except Exception as e:
                            logger.error("fix_auto_finished_upsert_failed", fixture_id=fixture_id, err=str(e))
                            fixtures_failed += 1
                    else:
                        fixtures_corrected += 1  # Count in dry-run too
                else:
                    fixtures_unchanged += 1
                    # Still clear verification flag if scores match
                    if not dry_run:
                        try:
                            with get_transaction() as conn:
                                with conn.cursor() as cur:
                                    cur.execute(
                                        "UPDATE core.fixtures SET needs_score_verification = FALSE WHERE id = %s",
                                        (fixture_id,),
                                    )
                                conn.commit()
                        except Exception as e:
                            logger.warning("fix_auto_finished_flag_clear_failed", fixture_id=fixture_id, err=str(e))

        q = limiter.quota
        logger.info(
            "fix_auto_finished_complete",
            total=len(fixtures),
            api_requests=total_requests,
            corrected=fixtures_corrected,
            unchanged=fixtures_unchanged,
            failed=fixtures_failed,
            daily_remaining=q.daily_remaining,
            minute_remaining=q.minute_remaining,
            dry_run=dry_run,
        )

        return 0

    finally:
        await client.aclose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fix auto-finished matches with incorrect scores")
    parser.add_argument("--dry-run", action="store_true", help="Don't make any changes, just report")
    parser.add_argument(
        "--date-from",
        type=str,
        default="2025-12-28",
        help="Only process fixtures from this date onwards (YYYY-MM-DD)",
    )
    args = parser.parse_args()

    sys.exit(asyncio.run(main(dry_run=args.dry_run, date_from=args.date_from)))

