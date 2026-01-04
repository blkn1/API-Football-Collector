#!/usr/bin/env python3
"""
Check fixture events that were fixed by verification job.

Usage:
    python scripts/check_fixture_events.py [fixture_id]
    python scripts/check_fixture_events.py 1396373
    python scripts/check_fixture_events.py  # Shows recently verified fixtures
"""

from __future__ import annotations

import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.utils.db import get_transaction


def check_fixture_events(fixture_id: int | None = None) -> None:
    """Check events for a specific fixture or recently verified fixtures."""
    if fixture_id:
        _check_single_fixture(fixture_id)
    else:
        _check_recently_verified()


def _check_single_fixture(fixture_id: int) -> None:
    """Check events for a single fixture."""
    with get_transaction() as conn:
        with conn.cursor() as cur:
            # Get fixture info
            cur.execute(
                """
                SELECT
                    f.id,
                    f.league_id,
                    f.status_short,
                    f.status_long,
                    f.goals_home,
                    f.goals_away,
                    f.needs_score_verification,
                    f.updated_at,
                    EXISTS (SELECT 1 FROM core.fixture_players p WHERE p.fixture_id = f.id) AS has_players,
                    EXISTS (SELECT 1 FROM core.fixture_events e WHERE e.fixture_id = f.id) AS has_events,
                    EXISTS (SELECT 1 FROM core.fixture_statistics s WHERE s.fixture_id = f.id) AS has_statistics,
                    EXISTS (SELECT 1 FROM core.fixture_lineups l WHERE l.fixture_id = f.id) AS has_lineups
                FROM core.fixtures f
                WHERE f.id = %s
                """,
                (fixture_id,),
            )
            fixture_row = cur.fetchone()
            
            if not fixture_row:
                print(f"❌ Fixture {fixture_id} not found")
                return
            
            print(f"\n{'='*60}")
            print(f"Fixture {fixture_id} Status")
            print(f"{'='*60}")
            print(f"League ID:     {fixture_row[1]}")
            print(f"Status:        {fixture_row[2]} ({fixture_row[3]})")
            print(f"Score:         {fixture_row[4]} - {fixture_row[5]}")
            print(f"Needs Verify:  {fixture_row[6]}")
            print(f"Updated At:    {fixture_row[7]}")
            print(f"\nDetails:")
            print(f"  - Players:     {'✅' if fixture_row[8] else '❌'}")
            print(f"  - Events:       {'✅' if fixture_row[9] else '❌'}")
            print(f"  - Statistics:  {'✅' if fixture_row[10] else '❌'}")
            print(f"  - Lineups:     {'✅' if fixture_row[11] else '❌'}")
            
            # Get events
            cur.execute(
                """
                SELECT 
                    time_elapsed,
                    time_extra,
                    type,
                    detail,
                    comments,
                    team_id,
                    player_id,
                    updated_at
                FROM core.fixture_events
                WHERE fixture_id = %s
                ORDER BY time_elapsed NULLS LAST, time_extra NULLS LAST, updated_at ASC
                """,
                (fixture_id,),
            )
            events = cur.fetchall()
            
            if not events:
                print(f"\n❌ No events found for fixture {fixture_id}")
                return
            
            print(f"\n{'='*60}")
            print(f"Events ({len(events)} total)")
            print(f"{'='*60}")
            for ev in events:
                elapsed = ev[0] or 0
                extra = f"+{ev[1]}" if ev[1] else ""
                time_str = f"{elapsed}{extra}'"
                print(f"{time_str:8} | {ev[2]:15} | {ev[3]:20} | {ev[4] or ''}")
                if ev[5]:
                    print(f"         | Team: {ev[5]} | Player: {ev[6]}")
            
            # Get RAW fetch times
            cur.execute(
                """
                SELECT 
                    endpoint,
                    fetched_at,
                    status_code,
                    results
                FROM raw.api_responses
                WHERE endpoint IN ('/fixtures/events', '/fixtures/players', '/fixtures/statistics', '/fixtures/lineups')
                  AND (requested_params->>'fixture')::bigint = %s
                ORDER BY endpoint, fetched_at DESC
                """,
                (fixture_id,),
            )
            raw_logs = cur.fetchall()
            
            if raw_logs:
                print(f"\n{'='*60}")
                print("RAW API Fetch Times")
                print(f"{'='*60}")
                for log in raw_logs:
                    print(f"{log[0]:25} | {log[1]} | Status: {log[2]} | Results: {log[3]}")


def _check_recently_verified() -> None:
    """Check recently verified fixtures (last 24h)."""
    with get_transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT 
                    f.id,
                    f.league_id,
                    f.status_short,
                    f.goals_home,
                    f.goals_away,
                    f.needs_score_verification,
                    f.updated_at,
                    (SELECT COUNT(*) FROM core.fixture_events e WHERE e.fixture_id = f.id) AS events_count,
                    (SELECT MAX(r.fetched_at) FROM raw.api_responses r WHERE r.endpoint='/fixtures/events' AND (r.requested_params->>'fixture')::bigint=f.id) AS last_events_fetch
                FROM core.fixtures f
                WHERE f.status_short = 'FT'
                  AND f.updated_at >= NOW() - INTERVAL '24 hours'
                  AND EXISTS (SELECT 1 FROM core.fixture_events e WHERE e.fixture_id = f.id)
                ORDER BY f.updated_at DESC
                LIMIT 20
                """,
            )
            fixtures = cur.fetchall()
            
            if not fixtures:
                print("❌ No recently verified fixtures with events found")
                return
            
            print(f"\n{'='*60}")
            print(f"Recently Verified Fixtures with Events ({len(fixtures)} total)")
            print(f"{'='*60}")
            print(f"{'ID':<10} | {'League':<8} | {'Score':<10} | {'Events':<8} | {'Verify':<8} | {'Updated At'}")
            print(f"{'-'*60}")
            for fx in fixtures:
                score = f"{fx[3] or 0}-{fx[4] or 0}"
                verify = "✅" if not fx[5] else "⏳"
                print(f"{fx[0]:<10} | {fx[1]:<8} | {score:<10} | {fx[7]:<8} | {verify:<8} | {fx[6]}")


if __name__ == "__main__":
    fixture_id = int(sys.argv[1]) if len(sys.argv) > 1 else None
    check_fixture_events(fixture_id)

