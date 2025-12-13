from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC_DIR))

from utils.db import get_db_connection  # noqa: E402
from utils.logging import setup_logging  # noqa: E402


def print_coverage_report(*, season: int) -> None:
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                  l.name AS league,
                  c.count_coverage,
                  c.freshness_coverage,
                  c.pipeline_coverage,
                  c.overall_coverage,
                  c.calculated_at
                FROM mart.coverage_status c
                JOIN core.leagues l ON c.league_id = l.id
                WHERE c.season = %s
                  AND c.endpoint = '/fixtures'
                ORDER BY c.overall_coverage DESC NULLS LAST
                """,
                (int(season),),
            )
            rows = cur.fetchall()

    print("\nCOVERAGE REPORT")
    print("=" * 90)
    print(f"{'League':<24} {'Count':>8} {'Fresh':>8} {'Pipeline':>10} {'Overall':>10} {'Updated':>20}")
    print("-" * 90)
    for league, count, fresh, pipeline, overall, updated in rows:
        updated_str = updated.strftime("%Y-%m-%d %H:%M") if updated else "-"
        def _fmt(v):
            return "-" if v is None else f"{float(v):.1f}%"
        print(
            f"{str(league):<24} {_fmt(count):>8} {_fmt(fresh):>8} {_fmt(pipeline):>10} {_fmt(overall):>10} {updated_str:>20}"
        )
    print("=" * 90)


def main() -> int:
    setup_logging()
    parser = argparse.ArgumentParser(description="Phase 3 - Coverage report")
    parser.add_argument("--season", type=int, default=2024, help="Season year (default: 2024)")
    args = parser.parse_args()
    print_coverage_report(season=args.season)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


