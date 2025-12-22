from __future__ import annotations

import json
import os
import sys
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


def _print(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, default=str))


def main() -> int:
    now = datetime.now(timezone.utc).isoformat()
    dsn = _db_dsn()
    out: dict[str, Any] = {"ok": True, "ts_utc": now}

    conn = psycopg2.connect(dsn, connect_timeout=5)
    try:
        with conn.cursor() as cur:
            # 1) Smoke DB
            _q(cur, "SELECT 1;")

            # 2) RAW: /players/topscorers evidence
            rows = _q(
                cur,
                """
                SELECT COUNT(*)::int AS requests_7d, MAX(fetched_at) AS last_fetched_at
                FROM raw.api_responses
                WHERE endpoint = '/players/topscorers'
                  AND fetched_at > NOW() - INTERVAL '7 days'
                """,
            )
            out["raw_topscorers"] = {"requests_7d": rows[0][0], "last_fetched_at_utc": rows[0][1] if rows else None}

            # 3) CORE counts
            counts = _q(
                cur,
                """
                SELECT
                  (SELECT COUNT(*) FROM raw.api_responses)::bigint AS raw_api_responses,
                  (SELECT COUNT(*) FROM core.leagues)::bigint AS core_leagues,
                  (SELECT COUNT(*) FROM core.teams)::bigint AS core_teams,
                  (SELECT COUNT(*) FROM core.fixtures)::bigint AS core_fixtures,
                  (SELECT COUNT(*) FROM core.fixture_events)::bigint AS core_fixture_events,
                  (SELECT COUNT(*) FROM core.fixture_lineups)::bigint AS core_fixture_lineups,
                  (SELECT COUNT(*) FROM core.fixture_statistics)::bigint AS core_fixture_statistics,
                  (SELECT COUNT(*) FROM core.fixture_players)::bigint AS core_fixture_players,
                  (SELECT COUNT(*) FROM core.standings)::bigint AS core_standings,
                  (SELECT COUNT(*) FROM core.injuries)::bigint AS core_injuries,
                  (SELECT COUNT(*) FROM core.top_scorers)::bigint AS core_top_scorers,
                  (SELECT COUNT(*) FROM core.team_statistics)::bigint AS core_team_statistics
                """,
            )
            if counts:
                (
                    raw_api_responses,
                    core_leagues,
                    core_teams,
                    core_fixtures,
                    core_fixture_events,
                    core_fixture_lineups,
                    core_fixture_statistics,
                    core_fixture_players,
                    core_standings,
                    core_injuries,
                    core_top_scorers,
                    core_team_statistics,
                ) = counts[0]
                out["counts"] = {
                    "raw_api_responses": int(raw_api_responses),
                    "core_leagues": int(core_leagues),
                    "core_teams": int(core_teams),
                    "core_fixtures": int(core_fixtures),
                    "core_fixture_events": int(core_fixture_events),
                    "core_fixture_lineups": int(core_fixture_lineups),
                    "core_fixture_statistics": int(core_fixture_statistics),
                    "core_fixture_players": int(core_fixture_players),
                    "core_standings": int(core_standings),
                    "core_injuries": int(core_injuries),
                    "core_top_scorers": int(core_top_scorers),
                    "core_team_statistics": int(core_team_statistics),
                }

            # 4) Grouped top_scorers / team_statistics
            out["core_top_scorers_groups"] = [
                {"league_id": int(r[0]), "season": int(r[1]), "rows": int(r[2]), "last_updated_at_utc": r[3]}
                for r in _q(
                    cur,
                    """
                    SELECT league_id, season, COUNT(*)::int AS rows, MAX(updated_at) AS last_updated_at
                    FROM core.top_scorers
                    GROUP BY league_id, season
                    ORDER BY season DESC, league_id ASC
                    LIMIT 200
                    """,
                )
            ]

            out["core_team_statistics_groups"] = [
                {"league_id": int(r[0]), "season": int(r[1]), "rows": int(r[2]), "last_updated_at_utc": r[3]}
                for r in _q(
                    cur,
                    """
                    SELECT league_id, season, COUNT(*)::int AS rows, MAX(updated_at) AS last_updated_at
                    FROM core.team_statistics
                    GROUP BY league_id, season
                    ORDER BY season DESC, league_id ASC
                    LIMIT 200
                    """,
                )
            ]

    finally:
        conn.close()

    _print(out)

    # Hard failure condition for the reported issue:
    # if RAW has 0 requests for top scorers, the job hasn't run (or failed before calling API).
    if (out.get("raw_topscorers") or {}).get("requests_7d", 0) == 0:
        return 2
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as e:
        print(f"e2e_validate_failed:{type(e).__name__}:{e}", file=sys.stderr)
        raise


