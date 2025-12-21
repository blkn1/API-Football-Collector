from __future__ import annotations

import asyncio
from datetime import date as Date, datetime, timedelta, timezone
import json
import os
import re
from typing import Any, AsyncIterator

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from src.mcp import queries as mcp_queries
from src.utils.db import get_db_connection


app = FastAPI(title="api-football-read-api", version="v1")
security = HTTPBasic(auto_error=False)


def _ip_allowlist() -> set[str] | None:
    raw = (os.getenv("READ_API_IP_ALLOWLIST") or "").strip()
    if not raw:
        return None
    return {x.strip() for x in raw.split(",") if x.strip()}


def _basic_auth_configured() -> tuple[str, str] | None:
    user = os.getenv("READ_API_BASIC_USER")
    pwd = os.getenv("READ_API_BASIC_PASSWORD")
    if user and pwd:
        return (user, pwd)
    return None


def require_access(request: Request, creds: HTTPBasicCredentials | None = Depends(security)) -> None:
    """Basic auth + optional IP allowlist. If no BASIC creds configured, auth is skipped."""

    allow = _ip_allowlist()
    if allow is not None:
        client_ip = request.client.host if request.client else ""
        if client_ip not in allow:
            raise HTTPException(status_code=403, detail="ip_not_allowed")

    cfg = _basic_auth_configured()
    if cfg is None:
        # For local/dev convenience. In production, set READ_API_BASIC_USER/PASSWORD.
        return

    if creds is None or creds.username is None or creds.password is None:
        raise HTTPException(status_code=401, detail="basic_auth_required", headers={"WWW-Authenticate": "Basic"})

    expected_user, expected_pwd = cfg
    if creds.username != expected_user or creds.password != expected_pwd:
        raise HTTPException(status_code=401, detail="invalid_credentials", headers={"WWW-Authenticate": "Basic"})


def _to_int_or_none(x: Any) -> int | None:
    try:
        return int(x) if x is not None else None
    except Exception:
        return None


def _to_iso_or_none(dt: Any) -> str | None:
    try:
        return dt.isoformat() if dt is not None else None
    except Exception:
        return None


_YMD_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _parse_ymd(x: str) -> Date:
    s = (x or "").strip()
    if not _YMD_RE.match(s):
        raise ValueError("invalid_date_format_expected_YYYY-MM-DD")
    return Date.fromisoformat(s)


def _utc_end_of_day(d: Date) -> datetime:
    # inclusive end-of-day: next day 00:00 minus 1 microsecond
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc) + timedelta(days=1) - timedelta(microseconds=1)


def _fetchone(sql_text: str, params: tuple[Any, ...]) -> tuple[Any, ...] | None:
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql_text, params)
            row = cur.fetchone()
        conn.commit()
    return row


def _fetchall(sql_text: str, params: tuple[Any, ...]) -> list[tuple[Any, ...]]:
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql_text, params)
            rows = cur.fetchall()
        conn.commit()
    return rows


async def _fetchone_async(sql_text: str, params: tuple[Any, ...]) -> tuple[Any, ...] | None:
    return await asyncio.to_thread(_fetchone, sql_text, params)


async def _fetchall_async(sql_text: str, params: tuple[Any, ...]) -> list[tuple[Any, ...]]:
    return await asyncio.to_thread(_fetchall, sql_text, params)


@app.get("/v1/health")
async def health() -> dict:
    try:
        row = await _fetchone_async("SELECT 1;", ())
        return {"ok": True, "db": bool(row and row[0] == 1)}
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@app.get("/v1/quota", dependencies=[Depends(require_access)])
async def quota() -> dict:
    row = await _fetchone_async(mcp_queries.LAST_QUOTA_HEADERS_QUERY, ())
    if not row:
        return {"ok": True, "daily_remaining": None, "minute_remaining": None, "observed_at_utc": None}
    observed_at, daily_raw, minute_raw = row
    return {
        "ok": True,
        "daily_remaining": _to_int_or_none(daily_raw),
        "minute_remaining": _to_int_or_none(minute_raw),
        "observed_at_utc": _to_iso_or_none(observed_at),
    }


