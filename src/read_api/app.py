from __future__ import annotations

import asyncio
from datetime import date as Date, datetime, timedelta, timezone
import json
import os
import re
from pathlib import Path
from typing import Any, AsyncIterator

import yaml

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from src.mcp import queries as mcp_queries
from src.utils.db import get_db_connection, get_transaction, upsert_core, upsert_raw
from src.collector.api_client import APIClient
from src.collector.rate_limiter import RateLimiter
from src.utils.config import load_api_config, load_rate_limiter_config
from src.transforms.fixtures import transform_fixtures
from src.utils.dependencies import ensure_fixtures_dependencies


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


def require_only_query_params(allowed: set[str]):
    """
    Strict query param enforcement to prevent clients (incl. LLM tools) from sending unsupported params.
    Unknown query params -> 400 with a machine-readable payload in HTTPException.detail.
    """
    allowed_set = {str(x) for x in (allowed or set())}

    async def _dep(request: Request) -> None:
        unknown = sorted({k for k in request.query_params.keys() if k not in allowed_set})
        if unknown:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "unknown_query_params",
                    "unknown": unknown,
                    "allowed": sorted(allowed_set),
                },
            )

    return _dep


# Singleton instances for API client and rate limiter (lazy initialization)
_api_client: APIClient | None = None
_rate_limiter: RateLimiter | None = None


def get_api_client() -> APIClient:
    """Get or create singleton APIClient instance."""
    global _api_client
    if _api_client is None:
        api_cfg = load_api_config()
        _api_client = APIClient(
            base_url=api_cfg.base_url,
            api_key_env=api_cfg.api_key_env,
            timeout_seconds=api_cfg.timeout_seconds,
        )
    return _api_client


def get_rate_limiter() -> RateLimiter:
    """Get or create singleton RateLimiter instance."""
    global _rate_limiter
    if _rate_limiter is None:
        rl_cfg = load_rate_limiter_config()
        # RateLimiter uses max_tokens (bucket capacity) and refill_rate (tokens per second)
        # Config provides minute_soft_limit (safe working cap per minute)
        # refill_rate = minute_soft_limit / 60.0 (tokens per second)
        _rate_limiter = RateLimiter(
            max_tokens=rl_cfg.minute_soft_limit,
            refill_rate=float(rl_cfg.minute_soft_limit) / 60.0,
            emergency_stop_threshold=rl_cfg.emergency_stop_threshold,
        )
    return _rate_limiter


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

def _default_season_env() -> int | None:
    raw = (os.getenv("READ_API_DEFAULT_SEASON") or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except Exception:
        return None


def _require_season(season: int | None) -> int:
    s = season if season is not None else _default_season_env()
    if s is None:
        raise HTTPException(status_code=400, detail="season_required")
    try:
        return int(s)
    except Exception:
        raise HTTPException(status_code=400, detail="invalid_season")


def _safe_limit(limit: int, *, cap: int) -> int:
    return max(1, min(int(limit), int(cap)))


def _safe_offset(offset: int) -> int:
    return max(0, int(offset))


def _get_tracked_league_ids() -> set[int]:
    """
    Load tracked league IDs from config/jobs/daily.yaml -> tracked_leagues[*].id.
    Returns empty set if config is missing or invalid.
    """
    project_root = Path(__file__).resolve().parents[2]
    cfg_path = Path(os.getenv("API_FOOTBALL_DAILY_CONFIG", str(project_root / "config" / "jobs" / "daily.yaml")))
    if not cfg_path.exists():
        return set()
    try:
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return set()
    tracked = cfg.get("tracked_leagues") or []
    ids: set[int] = set()
    if isinstance(tracked, list):
        for x in tracked:
            if not isinstance(x, dict) or "id" not in x:
                continue
            try:
                ids.add(int(x["id"]))
            except Exception:
                continue
    return ids


def _resolve_league_ids(*, league_id: int | None, country: str | None, season: int | None) -> list[int]:
    """
    Resolve scope to a list of league_ids.
    - If league_id is provided, returns [league_id]
    - Else if country is provided, returns all league_ids matching country_name/country_code
      and optionally filtered by season presence in core.leagues.seasons JSONB.
    """
    if league_id is not None:
        return [int(league_id)]
    c = (country or "").strip()
    if not c:
        return []

    filters: list[str] = []
    params: list[Any] = []

    # Match both country_name and country_code (case-insensitive)
    filters.append("AND (l.country_name ILIKE %s OR l.country_code ILIKE %s)")
    params.append(c)
    params.append(c)

    if season is not None:
        # seasons is stored as JSONB array (API schema) - filter if year exists.
        filters.append(
            """
            AND (
              l.seasons IS NULL
              OR EXISTS (
                SELECT 1
                FROM jsonb_array_elements(l.seasons) s
                WHERE (s->>'year')::int = %s
              )
            )
            """.strip()
        )
        params.append(int(season))

    sql_text = f"""
    SELECT l.id
    FROM core.leagues l
    WHERE 1=1
    {' '.join(filters)}
    ORDER BY l.id ASC
    """

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql_text, tuple(params))
            rows = cur.fetchall()
        conn.commit()
    return [int(r[0]) for r in rows if r and r[0] is not None]


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


@app.get("/v1/health", dependencies=[Depends(require_only_query_params(set()))])
async def health() -> Response:
    try:
        row = await _fetchone_async("SELECT 1;", ())
        return JSONResponse(content={"ok": True, "db": bool(row and row[0] == 1)})
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@app.get(
    "/v1/quota",
    dependencies=[Depends(require_access), Depends(require_only_query_params(set()))],
)
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


@app.get(
    "/v1/fixtures",
    dependencies=[Depends(require_access), Depends(require_only_query_params({"league_id", "date", "status", "limit"}))],
)
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


@app.get(
    "/v1/teams/{team_id}/fixtures",
    dependencies=[Depends(require_access), Depends(require_only_query_params({"from_date", "to_date", "status", "limit"}))],
)
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


@app.get(
    "/v1/fixtures/{fixture_id}/details",
    dependencies=[Depends(require_access), Depends(require_only_query_params(set()))],
)
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


@app.get(
    "/v1/h2h",
    dependencies=[Depends(require_access), Depends(require_only_query_params({"home_team_id", "away_team_id", "limit"}))],
)
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


