from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC_DIR))

from collector.api_client import APIClient  # noqa: E402
from collector.rate_limiter import RateLimiter  # noqa: E402
from utils.logging import setup_logging, get_logger  # noqa: E402
from utils.venues_backfill import backfill_missing_venues_for_fixtures  # noqa: E402


logger = get_logger(script="venues_backfill")


async def _amain() -> int:
    setup_logging()
    p = argparse.ArgumentParser(description="Backfill missing core.venues referenced by core.fixtures")
    p.add_argument("--dry-run", action="store_true", help="No DB writes (still calls API)")
    p.add_argument("--max", type=int, default=50, help="Max venues to fetch this run (default 50)")
    args = p.parse_args()

    # Find venue IDs referenced by fixtures but missing in venues
    # Note: we intentionally do this in SQL for accuracy against current DB state.
    from utils.db import query_scalar, get_transaction  # noqa: E402

    with get_transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT f.venue_id
                FROM core.fixtures f
                LEFT JOIN core.venues v ON v.id = f.venue_id
                WHERE f.venue_id IS NOT NULL AND v.id IS NULL
                ORDER BY f.venue_id
                """
            )
            missing = [int(r[0]) for r in cur.fetchall()]

    if not missing:
        logger.info("no_missing_venues")
        return 0

    logger.info("missing_venues_found", count=len(missing), sample=missing[:10])

    limiter = RateLimiter(max_tokens=300, refill_rate=5.0)
    client = APIClient()
    try:
        upserted = await backfill_missing_venues_for_fixtures(
            venue_ids=missing,
            client=client,
            limiter=limiter,
            dry_run=args.dry_run,
            max_to_fetch=args.max,
        )
    finally:
        await client.aclose()

    logger.info("venues_backfill_complete", upserted=upserted, daily_remaining=limiter.quota.daily_remaining)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_amain()))


