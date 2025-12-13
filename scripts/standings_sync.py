from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC_DIR))

from utils.standings import StandingsSyncSummary, sync_standings  # noqa: E402
from utils.logging import get_logger, setup_logging  # noqa: E402


logger = get_logger(script="standings_sync")

async def _amain() -> int:
    setup_logging()
    parser = argparse.ArgumentParser(description="Phase 3 - Standings sync (/standings)")
    parser.add_argument("--league", type=int, default=None, help="Sync a single league id")
    parser.add_argument("--dry-run", action="store_true", help="No DB writes (still calls API)")
    args = parser.parse_args()

    cfg_path = PROJECT_ROOT / "config" / "jobs" / "daily.yaml"
    summary = await sync_standings(league_filter=args.league, dry_run=args.dry_run, config_path=cfg_path)
    print(f"[INFO] Standings sync complete (leagues={summary.leagues}, rows={summary.total_rows}, api_requests={summary.api_requests}, daily_remaining={summary.daily_remaining})")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_amain()))


