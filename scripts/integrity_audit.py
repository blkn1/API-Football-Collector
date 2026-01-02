from __future__ import annotations

"""
Broad integrity audit (read-only) for production.

Goal: detect likely bad states early (broken FT, stuck verification flags, empty detail responses)
without modifying data.

Usage (inside collector container):
  cd /app && python3 scripts/integrity_audit.py
  cd /app && DAYS=7 python3 scripts/integrity_audit.py
"""

import json
import os
from datetime import datetime, timezone
from typing import Any

import psycopg2


def _db_dsn() -> str:
    dsn = os.getenv("DATABASE_URL")
    if dsn:
        return dsn
    host = os.getenv("POSTGRES_HOST", "localhost")
    port = int(os.getenv("POSTGRES_PORT", "5432"))
    user = os.getenv("POSTGRES_USER", "postgres")
    password = os.getenv("POSTGRES_PASSWORD", "postgres")
    db = os.getenv("POSTGRES_DB", "api_football")
    return f"postgresql://{user}:{password}@{host}:{port}/{db}"


def _q(cur, sql_text: str, params: tuple[Any, ...] = ()) -> list[tuple]:
    cur.execute(sql_text, params)
    try:
        return cur.fetchall()
    except Exception:
        return []


def main() -> int:
    days = int(os.getenv("DAYS", "14"))
    now = datetime.now(timezone.utc).isoformat()
    out: dict[str, Any] = {"ok": True, "ts_utc": now, "window_days": days}

    conn = psycopg2.connect(_db_dsn(), connect_timeout=5)
    try:
        with conn.cursor() as cur:
            # 1) Broken FT rows (data consistency)
            rows = _q(
                cur,
                """
                SELECT COUNT(*)::bigint
                FROM core.fixtures f
                WHERE f.status_short = 'FT'
                  AND f.date >= NOW() - (%s::text || ' days')::interval
                  AND (
                    f.elapsed IS NULL OR f.elapsed < 90
                    OR f.goals_home IS NULL OR f.goals_away IS NULL
                    OR (f.score IS NULL OR (f.score->'fulltime') IS NULL)
                  )
                """,
                (days,),
            )
            out["broken_ft_count"] = int(rows[0][0]) if rows else 0

            samples = _q(
                cur,
                """
                SELECT f.id, f.league_id, f.date, f.elapsed, f.goals_home, f.goals_away, f.score, f.status_long, f.updated_at
                FROM core.fixtures f
                WHERE f.status_short = 'FT'
                  AND f.date >= NOW() - (%s::text || ' days')::interval
                  AND (
                    f.elapsed IS NULL OR f.elapsed < 90
                    OR f.goals_home IS NULL OR f.goals_away IS NULL
                    OR (f.score IS NULL OR (f.score->'fulltime') IS NULL)
                  )
                ORDER BY f.updated_at DESC
                LIMIT 25
                """,
                (days,),
            )
            out["broken_ft_samples"] = [
                {
                    "id": int(r[0]),
                    "league_id": int(r[1]),
                    "date_utc": r[2],
                    "elapsed": r[3],
                    "goals_home": r[4],
                    "goals_away": r[5],
                    "score": r[6],
                    "status_long": r[7],
                    "updated_at_utc": r[8],
                }
                for r in samples
            ]

            # 2) Verification backlog
            rows = _q(
                cur,
                """
                SELECT COUNT(*)::bigint
                FROM core.fixtures f
                WHERE f.needs_score_verification = TRUE
                """,
            )
            out["needs_score_verification_count"] = int(rows[0][0]) if rows else 0

            samples = _q(
                cur,
                """
                SELECT f.id, f.league_id, f.date, f.goals_home, f.goals_away, f.elapsed, f.score, f.updated_at
                FROM core.fixtures f
                WHERE f.needs_score_verification = TRUE
                ORDER BY f.updated_at ASC
                LIMIT 50
                """,
            )
            out["needs_score_verification_samples"] = [
                {
                    "id": int(r[0]),
                    "league_id": int(r[1]),
                    "date_utc": r[2],
                    "goals_home": r[3],
                    "goals_away": r[4],
                    "elapsed": r[5],
                    "score": r[6],
                    "updated_at_utc": r[7],
                }
                for r in samples
            ]

            # 3) Empty detail responses in RAW (last N days) for per-fixture endpoints
            empty = _q(
                cur,
                """
                SELECT endpoint,
                       COUNT(*)::bigint AS empty_calls
                FROM raw.api_responses
                WHERE endpoint IN ('/fixtures/events','/fixtures/statistics','/fixtures/lineups','/fixtures/players')
                  AND fetched_at >= NOW() - (%s::text || ' days')::interval
                  AND (
                    COALESCE((body->>'results')::int, 0) = 0
                    OR jsonb_array_length(COALESCE(body->'response','[]'::jsonb)) = 0
                  )
                GROUP BY endpoint
                ORDER BY empty_calls DESC
                """,
                (days,),
            )
            out["raw_empty_detail_calls"] = [{"endpoint": r[0], "count": int(r[1])} for r in empty]

            # 4) Potential duplicate-ish events (heuristic)
            dup = _q(
                cur,
                """
                SELECT fixture_id,
                       team_id,
                       player_id,
                       type,
                       detail,
                       COUNT(*)::int AS n
                FROM core.fixture_events
                WHERE updated_at >= NOW() - (%s::text || ' days')::interval
                GROUP BY fixture_id, team_id, player_id, type, detail
                HAVING COUNT(*) > 1
                ORDER BY n DESC
                LIMIT 50
                """,
                (days,),
            )
            out["event_duplicates_heuristic"] = [
                {
                    "fixture_id": int(r[0]),
                    "team_id": r[1],
                    "player_id": r[2],
                    "type": r[3],
                    "detail": r[4],
                    "count": int(r[5]),
                }
                for r in dup
            ]

    finally:
        conn.close()

    print(json.dumps(out, ensure_ascii=False, default=str))

    # Non-zero exit if we see broken FT rows (signals incident to ops)
    return 2 if int(out.get("broken_ft_count") or 0) > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())