@app.get(
    "/v1/teams/{team_id}/metrics",
    dependencies=[Depends(require_access), Depends(require_only_query_params({"last_n", "as_of_date"}))],
)
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
        # Totals across the sample window (useful for e.g. "last 20 matches total corners")
        # Notes:
        # - totals are computed only where the stat exists in the underlying per-fixture statistics
        # - possession is included for parity with avg but is not a meaningful "total" (use avg)
        "match_stats_sum": {
            "total_shots": (stat_sums.get("total_shots") if stat_counts.get("total_shots", 0) > 0 else None),
            "shots_on_goal": (stat_sums.get("shots_on_goal") if stat_counts.get("shots_on_goal", 0) > 0 else None),
            "corner_kicks": (stat_sums.get("corner_kicks") if stat_counts.get("corner_kicks", 0) > 0 else None),
            "yellow_cards": (stat_sums.get("yellow_cards") if stat_counts.get("yellow_cards", 0) > 0 else None),
            "red_cards": (stat_sums.get("red_cards") if stat_counts.get("red_cards", 0) > 0 else None),
            "ball_possession_pct": (stat_sums.get("ball_possession") if stat_counts.get("ball_possession", 0) > 0 else None),
            "offsides": (stat_sums.get("offsides") if stat_counts.get("offsides", 0) > 0 else None),
        },
        # How many matches contributed to each stat (because some leagues/fixtures may not provide stats)
        "match_stats_count": {
            "total_shots": int(stat_counts.get("total_shots", 0)),
            "shots_on_goal": int(stat_counts.get("shots_on_goal", 0)),
            "corner_kicks": int(stat_counts.get("corner_kicks", 0)),
            "yellow_cards": int(stat_counts.get("yellow_cards", 0)),
            "red_cards": int(stat_counts.get("red_cards", 0)),
            "ball_possession_pct": int(stat_counts.get("ball_possession", 0)),
            "offsides": int(stat_counts.get("offsides", 0)),
        },
        "fixtures_sample": fixtures[: min(len(fixtures), 20)],
    }


@app.get(
    "/v1/standings/{league_id}/{season}",
    dependencies=[Depends(require_access), Depends(require_only_query_params(set()))],
)
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


@app.get(
    "/v1/teams",
    dependencies=[Depends(require_access), Depends(require_only_query_params({"search", "league_id", "limit"}))],
)
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


@app.get(
    "/v1/injuries",
    dependencies=[Depends(require_access), Depends(require_only_query_params({"league_id", "season", "team_id", "player_id", "limit"}))],
)
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
            "core_top_scorers": int(stats_row[12]),
            "core_team_statistics": int(stats_row[13]),
            "raw_last_fetched_at_utc": _to_iso_or_none(stats_row[14]),
            "core_fixtures_last_updated_at_utc": _to_iso_or_none(stats_row[15]),
        }

    return {"quota": quota, "db": stats}


# -----------------------------
# Curated Read API (Feature Store)
# -----------------------------

LEAGUES_LIST_SQL = """
SELECT
  l.id,
  l.name,
  l.type,
  l.logo,
  l.country_name,
  l.country_code,
  l.country_flag,
  l.seasons,
  l.updated_at
FROM core.leagues l
WHERE 1=1
  {filters}
ORDER BY l.country_name NULLS LAST, l.name ASC
LIMIT %s OFFSET %s
"""

FIXTURES_READ_SQL = """
SELECT
  f.id,
  f.league_id,
  l.name AS league_name,
  f.season,
  f.round,
  f.date,
  f.status_short,
  f.status_long,
  f.elapsed,
  f.needs_score_verification,
  f.verification_state,
  f.verification_attempt_count,
  f.verification_last_attempt_at,
  f.home_team_id,
  th.name AS home_team_name,
  f.away_team_id,
  ta.name AS away_team_name,
  f.goals_home,
  f.goals_away,
  f.score,
  f.updated_at
FROM core.fixtures f
JOIN core.leagues l ON l.id = f.league_id
JOIN core.teams th ON th.id = f.home_team_id
JOIN core.teams ta ON ta.id = f.away_team_id
WHERE 1=1
  {filters}
ORDER BY f.date DESC
LIMIT %s OFFSET %s
"""

TOP_SCORERS_READ_SQL = """
SELECT
  ts.league_id,
  l.name AS league_name,
  ts.season,
  ts.player_id,
  ts.rank,
  ts.team_id,
  ts.team_name,
  ts.goals,
  ts.assists,
  ts.raw,
  ts.updated_at
FROM core.top_scorers ts
JOIN core.leagues l ON l.id = ts.league_id
WHERE ts.league_id = %s
  AND ts.season = %s
ORDER BY ts.rank ASC NULLS LAST, ts.goals DESC NULLS LAST
LIMIT %s OFFSET %s
"""

TEAM_STATISTICS_READ_SQL = """
SELECT
  s.league_id,
  l.name AS league_name,
  s.season,
  s.team_id,
  t.name AS team_name,
  s.form,
  s.raw,
  s.updated_at
FROM core.team_statistics s
JOIN core.leagues l ON l.id = s.league_id
JOIN core.teams t ON t.id = s.team_id
WHERE s.league_id = %s
  AND s.season = %s
  {team_filter}
ORDER BY t.name ASC
LIMIT %s OFFSET %s
"""

READ_COVERAGE_SQL = """
SELECT
  c.league_id,
  l.name AS league_name,
  c.season,
  c.endpoint,
  c.expected_count,
  c.actual_count,
  c.count_coverage,
  c.last_update,
  c.lag_minutes,
  c.freshness_coverage,
  c.raw_count,
  c.core_count,
  c.pipeline_coverage,
  c.overall_coverage,
  c.calculated_at,
  c.flags
FROM mart.coverage_status c
JOIN core.leagues l ON l.id = c.league_id
WHERE 1=1
  {filters}
ORDER BY c.overall_coverage DESC NULLS LAST, c.calculated_at DESC
LIMIT %s OFFSET %s
"""

READ_COUNTRIES_SQL = """
SELECT
  l.country_name,
  l.country_code,
  l.country_flag,
  COUNT(*)::int AS leagues_count
FROM core.leagues l
WHERE 1=1
  AND l.country_name IS NOT NULL
  AND btrim(l.country_name) <> ''
  {filters}
GROUP BY l.country_name, l.country_code, l.country_flag
ORDER BY l.country_name ASC NULLS LAST
LIMIT %s OFFSET %s
"""