@app.get("/v1/fixtures", dependencies=[Depends(require_access)])
async def fixtures(league_id: int | None = None, date: str | None = None, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    safe_limit = max(1, min(int(limit), 200))
    filters: list[str] = []
    params: list[Any] = []

    if league_id is not None:
        filters.append("AND f.league_id = %s")
        params.append(int(league_id))
    if status is not None:
        filters.append("AND f.status_short = %s")
        params.append(str(status))
    if date is not None:
        # Accept YYYY-MM-DD only
        filters.append("AND DATE(f.date AT TIME ZONE 'UTC') = %s")
        params.append(str(date))

    sql_text = mcp_queries.FIXTURES_QUERY.format(filters="\n    ".join(filters))
    params.append(safe_limit)

    rows = await _fetchall_async(sql_text, tuple(params))
    out: list[dict[str, Any]] = []
    for r in rows:
        out.append(
            {
                "id": int(r[0]),
                "league_id": int(r[1]),
                "season": _to_int_or_none(r[2]),
                "date_utc": _to_iso_or_none(r[3]),
                "status": r[4],
                "home_team": r[5],
                "away_team": r[6],
                "goals_home": _to_int_or_none(r[7]),
                "goals_away": _to_int_or_none(r[8]),
                "updated_at_utc": _to_iso_or_none(r[9]),
            }
        )
    return out


@app.get("/v1/teams/{team_id}/fixtures", dependencies=[Depends(require_access)])
async def team_fixtures(
    team_id: int,
    from_date: str,
    to_date: str,
    status: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """
    List fixtures for a team across ALL competitions within a UTC date range.
    This powers team pages (history + upcoming) in the frontend.
    """
    safe_limit = max(1, min(int(limit), 500))
    try:
        d_from = _parse_ymd(from_date)
        d_to = _parse_ymd(to_date)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    if d_to < d_from:
        raise HTTPException(status_code=400, detail="to_date_must_be_gte_from_date")

    filters: list[str] = []
    params: list[Any] = []

    # Team participates as home OR away.
    filters.append("AND (f.home_team_id = %s OR f.away_team_id = %s)")
    params.extend([int(team_id), int(team_id)])

    filters.append("AND DATE(f.date AT TIME ZONE 'UTC') >= %s")
    params.append(d_from.isoformat())
    filters.append("AND DATE(f.date AT TIME ZONE 'UTC') <= %s")
    params.append(d_to.isoformat())

    if status is not None:
        filters.append("AND f.status_short = %s")
        params.append(str(status))

    sql_text = mcp_queries.TEAM_FIXTURES_QUERY.format(filters="\n    ".join(filters))
    params.append(safe_limit)
    rows = await _fetchall_async(sql_text, tuple(params))

    out: list[dict[str, Any]] = []
    for r in rows:
        out.append(
            {
                "id": int(r[0]),
                "league_id": int(r[1]),
                "season": _to_int_or_none(r[2]),
                "date_utc": _to_iso_or_none(r[3]),
                "status": r[4],
                "home_team_id": _to_int_or_none(r[5]),
                "home_team": r[6],
                "away_team_id": _to_int_or_none(r[7]),
                "away_team": r[8],
                "goals_home": _to_int_or_none(r[9]),
                "goals_away": _to_int_or_none(r[10]),
                "updated_at_utc": _to_iso_or_none(r[11]),
            }
        )
    return out


def _normalize_stat_key(t: Any) -> str:
    s = (str(t) if t is not None else "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
    return s


def _parse_intish(v: Any) -> int | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        try:
            return int(v)
        except Exception:
            return None
    s = str(v).strip()
    if not s:
        return None
    # handle "55%" possession-like strings
    s = s.replace("%", "").strip()
    if not s:
        return None
    try:
        return int(float(s))
    except Exception:
        return None


def _extract_team_match_stats(statistics_json: Any) -> dict[str, int | None]:
    """
    Convert core.fixture_statistics.statistics JSONB into a normalized dict of int-ish values.
    Keys are normalized (e.g. 'shots_on_goal', 'corner_kicks').
    """
    out: dict[str, int | None] = {}
    if not isinstance(statistics_json, list):
        return out
    for item in statistics_json:
        if not isinstance(item, dict):
            continue
        k = _normalize_stat_key(item.get("type"))
        if not k:
            continue
        out[k] = _parse_intish(item.get("value"))
    return out


@app.get("/v1/fixtures/{fixture_id}/details", dependencies=[Depends(require_access)])
async def fixture_details(fixture_id: int) -> dict[str, Any]:
    """
    Return a merged view of fixture detail data.\n
    - Prefer core.fixture_details snapshot JSONB when present\n
    - Fallback to normalized core.fixture_* tables\n
    """
    fid = int(fixture_id)

    snapshot_row = await _fetchone_async(mcp_queries.FIXTURE_DETAILS_SNAPSHOT_QUERY, (fid,))
    snapshot: dict[str, Any] | None = None
    if snapshot_row:
        snapshot = {
            "fixture_id": int(snapshot_row[0]),
            "events": snapshot_row[1],
            "lineups": snapshot_row[2],
            "statistics": snapshot_row[3],
            "players": snapshot_row[4],
            "updated_at_utc": _to_iso_or_none(snapshot_row[5]),
            "source": "core.fixture_details",
        }

    # Normalized fallbacks (always safe to attempt)
    players_rows = await _fetchall_async(mcp_queries.FIXTURE_PLAYERS_QUERY.format(team_filter=""), (fid, 5000))
    events_rows = await _fetchall_async(mcp_queries.FIXTURE_EVENTS_QUERY, (fid, 5000))
    stats_rows = await _fetchall_async(mcp_queries.FIXTURE_STATISTICS_QUERY, (fid,))
    lineups_rows = await _fetchall_async(mcp_queries.FIXTURE_LINEUPS_QUERY, (fid,))

    players_out: list[dict[str, Any]] = []
    for r in players_rows:
        players_out.append(
            {
                "fixture_id": int(r[0]),
                "team_id": _to_int_or_none(r[1]),
                "player_id": _to_int_or_none(r[2]),
                "player_name": r[3],
                "statistics": r[4],
                "updated_at_utc": _to_iso_or_none(r[5]),
            }
        )

    events_out: list[dict[str, Any]] = []
    for r in events_rows:
        events_out.append(
            {
                "fixture_id": int(r[0]),
                "time_elapsed": _to_int_or_none(r[1]),
                "time_extra": _to_int_or_none(r[2]),
                "team_id": _to_int_or_none(r[3]),
                "player_id": _to_int_or_none(r[4]),
                "assist_id": _to_int_or_none(r[5]),
                "type": r[6],
                "detail": r[7],
                "comments": r[8],
                "updated_at_utc": _to_iso_or_none(r[9]),
            }
        )

    stats_out: list[dict[str, Any]] = []
    for r in stats_rows:
        stats_out.append(
            {
                "fixture_id": int(r[0]),
                "team_id": _to_int_or_none(r[1]),
                "statistics": r[2],
                "updated_at_utc": _to_iso_or_none(r[3]),
            }
        )

    lineups_out: list[dict[str, Any]] = []
    for r in lineups_rows:
        lineups_out.append(
            {
                "fixture_id": int(r[0]),
                "team_id": _to_int_or_none(r[1]),
                "formation": r[2],
                "start_xi": r[3],
                "substitutes": r[4],
                "coach": r[5],
                "colors": r[6],
                "updated_at_utc": _to_iso_or_none(r[7]),
            }
        )

    # Merge preference: snapshot fields if present and non-null, else normalized.
    if snapshot:
        return {
            "ok": True,
            "fixture_id": fid,
            "source": snapshot.get("source"),
            "updated_at_utc": snapshot.get("updated_at_utc"),
            "events": snapshot.get("events") if snapshot.get("events") is not None else events_out,
            "lineups": snapshot.get("lineups") if snapshot.get("lineups") is not None else lineups_out,
            "statistics": snapshot.get("statistics") if snapshot.get("statistics") is not None else stats_out,
            "players": snapshot.get("players") if snapshot.get("players") is not None else players_out,
        }

    return {
        "ok": True,
        "fixture_id": fid,
        "source": "core.fixture_*",
        "events": events_out,
        "lineups": lineups_out,
        "statistics": stats_out,
        "players": players_out,
    }


@app.get("/v1/h2h", dependencies=[Depends(require_access)])
async def h2h(home_team_id: int, away_team_id: int, limit: int = 5) -> list[dict[str, Any]]:
    safe_limit = max(1, min(int(limit), 50))
    rows = await _fetchall_async(
        mcp_queries.H2H_FIXTURES_QUERY,
        (int(home_team_id), int(away_team_id), int(away_team_id), int(home_team_id), safe_limit),
    )
    out: list[dict[str, Any]] = []
    for r in rows:
        out.append(
            {
                "id": int(r[0]),
                "league_id": int(r[1]),
                "season": _to_int_or_none(r[2]),
                "date_utc": _to_iso_or_none(r[3]),
                "status": r[4],
                "home_team_id": _to_int_or_none(r[5]),
                "home_team": r[6],
                "away_team_id": _to_int_or_none(r[7]),
                "away_team": r[8],
                "goals_home": _to_int_or_none(r[9]),
                "goals_away": _to_int_or_none(r[10]),
                "updated_at_utc": _to_iso_or_none(r[11]),
            }
        )
    return out


@app.get("/v1/teams/{team_id}/metrics", dependencies=[Depends(require_access)])
async def team_metrics(team_id: int, last_n: int = 20, as_of_date: str | None = None) -> dict[str, Any]:
    """
    Aggregated features for match prediction.\n
    - last_n: number of completed matches to include (default 20)\n
    - as_of_date: optional YYYY-MM-DD; only matches with fixture.date <= end-of-day are considered\n
    """
    n = max(1, min(int(last_n), 50))
    if as_of_date is not None:
        try:
            d = _parse_ymd(as_of_date)
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e))
        cutoff = _utc_end_of_day(d)
    else:
        cutoff = datetime.now(timezone.utc)

    # Completed matches only (prediction history features).
    final_statuses = ("FT", "AET", "PEN")

    filters: list[str] = []
    params: list[Any] = []
    filters.append("AND (f.home_team_id = %s OR f.away_team_id = %s)")
    params.extend([int(team_id), int(team_id)])
    filters.append("AND f.status_short = ANY(%s)")
    params.append(list(final_statuses))
    filters.append("AND f.date <= %s")
    params.append(cutoff)

    sql_text = mcp_queries.TEAM_FIXTURES_QUERY.format(filters="\n    ".join(filters))
    params.append(n)
    rows = await _fetchall_async(sql_text, tuple(params))

    fixtures: list[dict[str, Any]] = []
    fixture_ids: list[int] = []
    for r in rows:
        fid = int(r[0])
        fixture_ids.append(fid)
        fixtures.append(
            {
                "id": fid,
                "league_id": int(r[1]),
                "season": _to_int_or_none(r[2]),
                "date_utc": _to_iso_or_none(r[3]),
                "status": r[4],
                "home_team_id": _to_int_or_none(r[5]),
                "home_team": r[6],
                "away_team_id": _to_int_or_none(r[7]),
                "away_team": r[8],
                "goals_home": _to_int_or_none(r[9]),
                "goals_away": _to_int_or_none(r[10]),
            }
        )

    # Pull per-fixture statistics (two rows per fixture: one per team) and events for first-goal timing.
    stats_by_fixture_team: dict[tuple[int, int], dict[str, int | None]] = {}
    if fixture_ids:
        # statistics
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT fixture_id, team_id, statistics
                    FROM core.fixture_statistics
                    WHERE fixture_id = ANY(%s)
                    """,
                    (fixture_ids,),
                )
                for fid, tid, stats_json in cur.fetchall():
                    if fid is None or tid is None:
                        continue
                    stats_by_fixture_team[(int(fid), int(tid))] = _extract_team_match_stats(stats_json)
            conn.commit()

    # Aggregate
    played = 0
    wins = draws = losses = 0
    gf = ga = 0
    btts_yes = 0
    clean_sheet = 0
    home_played = away_played = 0
    home_gf = home_ga = away_gf = away_ga = 0

    # stats accumulators: sum + count (only where present)
    stat_sums: dict[str, int] = {}
    stat_counts: dict[str, int] = {}

    for fx in fixtures:
        hid = fx.get("home_team_id")
        aid = fx.get("away_team_id")
        gh = fx.get("goals_home")
        ga_ = fx.get("goals_away")
        if hid is None or aid is None or gh is None or ga_ is None:
            continue

        is_home = int(hid) == int(team_id)
        is_away = int(aid) == int(team_id)
        if not (is_home or is_away):
            continue

        played += 1
        team_g = int(gh) if is_home else int(ga_)
        opp_g = int(ga_) if is_home else int(gh)
        gf += team_g
        ga += opp_g

        if team_g > opp_g:
            wins += 1
        elif team_g < opp_g:
            losses += 1
        else:
            draws += 1

        if team_g > 0 and opp_g > 0:
            btts_yes += 1
        if opp_g == 0:
            clean_sheet += 1

        if is_home:
            home_played += 1
            home_gf += team_g
            home_ga += opp_g
        else:
            away_played += 1
            away_gf += team_g
            away_ga += opp_g

        # collect stats if present for this team in this fixture
        st = stats_by_fixture_team.get((int(fx["id"]), int(team_id)))
        if st:
            for k, v in st.items():
                if v is None:
                    continue
                stat_sums[k] = stat_sums.get(k, 0) + int(v)
                stat_counts[k] = stat_counts.get(k, 0) + 1

    def _avg(key: str) -> float | None:
        c = stat_counts.get(key, 0)
        if c <= 0:
            return None
        return round(stat_sums.get(key, 0) / c, 4)

    def _rate(x: int) -> float | None:
        if played <= 0:
            return None
        return round((x / played) * 100.0, 4)

    return {
        "ok": True,
        "team_id": int(team_id),
        "window": {"last_n": int(n), "played": int(played), "as_of_utc": _to_iso_or_none(cutoff)},
        "results": {"wins": wins, "draws": draws, "losses": losses, "win_rate_pct": _rate(wins)},
        "goals": {
            "gf": gf,
            "ga": ga,
            "gf_avg": (round(gf / played, 4) if played else None),
            "ga_avg": (round(ga / played, 4) if played else None),
            "btts_rate_pct": _rate(btts_yes),
            "clean_sheet_rate_pct": _rate(clean_sheet),
            "home": {
                "played": home_played,
                "gf": home_gf,
                "ga": home_ga,
                "gf_avg": (round(home_gf / home_played, 4) if home_played else None),
                "ga_avg": (round(home_ga / home_played, 4) if home_played else None),
            },
            "away": {
                "played": away_played,
                "gf": away_gf,
                "ga": away_ga,
                "gf_avg": (round(away_gf / away_played, 4) if away_played else None),
                "ga_avg": (round(away_ga / away_played, 4) if away_played else None),
            },
        },
        "match_stats_avg": {
            # common chart keys (may be null if league doesn't provide)
            "total_shots": _avg("total_shots"),
            "shots_on_goal": _avg("shots_on_goal"),
            "corner_kicks": _avg("corner_kicks"),
            "yellow_cards": _avg("yellow_cards"),
            "red_cards": _avg("red_cards"),
            "ball_possession_pct": _avg("ball_possession"),
            "offsides": _avg("offsides"),
        },
        "fixtures_sample": fixtures[: min(len(fixtures), 20)],
    }


@app.get("/v1/standings/{league_id}/{season}", dependencies=[Depends(require_access)])
async def standings(league_id: int, season: int) -> list[dict[str, Any]]:
    rows = await _fetchall_async(mcp_queries.STANDINGS_QUERY, (int(league_id), int(season)))
    out: list[dict[str, Any]] = []
    for r in rows:
        out.append(
            {
                "league_id": int(r[0]),
                "season": int(r[1]),
                "team_id": int(r[2]),
                "team": r[3],
                "rank": _to_int_or_none(r[4]),
                "points": _to_int_or_none(r[5]),
                "goals_diff": _to_int_or_none(r[6]),
                "goals_for": _to_int_or_none(r[7]),
                "goals_against": _to_int_or_none(r[8]),
                "form": r[9],
                "status": r[10],
                "description": r[11],
                "group": r[12],
                "updated_at_utc": _to_iso_or_none(r[13]),
            }
        )
    return out


@app.get("/v1/teams", dependencies=[Depends(require_access)])
async def teams(search: str | None = None, league_id: int | None = None, limit: int = 50) -> list[dict[str, Any]]:
    safe_limit = max(1, min(int(limit), 200))
    filters: list[str] = []
    params: list[Any] = []

    if search:
        filters.append("AND t.name ILIKE %s")
        params.append(f"%{search}%")

    if league_id is not None:
        filters.append(
            """
            AND t.id IN (
              SELECT f.home_team_id FROM core.fixtures f WHERE f.league_id = %s
              UNION
              SELECT f.away_team_id FROM core.fixtures f WHERE f.league_id = %s
            )
            """.strip()
        )
        params.extend([int(league_id), int(league_id)])

    sql_text = mcp_queries.TEAMS_QUERY.format(filters="\n    ".join(filters))
    params.append(safe_limit)

    rows = await _fetchall_async(sql_text, tuple(params))
    out: list[dict[str, Any]] = []
    for r in rows:
        out.append(
            {
                "id": int(r[0]),
                "name": r[1],
                "code": r[2],
                "country": r[3],
                "founded": _to_int_or_none(r[4]),
                "national": bool(r[5]) if r[5] is not None else None,
                "logo": r[6],
                "venue_id": _to_int_or_none(r[7]),
                "updated_at_utc": _to_iso_or_none(r[8]),
            }
        )
    return out


@app.get("/v1/injuries", dependencies=[Depends(require_access)])
async def injuries(
    league_id: int | None = None,
    season: int | None = None,
    team_id: int | None = None,
    player_id: int | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    safe_limit = max(1, min(int(limit), 200))
    filters: list[str] = []
    params: list[Any] = []

    if league_id is not None:
        filters.append("AND i.league_id = %s")
        params.append(int(league_id))
    if season is not None:
        filters.append("AND i.season = %s")
        params.append(int(season))
    if team_id is not None:
        filters.append("AND i.team_id = %s")
        params.append(int(team_id))
    if player_id is not None:
        filters.append("AND i.player_id = %s")
        params.append(int(player_id))

    sql_text = mcp_queries.INJURIES_QUERY.format(filters="\n    ".join(filters))
    params.append(safe_limit)

    rows = await _fetchall_async(sql_text, tuple(params))
    out: list[dict[str, Any]] = []
    for r in rows:
        out.append(
            {
                "league_id": int(r[0]),
                "season": int(r[1]),
                "team_id": _to_int_or_none(r[2]),
                "player_id": _to_int_or_none(r[3]),
                "player_name": r[4],
                "team_name": r[5],
                "type": r[6],
                "reason": r[7],
                "severity": r[8],
                "date": str(r[9]) if r[9] is not None else None,
                "updated_at_utc": _to_iso_or_none(r[10]),
            }
        )
    return out


LIVE_SCORES_SQL = """
SELECT
  fixture_id,
  league_id,
  league_name,
  season,
  round,
  date,
  status_short,
  elapsed,
  home_team_id,
  home_team_name,
  away_team_id,
  away_team_name,
  goals_home,
  goals_away,
  updated_at
FROM mart.live_score_panel
ORDER BY date DESC
LIMIT %s
"""


def _sse_event(event: str, data: Any) -> bytes:
    payload = json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n".encode("utf-8")


async def _system_status_payload() -> dict[str, Any]:
    quota_row = await _fetchone_async(mcp_queries.LAST_QUOTA_HEADERS_QUERY, ())
    if quota_row:
        observed_at, daily_raw, minute_raw = quota_row
        quota = {
            "daily_remaining": _to_int_or_none(daily_raw),
            "minute_remaining": _to_int_or_none(minute_raw),
            "observed_at_utc": _to_iso_or_none(observed_at),
        }
    else:
        quota = {"daily_remaining": None, "minute_remaining": None, "observed_at_utc": None}

    stats_row = await _fetchone_async(mcp_queries.DATABASE_STATS_QUERY, ())
    stats = None
    if stats_row:
        stats = {
            "raw_api_responses": int(stats_row[0]),
            "core_leagues": int(stats_row[1]),
            "core_teams": int(stats_row[2]),
            "core_venues": int(stats_row[3]),
            "core_fixtures": int(stats_row[4]),
            "core_fixture_details": int(stats_row[5]),
            "core_injuries": int(stats_row[6]),
            "core_fixture_players": int(stats_row[7]),
            "core_fixture_events": int(stats_row[8]),
            "core_fixture_statistics": int(stats_row[9]),
            "core_fixture_lineups": int(stats_row[10]),
            "core_standings": int(stats_row[11]),
            "raw_last_fetched_at_utc": _to_iso_or_none(stats_row[12]),
            "core_fixtures_last_updated_at_utc": _to_iso_or_none(stats_row[13]),
        }

    return {"quota": quota, "db": stats}


@app.get("/v1/sse/system-status", dependencies=[Depends(require_access)])
async def sse_system_status(request: Request, interval_seconds: int = 5) -> Response:
    interval = max(2, min(int(interval_seconds), 60))

    async def gen() -> AsyncIterator[bytes]:
        # Initial event
        last_payload: str | None = None
        while True:
            if await request.is_disconnected():
                break
            payload = await _system_status_payload()
            encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
            if encoded != last_payload:
                last_payload = encoded
                yield _sse_event("system_status", payload)
            await asyncio.sleep(interval)

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/v1/sse/live-scores", dependencies=[Depends(require_access)])
async def sse_live_scores(request: Request, interval_seconds: int = 3, limit: int = 300) -> Response:
    interval = max(2, min(int(interval_seconds), 30))
    safe_limit = max(1, min(int(limit), 500))

    async def gen() -> AsyncIterator[bytes]:
        last_payload: str | None = None
        while True:
            if await request.is_disconnected():
                break
            rows = await _fetchall_async(LIVE_SCORES_SQL, (safe_limit,))
            items: list[dict[str, Any]] = []
            for r in rows:
                items.append(
                    {
                        "fixture_id": int(r[0]),
                        "league_id": int(r[1]),
                        "league_name": r[2],
                        "season": _to_int_or_none(r[3]),
                        "round": r[4],
                        "date_utc": _to_iso_or_none(r[5]),
                        "status_short": r[6],
                        "elapsed": _to_int_or_none(r[7]),
                        "home_team_id": _to_int_or_none(r[8]),
                        "home_team_name": r[9],
                        "away_team_id": _to_int_or_none(r[10]),
                        "away_team_name": r[11],
                        "goals_home": _to_int_or_none(r[12]),
                        "goals_away": _to_int_or_none(r[13]),
                        "updated_at_utc": _to_iso_or_none(r[14]),
                    }
                )

            encoded = json.dumps(items, ensure_ascii=False, sort_keys=True)
            if encoded != last_payload:
                last_payload = encoded
                yield _sse_event("live_score_update", {"items": items})
            await asyncio.sleep(interval)

    return StreamingResponse(gen(), media_type="text/event-stream")


OPS_DASHBOARD_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>API-Football Ops Panel</title>
  <style>
    :root { color-scheme: light dark; }
    body { font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif; margin: 24px; line-height: 1.35; }
    header { display:flex; align-items:baseline; justify-content:space-between; gap:16px; }
    h1 { margin:0; font-size: 20px; }
    .muted { opacity: .7; font-size: 12px; }
    .grid { display:grid; grid-template-columns: repeat(12, 1fr); gap: 12px; margin-top: 16px; }
    .card { border: 1px solid rgba(127,127,127,.35); border-radius: 10px; padding: 12px; grid-column: span 6; }
    .card h2 { margin: 0 0 8px 0; font-size: 14px; }
    pre { margin:0; overflow:auto; padding: 10px; border-radius: 8px; border: 1px solid rgba(127,127,127,.25); }
    nav a { margin-right: 10px; }
    @media (max-width: 1000px) { .card { grid-column: span 12; } }
  </style>
</head>
<body>
  <header>
    <div>
      <h1>API-Football Ops Panel</h1>
      <div class="muted" id="ts">loading…</div>
    </div>
    <nav>
      <a href="/ops">Dashboard</a>
      <a href="/docs">OpenAPI</a>
    </nav>
  </header>

  <div class="grid">
    <section class="card">
      <h2>System status</h2>
      <pre id="system"></pre>
    </section>
    <section class="card">
      <h2>Backfill + RAW errors + Recent logs</h2>
      <pre id="ops"></pre>
    </section>
  </div>

  <script>
    async function refresh() {
      const res = await fetch('/ops/api/system_status');
      const data = await res.json();
      document.getElementById('ts').textContent = 'updated: ' + new Date().toISOString();
      document.getElementById('system').textContent = JSON.stringify({
        quota: data.quota,
        db: data.db,
        coverage_summary: data.coverage_summary,
        job_status: data.job_status,
      }, null, 2);
      document.getElementById('ops').textContent = JSON.stringify({
        backfill: data.backfill,
        raw_errors: data.raw_errors,
        recent_log_errors: data.recent_log_errors,
      }, null, 2);
    }
    refresh();
    setInterval(refresh, 5000);
  </script>
</body>
</html>
"""


@app.get("/ops", dependencies=[Depends(require_access)])
async def ops_dashboard() -> Response:
    return Response(content=OPS_DASHBOARD_HTML, media_type="text/html")


@app.get("/ops/api/system_status", dependencies=[Depends(require_access)])
async def ops_system_status() -> dict:
    # Import here (lazy) to avoid any startup coupling when ops panel isn't used.
    from src.mcp import server as mcp_server

    default_season = os.getenv("READ_API_DEFAULT_SEASON")
    season_int = int(default_season) if default_season and default_season.strip().isdigit() else None

    quota = await mcp_server.get_rate_limit_status()
    db = await mcp_server.get_database_stats()
    coverage_summary = await mcp_server.get_coverage_summary(season=season_int) if season_int is not None else await mcp_server.get_coverage_summary()
    job_status = await mcp_server.get_job_status()
    backfill = await mcp_server.get_backfill_progress()
    raw_errors = await mcp_server.get_raw_error_summary(since_minutes=60)
    raw_error_samples = await mcp_server.get_raw_error_samples(
        since_minutes=60,
        endpoint="/fixtures",
        limit=10,
    )
    recent_log_errors = await mcp_server.get_recent_log_errors(limit=50)

    return {
        "ok": True,
        "quota": quota,
        "db": db,
        "coverage_summary": coverage_summary,
        "job_status": job_status,
        "backfill": backfill,
        "raw_errors": raw_errors,
        "raw_error_samples": raw_error_samples,
        "recent_log_errors": recent_log_errors,
    }
