from __future__ import annotations

from typing import Any

from collector.api_client import APIClient, APIClientError, RateLimitError
from collector.rate_limiter import RateLimiter
from transforms.venues import transform_venues
from utils.db import get_transaction, query_scalar, upsert_core, upsert_raw
from utils.logging import get_logger


logger = get_logger(component="venues_backfill")


def _missing_venue_ids(venue_ids: list[int]) -> list[int]:
    """
    Return subset of venue_ids that are not present in core.venues.
    Uses a single SQL query for efficiency.
    """
    if not venue_ids:
        return []

    # Deduplicate and keep order stable-ish
    unique = list(dict.fromkeys(int(x) for x in venue_ids if x is not None))
    with get_transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT v.vid
                FROM (SELECT UNNEST(%s::bigint[]) AS vid) v
                LEFT JOIN core.venues cv ON cv.id = v.vid
                WHERE cv.id IS NULL
                """,
                (unique,),
            )
            rows = cur.fetchall()
    return [int(r[0]) for r in rows]


async def backfill_missing_venues_for_fixtures(
    *,
    venue_ids: list[int],
    client: APIClient,
    limiter: RateLimiter,
    dry_run: bool,
    max_to_fetch: int = 50,
) -> int:
    """
    Ensure venue IDs exist in core.venues by fetching missing ones from GET /venues?id=...
    Returns number of venues upserted (best-effort).
    """
    missing = _missing_venue_ids(venue_ids)
    if not missing:
        return 0

    # Cap to avoid blowing quota in pathological cases
    missing = missing[: int(max_to_fetch)]
    upserted = 0

    for vid in missing:
        try:
            limiter.acquire_token()
            result = await client.get("/venues", params={"id": int(vid)})
            limiter.update_from_headers(result.headers)
        except RateLimitError as e:
            logger.warning("venues_rate_limited", venue_id=vid, err=str(e))
            break
        except APIClientError as e:
            logger.error("venues_api_failed", venue_id=vid, err=str(e))
            continue
        except Exception as e:
            logger.error("venues_api_unexpected_error", venue_id=vid, err=str(e))
            continue

        env = result.data or {}
        if not dry_run:
            upsert_raw(
                endpoint="/venues",
                requested_params={"id": int(vid)},
                status_code=result.status_code,
                response_headers=result.headers,
                body=env,
            )

        rows = transform_venues(env)
        if not rows:
            continue

        if dry_run:
            upserted += len(rows)
            continue

        try:
            with get_transaction() as conn:
                upsert_core(
                    full_table_name="core.venues",
                    rows=rows,
                    conflict_cols=["id"],
                    update_cols=["name", "address", "city", "country", "capacity", "surface", "image"],
                    conn=conn,
                )
            upserted += len(rows)
        except Exception as e:
            logger.error("venues_db_upsert_failed", venue_id=vid, err=str(e))
            continue

    return upserted