@app.get(
    "/read/leagues",
    dependencies=[Depends(require_access), Depends(require_only_query_params({"country", "season", "limit", "offset"}))],
)
async def read_leagues(
    country: str | None = None,
    season: int | None = None,
    limit: int = 200,
    offset: int = 0,
) -> dict[str, Any]:
    safe_limit = _safe_limit(limit, cap=500)
    safe_offset = _safe_offset(offset)
    filters: list[str] = []
    params: list[Any] = []

    # Restrict to tracked leagues only
    tracked_ids = _get_tracked_league_ids()
    if not tracked_ids:
        # If no tracked leagues configured, return empty result
        return {"ok": True, "items": [], "paging": {"limit": safe_limit, "offset": safe_offset}}
    filters.append("AND l.id = ANY(%s)")
    params.append(list(tracked_ids))

    c = (country or "").strip()
    if c:
        filters.append("AND (l.country_name ILIKE %s OR l.country_code ILIKE %s)")
        params.append(c)
        params.append(c)

    if season is not None:
        filters.append(
            """
            AND (
              l.seasons IS NULL
              OR EXISTS (
                SELECT 1 FROM jsonb_array_elements(l.seasons) s
                WHERE (s->>'year')::int = %s
              )
            )
            """.strip()
        )
        params.append(int(season))

    sql_text = LEAGUES_LIST_SQL.format(filters="\n  ".join(filters))
    params.extend([safe_limit, safe_offset])
    rows = await _fetchall_async(sql_text, tuple(params))

    items: list[dict[str, Any]] = []
    for r in rows:
        items.append(
            {
                "id": int(r[0]),
                "name": r[1],
                "type": r[2],
                "logo": r[3],
                "country_name": r[4],
                "country_code": r[5],
                "country_flag": r[6],
                "seasons": r[7],
                "updated_at_utc": _to_iso_or_none(r[8]),
            }
        )
    return {"ok": True, "items": items, "paging": {"limit": safe_limit, "offset": safe_offset}}


@app.get(
    "/read/countries",
    dependencies=[Depends(require_access), Depends(require_only_query_params({"season", "q", "limit", "offset"}))],
)
async def read_countries(
    season: int | None = None,
    q: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> dict[str, Any]:
    """
    Hierarchy root: list countries present in core.leagues.
    Intended flow:
      /read/countries -> pick country -> /read/leagues?country=... -> pick league -> /read/fixtures, /read/h2h, ...
    """
    safe_limit = _safe_limit(limit, cap=500)
    safe_offset = _safe_offset(offset)
    filters: list[str] = []
    params: list[Any] = []

    # Restrict to tracked leagues only
    tracked_ids = _get_tracked_league_ids()
    if not tracked_ids:
        # If no tracked leagues configured, return empty result
        return {"ok": True, "items": [], "paging": {"limit": safe_limit, "offset": safe_offset}}
    filters.append("AND l.id = ANY(%s)")
    params.append(list(tracked_ids))

    query = (q or "").strip()
    if query:
        filters.append("AND (l.country_name ILIKE %s OR l.country_code ILIKE %s)")
        params.append(f"%{query}%")
        params.append(f"%{query}%")

    if season is not None:
        filters.append(
            """
            AND (
              l.seasons IS NULL
              OR EXISTS (
                SELECT 1 FROM jsonb_array_elements(l.seasons) s
                WHERE (s->>'year')::int = %s
              )
            )
            """.strip()
        )
        params.append(int(season))

    sql_text = READ_COUNTRIES_SQL.format(filters="\n  ".join(filters))
    params.extend([safe_limit, safe_offset])
    rows = await _fetchall_async(sql_text, tuple(params))

    items: list[dict[str, Any]] = []
    for r in rows:
        items.append(
            {
                "country_name": r[0],
                "country_code": r[1],
                "country_flag": r[2],
                "leagues_count": _to_int_or_none(r[3]),
            }
        )
    return {"ok": True, "items": items, "paging": {"limit": safe_limit, "offset": safe_offset}}

@app.get(
    "/read/fixtures",
    dependencies=[
        Depends(require_access),
        Depends(
            require_only_query_params(
                {
                    "league_id",
                    "country",
                    "season",
                    "date_from",
                    "date_to",
                    "team_id",
                    "status",
                    "limit",
                    "offset",
                    # Data-quality / operational filters (read-only)
                    "needs_score_verification",
                    "verification_state",
                    "min_verification_attempt_count",
                    "has_events",
                    "has_lineups",
                    "has_statistics",
                    "has_players",
                }
            )
        ),
    ],
)
async def read_fixtures(
    league_id: int | None = None,
    country: str | None = None,
    season: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    team_id: int | None = None,
    status: str | None = None,
    needs_score_verification: bool | None = None,
    verification_state: str | None = None,
    min_verification_attempt_count: int | None = None,
    has_events: bool | None = None,
    has_lineups: bool | None = None,
    has_statistics: bool | None = None,
    has_players: bool | None = None,
    limit: int = 200,
    offset: int = 0,
) -> dict[str, Any]:
    s = _require_season(season)
    safe_limit = _safe_limit(limit, cap=500)
    safe_offset = _safe_offset(offset)

    league_ids = _resolve_league_ids(league_id=league_id, country=country, season=s)
    if league_id is None and (country is None or not str(country).strip()):
        raise HTTPException(status_code=400, detail="league_id_or_country_required")
    if not league_ids:
        return {"ok": True, "items": [], "paging": {"limit": safe_limit, "offset": safe_offset}}

    filters: list[str] = []
    params: list[Any] = []
    filters.append("AND f.league_id = ANY(%s)")
    params.append(league_ids)
    filters.append("AND f.season = %s")
    params.append(int(s))

    if status is not None:
        filters.append("AND f.status_short = %s")
        params.append(str(status))

    if team_id is not None:
        filters.append("AND (f.home_team_id = %s OR f.away_team_id = %s)")
        params.extend([int(team_id), int(team_id)])

    if date_from is not None:
        try:
            d = _parse_ymd(str(date_from))
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e))
        filters.append("AND DATE(f.date AT TIME ZONE 'UTC') >= %s")
        params.append(d.isoformat())
    if date_to is not None:
        try:
            d = _parse_ymd(str(date_to))
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e))
        filters.append("AND DATE(f.date AT TIME ZONE 'UTC') <= %s")
        params.append(d.isoformat())

    # --- Verification / data-quality filters (read-only, optional) ---
    if needs_score_verification is not None:
        filters.append("AND f.needs_score_verification = %s")
        params.append(bool(needs_score_verification))

    if verification_state is not None:
        vs = str(verification_state).strip().lower()
        allowed_vs = {"pending", "verified", "not_found", "blocked"}
        if vs not in allowed_vs:
            raise HTTPException(status_code=400, detail={"error": "invalid_verification_state", "allowed": sorted(allowed_vs)})
        filters.append("AND COALESCE(f.verification_state, 'pending') = %s")
        params.append(vs)

    if min_verification_attempt_count is not None:
        try:
            mac = int(min_verification_attempt_count)
        except Exception:
            raise HTTPException(status_code=400, detail="invalid_min_verification_attempt_count")
        filters.append("AND COALESCE(f.verification_attempt_count, 0) >= %s")
        params.append(mac)

    def _exists_filter(*, flag: bool | None, sql_exists: str) -> None:
        if flag is None:
            return
        if bool(flag):
            filters.append(f"AND EXISTS ({sql_exists})")
        else:
            filters.append(f"AND NOT EXISTS ({sql_exists})")

    _exists_filter(
        flag=has_events,
        sql_exists="SELECT 1 FROM core.fixture_events e WHERE e.fixture_id = f.id",
    )
    _exists_filter(
        flag=has_lineups,
        sql_exists="SELECT 1 FROM core.fixture_lineups l WHERE l.fixture_id = f.id",
    )
    _exists_filter(
        flag=has_statistics,
        sql_exists="SELECT 1 FROM core.fixture_statistics s WHERE s.fixture_id = f.id",
    )
    _exists_filter(
        flag=has_players,
        sql_exists="SELECT 1 FROM core.fixture_players p WHERE p.fixture_id = f.id",
    )

    sql_text = FIXTURES_READ_SQL.format(filters="\n  ".join(filters))
    params.extend([safe_limit, safe_offset])
    rows = await _fetchall_async(sql_text, tuple(params))

    items: list[dict[str, Any]] = []
    for r in rows:
        items.append(
            {
                "id": int(r[0]),
                "league_id": int(r[1]),
                "league_name": r[2],
                "season": _to_int_or_none(r[3]),
                "round": r[4],
                "date_utc": _to_iso_or_none(r[5]),
                "status_short": r[6],
                "status_long": r[7],
                "elapsed": _to_int_or_none(r[8]),
                "needs_score_verification": bool(r[9]) if r[9] is not None else None,
                "verification_state": (str(r[10]) if r[10] is not None else None),
                "verification_attempt_count": _to_int_or_none(r[11]),
                "verification_last_attempt_at_utc": _to_iso_or_none(r[12]),
                "home_team_id": _to_int_or_none(r[13]),
                "home_team_name": r[14],
                "away_team_id": _to_int_or_none(r[15]),
                "away_team_name": r[16],
                "goals_home": _to_int_or_none(r[17]),
                "goals_away": _to_int_or_none(r[18]),
                "score": r[19],
                "updated_at_utc": _to_iso_or_none(r[20]),
            }
        )
    return {"ok": True, "items": items, "paging": {"limit": safe_limit, "offset": safe_offset}}


@app.get(
    "/read/fixtures/{fixture_id}",
    dependencies=[Depends(require_access), Depends(require_only_query_params(set()))],
)
async def read_fixture(fixture_id: int) -> dict[str, Any]:
    # Reuse FIXTURES_READ_SQL with id filter, limit 1
    filters = "AND f.id = %s"
    sql_text = FIXTURES_READ_SQL.format(filters=filters)
    rows = await _fetchall_async(sql_text, (int(fixture_id), 1, 0))
    if not rows:
        raise HTTPException(status_code=404, detail="fixture_not_found")
    r = rows[0]
    return {
        "ok": True,
        "item": {
            "id": int(r[0]),
            "league_id": int(r[1]),
            "league_name": r[2],
            "season": _to_int_or_none(r[3]),
            "round": r[4],
            "date_utc": _to_iso_or_none(r[5]),
            "status_short": r[6],
            "status_long": r[7],
            "elapsed": _to_int_or_none(r[8]),
            "needs_score_verification": bool(r[9]) if r[9] is not None else None,
            "verification_state": (str(r[10]) if r[10] is not None else None),
            "verification_attempt_count": _to_int_or_none(r[11]),
            "verification_last_attempt_at_utc": _to_iso_or_none(r[12]),
            "home_team_id": _to_int_or_none(r[13]),
            "home_team_name": r[14],
            "away_team_id": _to_int_or_none(r[15]),
            "away_team_name": r[16],
            "goals_home": _to_int_or_none(r[17]),
            "goals_away": _to_int_or_none(r[18]),
            "score": r[19],
            "updated_at_utc": _to_iso_or_none(r[20]),
        },
    }


@app.get(
    "/read/fixtures/{fixture_id}/events",
    dependencies=[Depends(require_access), Depends(require_only_query_params({"limit"}))],
)
async def read_fixture_events(fixture_id: int, limit: int = 5000) -> dict[str, Any]:
    # Snapshot + freshness-aware fallback to normalized table
    snapshot_row = await _fetchone_async(mcp_queries.FIXTURE_DETAILS_SNAPSHOT_QUERY, (int(fixture_id),))
    snapshot_events = snapshot_row[1] if snapshot_row else None
    snapshot_updated_at = snapshot_row[5] if snapshot_row else None

    # Normalized table stats
    table_stats = await _fetchone_async(
        """
        SELECT COUNT(*) AS cnt, MAX(updated_at) AS last_upd
        FROM core.fixture_events
        WHERE fixture_id = %s
        """,
        (int(fixture_id),),
    )
    table_count = int(table_stats[0]) if table_stats and table_stats[0] is not None else 0
    table_last_upd = table_stats[1] if table_stats else None

    # Last RAW fetch time for events
    last_raw_row = await _fetchone_async(
        """
        SELECT MAX(fetched_at) FROM raw.api_responses
        WHERE endpoint = '/fixtures/events'
          AND (requested_params->>'fixture')::bigint = %s
        """,
        (int(fixture_id),),
    )
    last_raw_events = last_raw_row[0] if last_raw_row else None

    safe_limit = _safe_limit(limit, cap=10000)

    def _snapshot_len(ev: Any) -> int:
        try:
            return len(ev) if isinstance(ev, list) else 0
        except Exception:
            return 0

    # Decide source: if normalized has more rows or is fresher than snapshot, use normalized.
    use_normalized = False
    if table_count > _snapshot_len(snapshot_events):
        use_normalized = True
    elif last_raw_events and snapshot_updated_at and last_raw_events > snapshot_updated_at:
        use_normalized = True

    if not use_normalized and snapshot_events is not None:
        return {
            "ok": True,
            "fixture_id": int(fixture_id),
            "items": snapshot_events[:safe_limit] if isinstance(snapshot_events, list) else snapshot_events,
            "source": "core.fixture_details",
        }

    # Fallback or preferred: normalized table
    rows = await _fetchall_async(mcp_queries.FIXTURE_EVENTS_QUERY, (int(fixture_id), safe_limit))
    items: list[dict[str, Any]] = []
    for r in rows:
        items.append(
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
    return {"ok": True, "fixture_id": int(fixture_id), "items": items, "source": "core.fixture_events"}


@app.get(
    "/read/fixtures/{fixture_id}/lineups",
    dependencies=[Depends(require_access), Depends(require_only_query_params(set()))],
)
async def read_fixture_lineups(fixture_id: int) -> dict[str, Any]:
    # Önce snapshot'ı kontrol et
    snapshot_row = await _fetchone_async(mcp_queries.FIXTURE_DETAILS_SNAPSHOT_QUERY, (int(fixture_id),))
    if snapshot_row and snapshot_row[2] is not None:  # lineups kolonu (index 2)
        return {
            "ok": True,
            "fixture_id": int(fixture_id),
            "items": snapshot_row[2],  # lineups JSONB'den direkt döndür
            "source": "core.fixture_details",
        }
    
    # Fallback: normalized tablo
    rows = await _fetchall_async(mcp_queries.FIXTURE_LINEUPS_QUERY, (int(fixture_id),))
    items: list[dict[str, Any]] = []
    for r in rows:
        items.append(
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
    return {"ok": True, "fixture_id": int(fixture_id), "items": items, "source": "core.fixture_lineups"}


@app.get(
    "/read/fixtures/{fixture_id}/statistics",
    dependencies=[Depends(require_access), Depends(require_only_query_params(set()))],
)
async def read_fixture_statistics(fixture_id: int) -> dict[str, Any]:
    # Önce snapshot'ı kontrol et
    snapshot_row = await _fetchone_async(mcp_queries.FIXTURE_DETAILS_SNAPSHOT_QUERY, (int(fixture_id),))
    if snapshot_row and snapshot_row[3] is not None:  # statistics kolonu (index 3)
        return {
            "ok": True,
            "fixture_id": int(fixture_id),
            "items": snapshot_row[3],  # statistics JSONB'den direkt döndür
            "source": "core.fixture_details",
        }
    
    # Fallback: normalized tablo
    rows = await _fetchall_async(mcp_queries.FIXTURE_STATISTICS_QUERY, (int(fixture_id),))
    items: list[dict[str, Any]] = []
    for r in rows:
        items.append(
            {
                "fixture_id": int(r[0]),
                "team_id": _to_int_or_none(r[1]),
                "statistics": r[2],
                "updated_at_utc": _to_iso_or_none(r[3]),
            }
        )
    return {"ok": True, "fixture_id": int(fixture_id), "items": items, "source": "core.fixture_statistics"}


@app.get(
    "/read/fixtures/{fixture_id}/players",
    dependencies=[Depends(require_access), Depends(require_only_query_params({"team_id", "limit"}))],
)
async def read_fixture_players(
    fixture_id: int, team_id: int | None = None, limit: int = 5000
) -> dict[str, Any]:
    # Önce snapshot'ı kontrol et
    snapshot_row = await _fetchone_async(mcp_queries.FIXTURE_DETAILS_SNAPSHOT_QUERY, (int(fixture_id),))
    if snapshot_row and snapshot_row[4] is not None:  # players kolonu (index 4)
        players_data = snapshot_row[4]
        # team_id filtresi varsa uygula
        if team_id is not None and isinstance(players_data, list):
            players_data = [p for p in players_data if isinstance(p, dict) and p.get("team_id") == team_id]
        safe_limit = _safe_limit(limit, cap=20000)
        return {
            "ok": True,
            "fixture_id": int(fixture_id),
            "items": players_data[:safe_limit] if isinstance(players_data, list) else players_data,
            "source": "core.fixture_details",
        }
    
    # Fallback: normalized tablo
    safe_limit = _safe_limit(limit, cap=20000)
    if team_id is not None:
        sql_text = mcp_queries.FIXTURE_PLAYERS_QUERY.format(team_filter="AND team_id = %s")
        rows = await _fetchall_async(sql_text, (int(fixture_id), int(team_id), safe_limit))
    else:
        sql_text = mcp_queries.FIXTURE_PLAYERS_QUERY.format(team_filter="")
        rows = await _fetchall_async(sql_text, (int(fixture_id), safe_limit))
    items: list[dict[str, Any]] = []
    for r in rows:
        items.append(
            {
                "fixture_id": int(r[0]),
                "team_id": _to_int_or_none(r[1]),
                "player_id": _to_int_or_none(r[2]),
                "player_name": r[3],
                "statistics": r[4],
                "updated_at_utc": _to_iso_or_none(r[5]),
            }
        )
    return {"ok": True, "fixture_id": int(fixture_id), "items": items, "source": "core.fixture_players"}


@app.get(
    "/read/standings",
    dependencies=[Depends(require_access), Depends(require_only_query_params({"league_id", "season"}))],
)
async def read_standings(league_id: int, season: int | None = None) -> dict[str, Any]:
    s = _require_season(season)
    rows = await _fetchall_async(mcp_queries.STANDINGS_QUERY, (int(league_id), int(s)))
    items: list[dict[str, Any]] = []
    for r in rows:
        items.append(
            {
                "league_id": int(r[0]),
                "season": int(r[1]),
                "team_id": int(r[2]),
                "team_name": r[3],
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
    return {"ok": True, "league_id": int(league_id), "season": int(s), "items": items}


@app.get(
    "/read/injuries",
    dependencies=[Depends(require_access), Depends(require_only_query_params({"league_id", "country", "season", "team_id", "player_id", "limit", "offset"}))],
)
async def read_injuries(
    league_id: int | None = None,
    country: str | None = None,
    season: int | None = None,
    team_id: int | None = None,
    player_id: int | None = None,
    limit: int = 200,
    offset: int = 0,
) -> dict[str, Any]:
    s = _require_season(season)
    safe_limit = _safe_limit(limit, cap=1000)
    safe_offset = _safe_offset(offset)

    league_ids = _resolve_league_ids(league_id=league_id, country=country, season=s)
    if league_id is None and (country is None or not str(country).strip()):
        raise HTTPException(status_code=400, detail="league_id_or_country_required")
    if not league_ids:
        return {"ok": True, "items": [], "paging": {"limit": safe_limit, "offset": safe_offset}}

    filters: list[str] = []
    params: list[Any] = []
    filters.append("AND i.league_id = ANY(%s)")
    params.append(league_ids)
    filters.append("AND i.season = %s")
    params.append(int(s))
    if team_id is not None:
        filters.append("AND i.team_id = %s")
        params.append(int(team_id))
    if player_id is not None:
        filters.append("AND i.player_id = %s")
        params.append(int(player_id))

    # NOTE: INJURIES_QUERY doesn't support OFFSET; implement a local SQL with offset for read API.
    sql_text = f"""
    SELECT
      i.league_id,
      i.season,
      i.team_id,
      i.player_id,
      i.player_name,
      i.team_name,
      i.type,
      i.reason,
      i.severity,
      i.date,
      i.updated_at
    FROM core.injuries i
    WHERE 1=1
      {' '.join(filters)}
    ORDER BY i.updated_at DESC NULLS LAST
    LIMIT %s OFFSET %s
    """
    params.extend([safe_limit, safe_offset])
    rows = await _fetchall_async(sql_text, tuple(params))

    items: list[dict[str, Any]] = []
    for r in rows:
        items.append(
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
    return {"ok": True, "items": items, "paging": {"limit": safe_limit, "offset": safe_offset}}


@app.get(
    "/read/top_scorers",
    dependencies=[Depends(require_access), Depends(require_only_query_params({"league_id", "season", "include_raw", "limit", "offset"}))],
)
async def read_top_scorers(
    league_id: int,
    season: int | None = None,
    include_raw: bool = True,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    s = _require_season(season)
    safe_limit = _safe_limit(limit, cap=500)
    safe_offset = _safe_offset(offset)
    rows = await _fetchall_async(TOP_SCORERS_READ_SQL, (int(league_id), int(s), safe_limit, safe_offset))
    items: list[dict[str, Any]] = []
    for r in rows:
        items.append(
            {
                "league_id": int(r[0]),
                "league_name": r[1],
                "season": int(r[2]),
                "player_id": _to_int_or_none(r[3]),
                "rank": _to_int_or_none(r[4]),
                "team_id": _to_int_or_none(r[5]),
                "team_name": r[6],
                "goals": _to_int_or_none(r[7]),
                "assists": _to_int_or_none(r[8]),
                "raw": (r[9] if include_raw else None),
                "updated_at_utc": _to_iso_or_none(r[10]),
            }
        )
    return {"ok": True, "league_id": int(league_id), "season": int(s), "items": items, "paging": {"limit": safe_limit, "offset": safe_offset}}


@app.get(
    "/read/team_statistics",
    dependencies=[Depends(require_access), Depends(require_only_query_params({"league_id", "season", "team_id", "include_raw", "limit", "offset"}))],
)
async def read_team_statistics(
    league_id: int,
    season: int | None = None,
    team_id: int | None = None,
    include_raw: bool = True,
    limit: int = 200,
    offset: int = 0,
) -> dict[str, Any]:
    s = _require_season(season)
    safe_limit = _safe_limit(limit, cap=2000)
    safe_offset = _safe_offset(offset)
    team_filter = ""
    params: list[Any] = [int(league_id), int(s)]
    if team_id is not None:
        team_filter = "AND s.team_id = %s"
        params.append(int(team_id))
    sql_text = TEAM_STATISTICS_READ_SQL.format(team_filter=team_filter)
    params.extend([safe_limit, safe_offset])
    rows = await _fetchall_async(sql_text, tuple(params))
    items: list[dict[str, Any]] = []
    for r in rows:
        items.append(
            {
                "league_id": int(r[0]),
                "league_name": r[1],
                "season": int(r[2]),
                "team_id": int(r[3]),
                "team_name": r[4],
                "form": r[5],
                "raw": (r[6] if include_raw else None),
                "updated_at_utc": _to_iso_or_none(r[7]),
            }
        )
    return {"ok": True, "league_id": int(league_id), "season": int(s), "items": items, "paging": {"limit": safe_limit, "offset": safe_offset}}

@app.get(
    "/read/coverage",
    dependencies=[Depends(require_access), Depends(require_only_query_params({"season", "league_id", "country", "endpoint", "limit", "offset"}))],
)
async def read_coverage(
    league_id: int | None = None,
    country: str | None = None,
    season: int | None = None,
    endpoint: str | None = None,
    limit: int = 500,
    offset: int = 0,
) -> dict[str, Any]:
    s = _require_season(season)
    safe_limit = _safe_limit(limit, cap=2000)
    safe_offset = _safe_offset(offset)

    league_ids = _resolve_league_ids(league_id=league_id, country=country, season=s) if (league_id is not None or (country or "").strip()) else []

    filters: list[str] = []
    params: list[Any] = []
    filters.append("AND c.season = %s")
    params.append(int(s))

    if league_ids:
        filters.append("AND c.league_id = ANY(%s)")
        params.append(league_ids)

    ep = (endpoint or "").strip()
    if ep:
        filters.append("AND c.endpoint = %s")
        params.append(ep)

    sql_text = READ_COVERAGE_SQL.format(filters="\n  ".join(filters))
    params.extend([safe_limit, safe_offset])
    rows = await _fetchall_async(sql_text, tuple(params))

    items: list[dict[str, Any]] = []
    for r in rows:
        items.append(
            {
                "league_id": int(r[0]),
                "league_name": r[1],
                "season": int(r[2]),
                "endpoint": r[3],
                "expected_count": _to_int_or_none(r[4]),
                "actual_count": _to_int_or_none(r[5]),
                "count_coverage": r[6],
                "last_update_utc": _to_iso_or_none(r[7]),
                "lag_minutes": _to_int_or_none(r[8]),
                "freshness_coverage": r[9],
                "raw_count": _to_int_or_none(r[10]),
                "core_count": _to_int_or_none(r[11]),
                "pipeline_coverage": r[12],
                "overall_coverage": r[13],
                "calculated_at_utc": _to_iso_or_none(r[14]),
                "flags": (r[15] if len(r) > 15 else None),
            }
        )

    return {
        "ok": True,
        "filters": {"league_id": league_id, "country": country, "season": int(s), "endpoint": ep or None},
        "items": items,
        "paging": {"limit": safe_limit, "offset": safe_offset},
    }


@app.get(
    "/read/h2h",
    dependencies=[Depends(require_access), Depends(require_only_query_params({"team1_id", "team2_id", "league_id", "season", "limit", "offset", "force_api"}))],
)
async def read_h2h(
    team1_id: int,
    team2_id: int,
    league_id: int | None = None,
    season: int | None = None,
    limit: int = 20,
    offset: int = 0,
    force_api: bool = False,
) -> dict[str, Any]:
    """
    Head-to-head fixtures + summary (W/D/L, goals) derived from core.fixtures.
    Optionally filter by league_id and/or season.
    
    If force_api=true or DB results are insufficient, fetches from API-Football /fixtures/headtohead
    and stores results in core.fixtures before returning.
    
    Parameters:
    - limit: Maximum number of results (default: 20, max: 200)
    - offset: Pagination offset (default: 0)
    - force_api: If true, always fetch from API-Football instead of using DB cache
    """
    safe_limit = _safe_limit(limit, cap=200)
    safe_offset = _safe_offset(offset)

    # First, try to get results from DB (unless force_api is true)
    if not force_api:
        filters: list[str] = []
        params: list[Any] = [int(team1_id), int(team2_id), int(team2_id), int(team1_id)]

        if league_id is not None:
            filters.append("AND f.league_id = %s")
            params.append(int(league_id))

        if season is not None:
            filters.append("AND f.season = %s")
            params.append(int(season))

        sql_text = f"""
        SELECT
          f.id,
          f.league_id,
          f.season,
          f.date,
          f.status_short,
          f.home_team_id,
          th.name as home_team_name,
          f.away_team_id,
          ta.name as away_team_name,
          f.goals_home,
          f.goals_away,
          f.updated_at
        FROM core.fixtures f
        JOIN core.teams th ON f.home_team_id = th.id
        JOIN core.teams ta ON f.away_team_id = ta.id
        WHERE (
          (f.home_team_id = %s AND f.away_team_id = %s)
          OR
          (f.home_team_id = %s AND f.away_team_id = %s)
        )
        {' '.join(filters)}
        ORDER BY f.date DESC
        LIMIT %s OFFSET %s
        """
        params.append(safe_limit)
        params.append(safe_offset)
        rows = await _fetchall_async(sql_text, tuple(params))

        # If we have any cached results, return them.
        # NOTE: H2H is naturally a "small" dataset. Requiring len(rows) >= limit would
        # unnecessarily force API calls (and DB writes) in typical use and in unit tests.
        if rows:
            items: list[dict[str, Any]] = []
            played = wins = draws = losses = 0
            gf = ga = 0
            for r in rows:
                fid = int(r[0])
                hid = _to_int_or_none(r[5])
                aid = _to_int_or_none(r[7])
                gh = _to_int_or_none(r[9])
                ga_ = _to_int_or_none(r[10])
                items.append(
                    {
                        "id": fid,
                        "league_id": int(r[1]),
                        "season": _to_int_or_none(r[2]),
                        "date_utc": _to_iso_or_none(r[3]),
                        "status": r[4],
                        "home_team_id": hid,
                        "home_team": r[6],
                        "away_team_id": aid,
                        "away_team": r[8],
                        "goals_home": gh,
                        "goals_away": ga_,
                        "updated_at_utc": _to_iso_or_none(r[11]),
                    }
                )

                if hid is None or aid is None or gh is None or ga_ is None:
                    continue
                played += 1
                team1_is_home = int(hid) == int(team1_id)
                team1_goals = int(gh) if team1_is_home else int(ga_)
                team2_goals = int(ga_) if team1_is_home else int(gh)
                gf += team1_goals
                ga += team2_goals
                if team1_goals > team2_goals:
                    wins += 1
                elif team1_goals < team2_goals:
                    losses += 1
                else:
                    draws += 1

            return {
                "ok": True,
                "teams": {"team1_id": int(team1_id), "team2_id": int(team2_id)},
                "filters": {"league_id": int(league_id) if league_id is not None else None, "season": int(season) if season is not None else None},
                "summary_team1": {
                    "played": played,
                    "wins": wins,
                    "draws": draws,
                    "losses": losses,
                    "goals_for": gf,
                    "goals_against": ga,
                    "goals_for_avg": (round(gf / played, 4) if played else None),
                    "goals_against_avg": (round(ga / played, 4) if played else None),
                },
                "items": items,
                "source": "database",
            }

    # Fetch from API-Football /fixtures/headtohead
    client = get_api_client()
    limiter = get_rate_limiter()

    # API-Football headtohead endpoint format: h2h=team1-team2
    h2h_param = f"{int(team1_id)}-{int(team2_id)}"
    api_params: dict[str, Any] = {"h2h": h2h_param}

    if league_id is not None:
        api_params["league"] = int(league_id)
    if season is not None:
        api_params["season"] = int(season)

    try:
        # Acquire rate limiter token
        limiter.acquire_token()
        
        # Call API
        result = await client.get("/fixtures/headtohead", params=api_params)
        limiter.update_from_headers(result.headers)

        if result.status_code != 200 or not result.data:
            raise HTTPException(
                status_code=500,
                detail=f"API request failed: status={result.status_code}",
            )

        envelope = result.data
        errors = envelope.get("errors") or {}
        if errors:
            raise HTTPException(
                status_code=400,
                detail=f"API returned errors: {errors}",
            )

        # Store RAW response (run in thread pool to avoid blocking)
        await asyncio.to_thread(
            upsert_raw,
            endpoint="/fixtures/headtohead",
            requested_params=api_params,
            status_code=result.status_code,
            response_headers=result.headers,
            body=envelope,
        )

        # FK guard: ensure leagues/teams/venues exist before upserting fixtures.
        # Group by (league_id, season) because ensure_fixtures_dependencies is league+season scoped.
        try:
            grouped: dict[tuple[int, int], list[dict[str, Any]]] = {}
            for it in envelope.get("response") or []:
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
                    fixtures_envelope={**envelope, "response": items},
                    client=client,
                    limiter=limiter,
                    log_venues=False,
                )
        except Exception as e:
            # Log but don't fail - some leagues might not be fetchable
            import traceback
            error_trace = traceback.format_exc()
            print(f"Warning: Failed to ensure dependencies for h2h fixtures: {error_trace}", flush=True)

        # Transform and store in CORE
        fixtures_rows, details_rows = transform_fixtures(envelope)

        if fixtures_rows:
            # Run DB operations in thread pool to avoid blocking async event loop
            def _upsert_fixtures():
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
                    if details_rows:
                        upsert_core(
                            full_table_name="core.fixture_details",
                            rows=details_rows,
                            conflict_cols=["fixture_id"],
                            update_cols=["events", "lineups", "statistics", "players"],
                            conn=conn,
                        )
            
            await asyncio.to_thread(_upsert_fixtures)

        # Now fetch from DB with pagination
        filters_db: list[str] = []
        params_db: list[Any] = [int(team1_id), int(team2_id), int(team2_id), int(team1_id)]

        if league_id is not None:
            filters_db.append("AND f.league_id = %s")
            params_db.append(int(league_id))

        if season is not None:
            filters_db.append("AND f.season = %s")
            params_db.append(int(season))

        sql_text_db = f"""
        SELECT
          f.id,
          f.league_id,
          f.season,
          f.date,
          f.status_short,
          f.home_team_id,
          th.name as home_team_name,
          f.away_team_id,
          ta.name as away_team_name,
          f.goals_home,
          f.goals_away,
          f.updated_at
        FROM core.fixtures f
        JOIN core.teams th ON f.home_team_id = th.id
        JOIN core.teams ta ON f.away_team_id = ta.id
        WHERE (
          (f.home_team_id = %s AND f.away_team_id = %s)
          OR
          (f.home_team_id = %s AND f.away_team_id = %s)
        )
        {' '.join(filters_db)}
        ORDER BY f.date DESC
        LIMIT %s OFFSET %s
        """
        params_db.append(safe_limit)
        params_db.append(safe_offset)
        rows_db = await _fetchall_async(sql_text_db, tuple(params_db))

        items: list[dict[str, Any]] = []
        played = wins = draws = losses = 0
        gf = ga = 0
        for r in rows_db:
            fid = int(r[0])
            hid = _to_int_or_none(r[5])
            aid = _to_int_or_none(r[7])
            gh = _to_int_or_none(r[9])
            ga_ = _to_int_or_none(r[10])
            items.append(
                {
                    "id": fid,
                    "league_id": int(r[1]),
                    "season": _to_int_or_none(r[2]),
                    "date_utc": _to_iso_or_none(r[3]),
                    "status": r[4],
                    "home_team_id": hid,
                    "home_team": r[6],
                    "away_team_id": aid,
                    "away_team": r[8],
                    "goals_home": gh,
                    "goals_away": ga_,
                    "updated_at_utc": _to_iso_or_none(r[11]),
                }
            )

            if hid is None or aid is None or gh is None or ga_ is None:
                continue
            played += 1
            team1_is_home = int(hid) == int(team1_id)
            team1_goals = int(gh) if team1_is_home else int(ga_)
            team2_goals = int(ga_) if team1_is_home else int(gh)
            gf += team1_goals
            ga += team2_goals
            if team1_goals > team2_goals:
                wins += 1
            elif team1_goals < team2_goals:
                losses += 1
            else:
                draws += 1

        return {
            "ok": True,
            "teams": {"team1_id": int(team1_id), "team2_id": int(team2_id)},
            "filters": {"league_id": int(league_id) if league_id is not None else None, "season": int(season) if season is not None else None},
            "summary_team1": {
                "played": played,
                "wins": wins,
                "draws": draws,
                "losses": losses,
                "goals_for": gf,
                "goals_against": ga,
                "goals_for_avg": (round(gf / played, 4) if played else None),
                "goals_against_avg": (round(ga / played, 4) if played else None),
            },
            "items": items,
            "source": "api",
        }

    except HTTPException:
        raise
    except Exception as e:
        import traceback
        error_trace = traceback.format_exc()
        # Log full traceback for debugging
        print(f"Error in read_h2h: {error_trace}", flush=True)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to fetch h2h data: {str(e)}",
        )


@app.get(
    "/v1/sse/system-status",
    dependencies=[Depends(require_access), Depends(require_only_query_params({"interval_seconds"}))],
)
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


@app.get(
    "/v1/sse/live-scores",
    dependencies=[Depends(require_access), Depends(require_only_query_params({"interval_seconds", "limit"}))],
)
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


@app.get(
    "/ops",
    dependencies=[Depends(require_access), Depends(require_only_query_params(set()))],
)
async def ops_dashboard() -> Response:
    return Response(content=OPS_DASHBOARD_HTML, media_type="text/html")


@app.get(
    "/ops/api/system_status",
    dependencies=[Depends(require_access), Depends(require_only_query_params(set()))],
)
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
    standings_progress = await mcp_server.get_standings_refresh_progress(job_id="daily_standings")
    raw_errors = await mcp_server.get_raw_error_summary(since_minutes=60)
    raw_error_samples = await mcp_server.get_raw_error_samples(
        since_minutes=60,
        endpoint="/fixtures",
        limit=10,
    )
    recent_log_errors = await mcp_server.get_recent_log_errors(limit=50)

    # Compact job view for /ops consumers (keeps full job_status untouched).
    jobs_compact: list[dict[str, Any]] = []
    try:
        for j in (job_status or {}).get("jobs") or []:
            if not isinstance(j, dict):
                continue
            jobs_compact.append(
                {
                    "job_id": j.get("job_id") or j.get("job_name"),
                    "enabled": j.get("enabled"),
                    "type": j.get("type"),
                    "endpoint": j.get("endpoint"),
                    "interval": j.get("interval"),
                    "status": j.get("status"),
                    "last_seen_at_utc": j.get("last_seen_at_utc"),
                    "last_seen_source": j.get("last_seen_source"),
                    "last_event": j.get("last_event"),
                    "last_event_ts_utc": j.get("last_event_ts_utc"),
                    "last_raw_fetched_at_utc": j.get("last_raw_fetched_at_utc"),
                }
            )
    except Exception:
        jobs_compact = []

    return {
        "ok": True,
        "quota": quota,
        "db": db,
        "coverage_summary": coverage_summary,
        "job_status": job_status,
        "job_status_compact": jobs_compact,
        "standings_progress": standings_progress,
        "backfill": backfill,
        "raw_errors": raw_errors,
        "raw_error_samples": raw_error_samples,
        "recent_log_errors": recent_log_errors,
    }


@app.get(
    "/ops/api/scope_policy",
    dependencies=[Depends(require_access), Depends(require_only_query_params({"league_id", "season"}))],
)
async def ops_scope_policy(league_id: int, season: int | None = None) -> dict:
    """
    Explain why certain endpoints are missing for a given league.

    This is a thin wrapper over MCP's `get_scope_policy()` so ops users can answer:
    - \"Why is there no standings for this competition?\"
    - \"Is it out-of-scope by policy or missing due to a pipeline issue?\"
    """
    from src.mcp import server as mcp_server

    return await mcp_server.get_scope_policy(league_id=int(league_id), season=(int(season) if season is not None else None))
