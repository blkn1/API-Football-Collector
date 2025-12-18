from __future__ import annotations

import asyncio
import importlib
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import date as date_type
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from src.utils.logging import get_logger

# IMPORTANT: Our project uses directory name `src/mcp`, which collides with the external
# library name `mcp` if `src/` is placed on sys.path (tests do this).
# To reliably import the external library, temporarily remove local src paths.
def _import_external_mcp() -> type:
    script_dir = Path(__file__).resolve().parent
    src_dir = Path(__file__).resolve().parents[1]  # .../src
    saved = list(sys.path)
    try:
        sys.path = [p for p in sys.path if p not in (str(script_dir), str(src_dir))]
        mcp_server_mod = importlib.import_module("mcp.server")
        # mcp>=1.x exposes FastMCP (recommended high-level API) which provides @app.tool().
        return mcp_server_mod.FastMCP
    finally:
        sys.path = saved


FastMCP = _import_external_mcp()

# Ensure imports work when launched as a script:
#   python /abs/path/to/src/mcp/server.py
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.mcp import queries  # noqa: E402
from src.utils.db import get_db_connection  # noqa: E402

logger = get_logger(component="mcp_server")


def _create_mcp_app() -> Any:
    """
    Create FastMCP app.

    IMPORTANT (Coolify / reverse proxy):
    Uvicorn must bind to 0.0.0.0 inside the container for external routing.
    Some FastMCP versions configure host/port at instantiation time (not in run()).
    """
    host = str(os.getenv("FASTMCP_HOST", "127.0.0.1")).strip()
    port = int(os.getenv("FASTMCP_PORT", "8000"))
    log_level = str(os.getenv("FASTMCP_LOG_LEVEL", "INFO")).strip()

    # Try the most specific signature first, then fall back.
    try:
        return FastMCP("api-football-mcp", host=host, port=port, log_level=log_level)
    except TypeError:
        logger.debug("fastmcp_ctor_signature_mismatch", variant="host+port+log_level")
    try:
        return FastMCP("api-football-mcp", host=host, port=port)
    except TypeError:
        logger.debug("fastmcp_ctor_signature_mismatch", variant="host+port")
    return FastMCP("api-football-mcp")


app = _create_mcp_app()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ok_error(message: str, *, details: Any | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"ok": False, "error": message, "ts_utc": _utc_now_iso()}
    if details is not None:
        payload["details"] = details
    return payload


def _parse_iso_date_utc(d: str) -> date_type:
    # Accept YYYY-MM-DD only.
    return datetime.strptime(d, "%Y-%m-%d").date()


def _to_int_or_none(x: Any) -> int | None:
    try:
        return int(x) if x is not None else None
    except Exception:
        return None


def _to_float_or_none(x: Any) -> float | None:
    try:
        return float(x) if x is not None else None
    except Exception:
        return None


def _to_iso_or_none(dt: Any) -> str | None:
    if dt is None:
        return None
    try:
        if isinstance(dt, datetime):
            # Preserve tz if present; otherwise assume UTC.
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.isoformat()
    except Exception:
        return None
    return None


def _db_fetchall(sql_text: str, params: tuple[Any, ...]) -> list[tuple[Any, ...]]:
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql_text, params)
            rows = cur.fetchall()
        conn.commit()
    return rows


def _db_fetchone(sql_text: str, params: tuple[Any, ...]) -> tuple[Any, ...] | None:
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql_text, params)
            row = cur.fetchone()
        conn.commit()
    return row


async def _db_fetchall_async(sql_text: str, params: tuple[Any, ...]) -> list[tuple[Any, ...]]:
    return await asyncio.to_thread(_db_fetchall, sql_text, params)


async def _db_fetchone_async(sql_text: str, params: tuple[Any, ...]) -> tuple[Any, ...] | None:
    return await asyncio.to_thread(_db_fetchone, sql_text, params)


@dataclass(frozen=True)
class JobConfig:
    job_id: str
    enabled: bool
    endpoint: str | None
    interval: dict[str, Any] | None
    type: str | None


def _load_jobs_from_yaml(path: Path) -> list[JobConfig]:
    try:
        cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as e:
        logger.warning("mcp_failed_to_read_jobs_config", path=str(path), err=str(e))
        return []
    jobs = cfg.get("jobs") or []
    out: list[JobConfig] = []
    for j in jobs:
        if not isinstance(j, dict):
            continue
        out.append(
            JobConfig(
                job_id=str(j.get("job_id") or ""),
                enabled=bool(j.get("enabled", False)),
                endpoint=(j.get("endpoint") or None),
                interval=(j.get("interval") or None),
                type=(j.get("type") or None),
            )
        )
    return [x for x in out if x.job_id]


def _tracked_leagues_from_daily_config() -> list[dict[str, Any]]:
    # Prefer Phase 3 shape (config/jobs/daily.yaml has tracked_leagues + season).
    cfg_path = Path(os.getenv("API_FOOTBALL_DAILY_CONFIG", str(PROJECT_ROOT / "config" / "jobs" / "daily.yaml")))
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    tracked = cfg.get("tracked_leagues") or []
    leagues: list[dict[str, Any]] = []
    if isinstance(tracked, list):
        for x in tracked:
            if not isinstance(x, dict) or "id" not in x:
                continue
            leagues.append({"id": int(x["id"]), "name": x.get("name")})
    return leagues


def _default_season_from_daily_config() -> int | None:
    """
    Default season comes from config/jobs/daily.yaml (or API_FOOTBALL_DAILY_CONFIG override).
    This keeps MCP tools config-driven while avoiding assumptions.
    """
    cfg_path = Path(os.getenv("API_FOOTBALL_DAILY_CONFIG", str(PROJECT_ROOT / "config" / "jobs" / "daily.yaml")))
    try:
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return None
    season_cfg = cfg.get("season")
    try:
        return int(season_cfg) if season_cfg is not None else None
    except Exception:
        return None


def _parse_job_logs(job_name: str | None = None) -> dict[str, Any]:
    """
    Parse structlog JSONL produced by scripts (best-effort).
    We surface "last run" and "status" from the most recent relevant events.
    """
    log_path = Path(os.getenv("COLLECTOR_LOG_FILE", str(PROJECT_ROOT / "logs" / "collector.jsonl")))
    if not log_path.exists():
        return {"log_file": str(log_path), "jobs": []}

    # Best effort: read last N lines to avoid huge file reads.
    max_lines = int(os.getenv("MCP_LOG_TAIL_LINES", "2000"))
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()[-max_lines:]
    except Exception as e:
        return {"log_file": str(log_path), "jobs": [], "error": f"failed_to_read_logs: {e}"}

    # We want "most recent event" per job_id.
    # The log file can contain a mix of:
    # - structlog JSON lines (preferred)
    # - plain-text APScheduler lines (Coolify/docker logs sometimes mirror into the same file)
    #
    # Strategy:
    # - iterate from newest -> oldest (reverse)
    # - record the FIRST event we can attribute to each job_id
    last_by_script: dict[str, dict[str, Any]] = {}

    # Plain text patterns (best-effort)
    # Example:
    #   2025-12-18 04:19:51 [info     ] job_scheduled component=collector_scheduler job_id=daily_fixtures_by_date ...
    _re_plain_job_id = re.compile(r"\bjob_id=(?P<job_id>[A-Za-z0-9_\-]+)\b")
    _re_plain_event = re.compile(r"\]\s+(?P<event>[A-Za-z0-9_]+)\b")
    _re_plain_ts = re.compile(r"^(?P<ts>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})")
    # APScheduler:
    #   Job "fixtures_backfill_league_season (trigger: ...)" executed successfully
    _re_aps_job = re.compile(r'^Job "(?P<job_id>[A-Za-z0-9_\-]+)\s+\(trigger:')
    _re_aps_running = re.compile(r'^Running job "(?P<job_id>[A-Za-z0-9_\-]+)\s+\(trigger:')

    for ln in reversed(lines):
        ln = ln.strip()
        if not ln:
            continue

        # JSON path
        if ln.startswith("{"):
            try:
                obj = json.loads(ln)
            except Exception:
                continue

            jid = obj.get("job_id")
            if jid is not None and str(jid).strip():
                script = str(jid)
            else:
                script = str(obj.get("script") or obj.get("component") or "unknown")

            # Alias common script-style names to scheduler job_id's (so get_job_status() can merge correctly).
            # scripts/daily_sync.py -> scheduler job_id=daily_fixtures_by_date
            # scripts/standings_sync.py -> scheduler job_id=daily_standings
            if script == "daily_sync":
                script = "daily_fixtures_by_date"
            elif script == "standings_sync":
                script = "daily_standings"
            if job_name and script != job_name:
                continue
            if script in last_by_script:
                continue

            last_by_script[script] = {
                "job_name": script,
                "timestamp": obj.get("timestamp"),
                "event": obj.get("event"),
                "level": obj.get("level"),
                "raw": obj,
            }
            continue

        # Plain-text fallback
        m_job = _re_plain_job_id.search(ln) or _re_aps_job.search(ln) or _re_aps_running.search(ln)
        if not m_job:
            continue
        script = str(m_job.group("job_id"))
        if script == "daily_sync":
            script = "daily_fixtures_by_date"
        elif script == "standings_sync":
            script = "daily_standings"
        if job_name and script != job_name:
            continue
        if script in last_by_script:
            continue

        m_ev = _re_plain_event.search(ln)
        event = m_ev.group("event") if m_ev else None
        m_ts = _re_plain_ts.search(ln)
        ts = m_ts.group("ts") if m_ts else None

        last_by_script[script] = {
            "job_name": script,
            "timestamp": ts,
            "event": event,
            "level": None,
            "raw": {"line": ln},
        }

    jobs: list[dict[str, Any]] = []
    for script, info in sorted(last_by_script.items(), key=lambda kv: (kv[0])):
        ev = str(info.get("event") or "")
        status: str
        if ev.endswith("_complete") or ev.endswith("_completed"):
            status = "success"
        elif ev.endswith("_failed") or (info.get("level") == "error"):
            status = "error"
        elif ev.endswith("_started"):
            status = "running_or_started"
        else:
            status = "unknown"

        jobs.append(
            {
                "job_name": script,
                "status": status,
                "last_event": ev or None,
                "last_event_ts_utc": info.get("timestamp"),
            }
        )

    return {"log_file": str(log_path), "jobs": jobs}


def _load_league_overrides() -> list[dict[str, Any]]:
    """
    Read config/league_overrides.yaml and return normalized overrides list.
    Supports both list and mapping forms.
    """
    path = Path(os.getenv("API_FOOTBALL_LEAGUE_OVERRIDES_CONFIG", str(PROJECT_ROOT / "config" / "league_overrides.yaml")))
    try:
        cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return []
    raw = cfg.get("overrides") or []

    out: list[dict[str, Any]] = []
    if isinstance(raw, list):
        for x in raw:
            if not isinstance(x, dict):
                continue
            if "league_id" not in x or "source" not in x:
                continue
            try:
                league_id = int(x["league_id"])
            except Exception:
                continue
            season = x.get("season")
            try:
                season_i = int(season) if season is not None else None
            except Exception:
                season_i = None
            out.append({"source": str(x.get("source") or ""), "league_id": league_id, "season": season_i})

    elif isinstance(raw, dict):
        for source, league_id in raw.items():
            try:
                out.append({"source": str(source), "league_id": int(league_id), "season": None})
            except Exception:
                continue

    # Drop empty sources
    return [x for x in out if x.get("source")]

@app.tool()
async def get_coverage_status(league_id: int | None = None, season: int | None = None) -> dict:
    """
    Get coverage metrics (mart.coverage_status) for all leagues or a specific league.

    Args:
        league_id: League ID (None = all)
        season: Season year (if omitted, uses config/jobs/daily.yaml season). If missing in config, request is rejected.
    """
    try:
        # Default season from config/jobs/daily.yaml when available (keeps config-driven behavior).
        if season is None:
            season_cfg = _default_season_from_daily_config()
            if season_cfg is None:
                return _ok_error(
                    "season_required",
                    details="Missing season in daily config. Pass season explicitly or set top-level 'season:' in config/jobs/daily.yaml",
                )
            season = int(season_cfg)

        sql_text = queries.COVERAGE_STATUS
        if league_id is not None:
            sql_text = sql_text.format(league_filter="AND c.league_id = %s")
            rows = await _db_fetchall_async(sql_text, (int(season), int(league_id)))
        else:
            sql_text = sql_text.format(league_filter="")
            rows = await _db_fetchall_async(sql_text, (int(season),))

        out: list[dict[str, Any]] = []
        for r in rows:
            out.append(
                {
                    "league": r[0],
                    "league_id": int(r[1]),
                    "season": int(r[2]),
                    "endpoint": r[3],
                    "count_coverage": _to_float_or_none(r[4]),
                    "freshness_coverage": _to_float_or_none(r[5]),
                    "pipeline_coverage": _to_float_or_none(r[6]),
                    "overall_coverage": _to_float_or_none(r[7]),
                    "last_update_utc": _to_iso_or_none(r[8]),
                    "lag_minutes": _to_int_or_none(r[9]),
                    "calculated_at_utc": _to_iso_or_none(r[10]),
                }
            )

        return {"ok": True, "season": int(season), "coverage": out, "ts_utc": _utc_now_iso()}
    except Exception as e:
        return _ok_error("get_coverage_status_failed", details=str(e))


@app.tool()
async def get_coverage_summary(season: int | None = None) -> dict:
    """
    Quick overview of coverage for a season.

    Args:
        season: Season year (defaults like get_coverage_status)
    """
    try:
        if season is None:
            season_cfg = _default_season_from_daily_config()
            if season_cfg is None:
                return _ok_error(
                    "season_required",
                    details="Missing season in daily config. Pass season explicitly or set top-level 'season:' in config/jobs/daily.yaml",
                )
            season = int(season_cfg)

        row = await _db_fetchone_async(queries.COVERAGE_SUMMARY, (int(season),))
        if not row:
            return {"ok": True, "season": int(season), "summary": None, "ts_utc": _utc_now_iso()}
        return {
            "ok": True,
            "season": int(row[0]),
            "summary": {
                "rows": int(row[1]),
                "leagues": int(row[2]),
                "endpoints": int(row[3]),
                "avg_overall_coverage": _to_float_or_none(row[4]),
                "last_calculated_at_utc": _to_iso_or_none(row[5]),
            },
            "ts_utc": _utc_now_iso(),
        }
    except Exception as e:
        return _ok_error("get_coverage_summary_failed", details=str(e))


@app.tool()
async def get_rate_limit_status() -> dict:
    """
    Get current API quota status (best-effort).

    Source:
    - raw.api_responses.response_headers from the latest API call that included quota headers.
    """
    try:
        row = await _db_fetchone_async(queries.LAST_QUOTA_HEADERS_QUERY, ())
        if not row:
            return {
                "ok": True,
                "source": "raw.api_responses (no rows with quota headers yet)",
                "daily_remaining": None,
                "minute_remaining": None,
                "observed_at_utc": None,
                "ts_utc": _utc_now_iso(),
            }

        observed_at, daily_raw, minute_raw = row
        return {
            "ok": True,
            "source": "raw.api_responses.response_headers",
            "daily_remaining": _to_int_or_none(daily_raw),
            "minute_remaining": _to_int_or_none(minute_raw),
            "observed_at_utc": _to_iso_or_none(observed_at),
            "ts_utc": _utc_now_iso(),
        }
    except Exception as e:
        return _ok_error("get_rate_limit_status_failed", details=str(e))


@app.tool()
async def get_live_loop_status(since_minutes: int = 5) -> dict:
    """
    Report whether live loop is actively polling /fixtures?live=all by inspecting RAW.

    Args:
        since_minutes: Lookback window (default 5, capped to 1440)
    """
    try:
        mins = max(1, min(int(since_minutes), 60 * 24))
        row = await _db_fetchone_async(queries.LIVE_LOOP_ACTIVITY_QUERY, (mins,))
        if not row:
            return {"ok": True, "window": {"since_minutes": mins}, "running": False, "requests": 0, "last_fetched_at_utc": None, "ts_utc": _utc_now_iso()}
        reqs, last_dt = row
        requests = _to_int_or_none(reqs) or 0
        return {
            "ok": True,
            "window": {"since_minutes": mins},
            "running": bool(requests > 0),
            "requests": int(requests),
            "last_fetched_at_utc": _to_iso_or_none(last_dt),
            "ts_utc": _utc_now_iso(),
        }
    except Exception as e:
        return _ok_error("get_live_loop_status_failed", details=str(e))


@app.tool()
async def get_daily_fixtures_by_date_status(since_minutes: int = 180) -> dict:
    """
    Report whether the scheduler's daily_fixtures_by_date job is actually calling /fixtures?date=YYYY-MM-DD.

    This avoids relying on log parsing; it inspects RAW request history directly.

    Notes:
      - Works for both modes:
        - per-tracked-leagues: requested_params includes {league, season, date}
        - global_by_date paging: requested_params includes {date, page}

    Args:
        since_minutes: Lookback window (default 180, capped to 1440)
    """
    try:
        mins = max(1, min(int(since_minutes), 60 * 24))
        row = await _db_fetchone_async(queries.DAILY_FIXTURES_BY_DATE_ACTIVITY_QUERY, (mins,))
        if not row:
            return {"ok": True, "window": {"since_minutes": mins}, "running": False, "requests": 0, "last_fetched_at_utc": None, "ts_utc": _utc_now_iso()}
        reqs, last_dt = row
        requests = _to_int_or_none(reqs) or 0
        return {
            "ok": True,
            "window": {"since_minutes": mins},
            "running": bool(requests > 0),
            "requests": int(requests),
            "last_fetched_at_utc": _to_iso_or_none(last_dt),
            "ts_utc": _utc_now_iso(),
        }
    except Exception as e:
        return _ok_error("get_daily_fixtures_by_date_status_failed", details=str(e))


@app.tool()
async def get_last_sync_time(endpoint: str) -> dict:
    """
    When was the last successful RAW fetch for an endpoint?

    Args:
        endpoint: API endpoint path, e.g. "/fixtures", "/standings"
    """
    try:
        if not endpoint.startswith("/"):
            endpoint = f"/{endpoint}"
        row = await _db_fetchone_async(queries.LAST_SYNC_TIME_QUERY, (endpoint,))
        last_dt = row[0] if row else None
        return {"ok": True, "endpoint": endpoint, "last_fetched_at_utc": _to_iso_or_none(last_dt), "ts_utc": _utc_now_iso()}
    except Exception as e:
        return _ok_error("get_last_sync_time_failed", details=str(e))


@app.tool()
async def query_fixtures(
    league_id: int | None = None,
    date: str | None = None,
    status: str | None = None,
    limit: int = 10,
) -> list:
    """
    Query fixtures (core.fixtures) with optional filters.

    Args:
        league_id: Optional league ID
        date: Optional UTC date (YYYY-MM-DD)
        status: Optional status_short (e.g. "NS", "FT", "1H")
        limit: Max rows (default 10, capped at 100)
    """
    try:
        safe_limit = max(1, min(int(limit), 100))
        filters: list[str] = []
        params: list[Any] = []

        if league_id is not None:
            filters.append("AND f.league_id = %s")
            params.append(int(league_id))
        if status is not None:
            filters.append("AND f.status_short = %s")
            params.append(str(status))
        if date is not None:
            d = _parse_iso_date_utc(str(date))
            filters.append("AND DATE(f.date AT TIME ZONE 'UTC') = %s")
            params.append(d.isoformat())

        sql_text = queries.FIXTURES_QUERY.format(filters="\n    ".join(filters))
        params.append(safe_limit)

        rows = await _db_fetchall_async(sql_text, tuple(params))
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
    except Exception as e:
        return [_ok_error("query_fixtures_failed", details=str(e))]


@app.tool()
async def query_standings(league_id: int, season: int) -> list:
    """
    Query standings (core.standings) for a league+season.
    """
    try:
        rows = await _db_fetchall_async(queries.STANDINGS_QUERY, (int(league_id), int(season)))
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
    except Exception as e:
        return [_ok_error("query_standings_failed", details=str(e))]


@app.tool()
async def query_teams(league_id: int | None = None, search: str | None = None, limit: int = 20) -> list:
    """
    Query teams (core.teams).

    Notes:
    - Teams are not season-scoped in CORE, so league_id filtering is best-effort via fixtures participation.
    """
    try:
        safe_limit = max(1, min(int(limit), 100))
        filters: list[str] = []
        params: list[Any] = []

        if search:
            filters.append("AND t.name ILIKE %s")
            params.append(f"%{search}%")

        # Best-effort league filter: teams that appear in fixtures for that league.
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

        sql_text = queries.TEAMS_QUERY.format(filters="\n    ".join(filters))
        params.append(safe_limit)

        rows = await _db_fetchall_async(sql_text, tuple(params))
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
    except Exception as e:
        return [_ok_error("query_teams_failed", details=str(e))]


@app.tool()
async def get_league_info(league_id: int) -> dict:
    """
    Get league info (core.leagues).
    """
    try:
        row = await _db_fetchone_async(queries.LEAGUE_INFO_QUERY, (int(league_id),))
        if not row:
            return {"ok": True, "league": None, "ts_utc": _utc_now_iso()}
        return {
            "ok": True,
            "league": {
                "id": int(row[0]),
                "name": row[1],
                "type": row[2],
                "logo": row[3],
                "country_name": row[4],
                "country_code": row[5],
                "country_flag": row[6],
                "updated_at_utc": _to_iso_or_none(row[7]),
            },
            "ts_utc": _utc_now_iso(),
        }
    except Exception as e:
        return _ok_error("get_league_info_failed", details=str(e))


@app.tool()
async def get_database_stats() -> dict:
    """
    Get DB record counts across RAW/CORE plus last activity timestamps.
    """
    try:
        row = await _db_fetchone_async(queries.DATABASE_STATS_QUERY, ())
        if not row:
            return {"ok": True, "stats": None, "ts_utc": _utc_now_iso()}
        return {
            "ok": True,
            "stats": {
                "raw_api_responses": int(row[0]),
                "core_leagues": int(row[1]),
                "core_teams": int(row[2]),
                "core_venues": int(row[3]),
                "core_fixtures": int(row[4]),
                "core_fixture_details": int(row[5]),
                "core_injuries": int(row[6]),
                "core_fixture_players": int(row[7]),
                "core_fixture_events": int(row[8]),
                "core_fixture_statistics": int(row[9]),
                "core_fixture_lineups": int(row[10]),
                "core_standings": int(row[11]),
                "raw_last_fetched_at_utc": _to_iso_or_none(row[12]),
                "core_fixtures_last_updated_at_utc": _to_iso_or_none(row[13]),
            },
            "ts_utc": _utc_now_iso(),
        }
    except Exception as e:
        return _ok_error("get_database_stats_failed", details=str(e))


@app.tool()
async def query_injuries(
    league_id: int | None = None,
    season: int | None = None,
    team_id: int | None = None,
    player_id: int | None = None,
    limit: int = 50,
) -> list:
    """
    Query injuries (core.injuries) with optional filters.
    """
    try:
        safe_limit = max(1, min(int(limit), 200))
        filters: list[str] = []
        params: list[Any] = []

        if league_id is not None:
            filters.append("AND i.league_id = %s")
            params.append(int(league_id))

        if season is None:
            season_cfg = _default_season_from_daily_config()
            if season_cfg is not None and league_id is not None:
                season = int(season_cfg)
        if season is not None:
            filters.append("AND i.season = %s")
            params.append(int(season))

        if team_id is not None:
            filters.append("AND i.team_id = %s")
            params.append(int(team_id))
        if player_id is not None:
            filters.append("AND i.player_id = %s")
            params.append(int(player_id))

        sql_text = queries.INJURIES_QUERY.format(filters="\n    ".join(filters))
        params.append(safe_limit)
        rows = await _db_fetchall_async(sql_text, tuple(params))

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
                    "date": (str(r[9]) if r[9] is not None else None),
                    "updated_at_utc": _to_iso_or_none(r[10]),
                }
            )
        return out
    except Exception as e:
        return [_ok_error("query_injuries_failed", details=str(e))]


@app.tool()
async def get_fixture_detail_status(fixture_id: int) -> dict:
    """
    For a fixture_id, report whether CORE has players/events/statistics/lineups,
    and the last RAW fetch time for each corresponding endpoint.
    """
    try:
        row = await _db_fetchone_async(queries.FIXTURE_DETAIL_STATUS_QUERY, (int(fixture_id),))
        if not row:
            return {"ok": True, "fixture": None, "ts_utc": _utc_now_iso()}

        return {
            "ok": True,
            "fixture": {
                "fixture_id": int(row[0]),
                "league_id": int(row[1]),
                "season": _to_int_or_none(row[2]),
                "date_utc": _to_iso_or_none(row[3]),
                "status_short": row[4],
                "has_players": bool(row[5]),
                "has_events": bool(row[6]),
                "has_statistics": bool(row[7]),
                "has_lineups": bool(row[8]),
                "last_players_fetch_utc": _to_iso_or_none(row[9]),
                "last_events_fetch_utc": _to_iso_or_none(row[10]),
                "last_statistics_fetch_utc": _to_iso_or_none(row[11]),
                "last_lineups_fetch_utc": _to_iso_or_none(row[12]),
            },
            "ts_utc": _utc_now_iso(),
        }
    except Exception as e:
        return _ok_error("get_fixture_detail_status_failed", details=str(e))


@app.tool()
async def query_fixture_players(fixture_id: int, team_id: int | None = None, limit: int = 300) -> list:
    """
    Query core.fixture_players for a fixture (optionally filter by team_id).
    """
    try:
        safe_limit = max(1, min(int(limit), 500))
        if team_id is not None:
            sql_text = queries.FIXTURE_PLAYERS_QUERY.format(team_filter="AND team_id = %s")
            rows = await _db_fetchall_async(sql_text, (int(fixture_id), int(team_id), safe_limit))
        else:
            sql_text = queries.FIXTURE_PLAYERS_QUERY.format(team_filter="")
            rows = await _db_fetchall_async(sql_text, (int(fixture_id), safe_limit))

        out: list[dict[str, Any]] = []
        for r in rows:
            out.append(
                {
                    "fixture_id": int(r[0]),
                    "team_id": _to_int_or_none(r[1]),
                    "player_id": _to_int_or_none(r[2]),
                    "player_name": r[3],
                    "statistics": r[4],
                    "updated_at_utc": _to_iso_or_none(r[5]),
                }
            )
        return out
    except Exception as e:
        return [_ok_error("query_fixture_players_failed", details=str(e))]


@app.tool()
async def query_fixture_events(fixture_id: int, limit: int = 300) -> list:
    """
    Query core.fixture_events for a fixture.
    """
    try:
        safe_limit = max(1, min(int(limit), 1000))
        rows = await _db_fetchall_async(queries.FIXTURE_EVENTS_QUERY, (int(fixture_id), safe_limit))
        out: list[dict[str, Any]] = []
        for r in rows:
            out.append(
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
        return out
    except Exception as e:
        return [_ok_error("query_fixture_events_failed", details=str(e))]


@app.tool()
async def query_fixture_statistics(fixture_id: int) -> list:
    """
    Query core.fixture_statistics for a fixture (one row per team).
    """
    try:
        rows = await _db_fetchall_async(queries.FIXTURE_STATISTICS_QUERY, (int(fixture_id),))
        out: list[dict[str, Any]] = []
        for r in rows:
            out.append(
                {
                    "fixture_id": int(r[0]),
                    "team_id": _to_int_or_none(r[1]),
                    "statistics": r[2],
                    "updated_at_utc": _to_iso_or_none(r[3]),
                }
            )
        return out
    except Exception as e:
        return [_ok_error("query_fixture_statistics_failed", details=str(e))]


@app.tool()
async def query_fixture_lineups(fixture_id: int) -> list:
    """
    Query core.fixture_lineups for a fixture (one row per team).
    """
    try:
        rows = await _db_fetchall_async(queries.FIXTURE_LINEUPS_QUERY, (int(fixture_id),))
        out: list[dict[str, Any]] = []
        for r in rows:
            out.append(
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
        return out
    except Exception as e:
        return [_ok_error("query_fixture_lineups_failed", details=str(e))]


@app.tool()
async def list_tracked_leagues() -> dict:
    """
    List tracked leagues from config (config/jobs/daily.yaml -> tracked_leagues).
    """
    try:
        leagues = _tracked_leagues_from_daily_config()
        overrides = _load_league_overrides()

        # Union view for convenience (unique by league_id)
        union: dict[int, dict[str, Any]] = {}
        for x in leagues:
            try:
                union[int(x["id"])] = {"league_id": int(x["id"]), "name": x.get("name"), "source": "daily_config", "season": None}
            except Exception:
                continue
        for o in overrides:
            lid = int(o["league_id"])
            # keep existing name if present, but annotate override source
            cur = union.get(lid) or {"league_id": lid, "name": None, "source": "overrides", "season": o.get("season")}
            # If already present from daily_config, keep source list as a string for now.
            if cur.get("source") != "daily_config":
                cur["source"] = "overrides"
            cur["season"] = cur.get("season") or o.get("season")
            union[lid] = cur

        return {
            "ok": True,
            "tracked_leagues": leagues,
            "league_overrides": overrides,
            "configured_leagues_union": sorted(union.values(), key=lambda d: int(d["league_id"])),
            "ts_utc": _utc_now_iso(),
        }
    except Exception as e:
        return _ok_error("list_tracked_leagues_failed", details=str(e))


@app.tool()
async def get_job_status(job_name: str | None = None) -> dict:
    """
    Get job status (best-effort).

    Sources:
    - YAML configs: config/jobs/*.yaml (enabled, interval, endpoint)
    - Structured logs: logs/collector.jsonl (last event per script/job)

    Args:
        job_name: Optional job/script name to filter (e.g. "daily_sync", "live_loop")
    """
    try:
        cfg_files = [
            PROJECT_ROOT / "config" / "jobs" / "static.yaml",
            PROJECT_ROOT / "config" / "jobs" / "daily.yaml",
            PROJECT_ROOT / "config" / "jobs" / "live.yaml",
        ]
        cfg_jobs: list[dict[str, Any]] = []
        for p in cfg_files:
            for j in _load_jobs_from_yaml(p):
                if job_name and j.job_id != job_name:
                    continue
                cfg_jobs.append(
                    {
                        "job_id": j.job_id,
                        "enabled": j.enabled,
                        "endpoint": j.endpoint,
                        "type": j.type,
                        "interval": j.interval,
                        "config_file": str(p),
                    }
                )

        logs = _parse_job_logs(job_name=job_name)

        # Merge by job_id/job_name when possible.
        jobs_by_id: dict[str, dict[str, Any]] = {j["job_id"]: j for j in cfg_jobs}
        merged: list[dict[str, Any]] = []
        for lj in logs.get("jobs") or []:
            jid = lj.get("job_name")
            base = jobs_by_id.get(jid, {})
            merged.append({**base, **lj})

        # Include any config jobs not present in logs.
        logged_ids = {m.get("job_id") or m.get("job_name") for m in merged}
        for j in cfg_jobs:
            if j.get("job_id") not in logged_ids:
                merged.append({**j, "status": ("disabled" if not j.get("enabled") else "unknown"), "last_event": None, "last_event_ts_utc": None})

        return {"ok": True, "jobs": merged, "log_file": logs.get("log_file"), "ts_utc": _utc_now_iso()}
    except Exception as e:
        return _ok_error("get_job_status_failed", details=str(e))


@app.tool()
async def get_backfill_progress(
    job_id: str | None = None,
    season: int | None = None,
    include_completed: bool = False,
    limit: int = 200,
) -> dict:
    """
    Report backfill progress from core.backfill_progress.

    Returns:
    - summaries: grouped by job_id (counts + last_updated_at)
    - tasks: most recently updated tasks (optionally excluding completed)
    """
    try:
        safe_limit = max(1, min(int(limit), 1000))
        job_id_str = str(job_id) if job_id is not None else None
        season_int = int(season) if season is not None else None

        summary_rows = await _db_fetchall_async(
            queries.BACKFILL_PROGRESS_SUMMARY_QUERY,
            (job_id_str, job_id_str, season_int, season_int),
        )
        task_rows = await _db_fetchall_async(
            queries.BACKFILL_PROGRESS_LIST_QUERY,
            (job_id_str, job_id_str, season_int, season_int, bool(include_completed), safe_limit),
        )

        summaries: list[dict[str, Any]] = []
        for r in summary_rows:
            summaries.append(
                {
                    "job_id": r[0],
                    "total_tasks": _to_int_or_none(r[1]),
                    "completed_tasks": _to_int_or_none(r[2]),
                    "pending_tasks": _to_int_or_none(r[3]),
                    "last_updated_at_utc": _to_iso_or_none(r[4]),
                }
            )

        tasks: list[dict[str, Any]] = []
        for r in task_rows:
            tasks.append(
                {
                    "job_id": r[0],
                    "league_id": _to_int_or_none(r[1]),
                    "season": _to_int_or_none(r[2]),
                    "next_page": _to_int_or_none(r[3]),
                    "completed": bool(r[4]),
                    "last_error": r[5],
                    "last_run_at_utc": _to_iso_or_none(r[6]),
                    "updated_at_utc": _to_iso_or_none(r[7]),
                }
            )

        return {
            "ok": True,
            "filters": {"job_id": job_id_str, "season": season_int, "include_completed": bool(include_completed), "limit": safe_limit},
            "summaries": summaries,
            "tasks": tasks,
            "ts_utc": _utc_now_iso(),
        }
    except Exception as e:
        return _ok_error("get_backfill_progress_failed", details=str(e))


@app.tool()
async def get_raw_error_summary(
    since_minutes: int = 60,
    endpoint: str | None = None,
    top_endpoints_limit: int = 25,
) -> dict:
    """
    Summarize RAW request health (status_code + envelope errors) over a recent time window.
    This is read-only and meant for operational monitoring.
    """
    try:
        mins = max(1, min(int(since_minutes), 60 * 24 * 14))  # cap at 14 days
        ep = str(endpoint) if endpoint is not None else None
        safe_top = max(1, min(int(top_endpoints_limit), 200))

        row = await _db_fetchone_async(queries.RAW_ERROR_SUMMARY_QUERY, (mins, ep, ep))
        by_ep = await _db_fetchall_async(queries.RAW_ERRORS_BY_ENDPOINT_QUERY, (mins, ep, ep, safe_top))

        summary = None
        if row:
            summary = {
                "total_requests": _to_int_or_none(row[0]),
                "ok_2xx": _to_int_or_none(row[1]),
                "err_4xx": _to_int_or_none(row[2]),
                "err_5xx": _to_int_or_none(row[3]),
                "envelope_errors": _to_int_or_none(row[4]),
                "last_fetched_at_utc": _to_iso_or_none(row[5]),
            }

        endpoints: list[dict[str, Any]] = []
        for r in by_ep:
            endpoints.append(
                {
                    "endpoint": r[0],
                    "total_requests": _to_int_or_none(r[1]),
                    "ok_2xx": _to_int_or_none(r[2]),
                    "non_2xx": _to_int_or_none(r[3]),
                    "envelope_errors": _to_int_or_none(r[4]),
                    "last_fetched_at_utc": _to_iso_or_none(r[5]),
                }
            )

        return {
            "ok": True,
            "window": {"since_minutes": mins},
            "filters": {"endpoint": ep, "top_endpoints_limit": safe_top},
            "summary": summary,
            "by_endpoint": endpoints,
            "ts_utc": _utc_now_iso(),
        }
    except Exception as e:
        return _ok_error("get_raw_error_summary_failed", details=str(e))


def _parse_recent_log_errors(*, job_name: str | None, limit: int) -> dict[str, Any]:
    log_path = Path(os.getenv("COLLECTOR_LOG_FILE", str(PROJECT_ROOT / "logs" / "collector.jsonl")))
    if not log_path.exists():
        return {"log_file": str(log_path), "errors": []}

    max_lines = int(os.getenv("MCP_LOG_TAIL_LINES", "4000"))
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()[-max_lines:]
    except Exception as e:
        return {"log_file": str(log_path), "errors": [], "error": f"failed_to_read_logs: {e}"}

    out: list[dict[str, Any]] = []
    for ln in reversed(lines):
        ln = ln.strip()
        if not ln.startswith("{"):
            continue
        try:
            obj = json.loads(ln)
        except Exception:
            continue

        script = str(obj.get("script") or obj.get("component") or obj.get("job_id") or "unknown")
        if job_name and script != job_name:
            continue

        level = str(obj.get("level") or "")
        event = str(obj.get("event") or "")
        is_error = (level == "error") or event.endswith("_failed")
        if not is_error:
            continue

        out.append(
            {
                "job_name": script,
                "timestamp": obj.get("timestamp"),
                "level": level or None,
                "event": event or None,
                "raw": obj,
            }
        )
        if len(out) >= limit:
            break

    return {"log_file": str(log_path), "errors": list(reversed(out))}


@app.tool()
async def get_recent_log_errors(job_name: str | None = None, limit: int = 50) -> dict:
    """
    Return the most recent error-level structured log events from collector.jsonl (best-effort).
    """
    try:
        safe_limit = max(1, min(int(limit), 200))
        parsed = _parse_recent_log_errors(job_name=job_name, limit=safe_limit)
        return {"ok": True, "job_name": job_name, "log_file": parsed.get("log_file"), "errors": parsed.get("errors") or [], "ts_utc": _utc_now_iso()}
    except Exception as e:
        return _ok_error("get_recent_log_errors_failed", details=str(e))


async def main() -> None:
    transport = str(os.getenv("MCP_TRANSPORT", "stdio")).strip().lower()
    if transport not in ("stdio", "sse", "streamable-http"):
        transport = "stdio"
    mount_path = os.getenv("MCP_MOUNT_PATH") or None
    app.run(transport=transport, mount_path=mount_path)


if __name__ == "__main__":
    # FastMCP.run() manages its own AnyIO event loop; do not wrap it in asyncio.run().
    transport = str(os.getenv("MCP_TRANSPORT", "stdio")).strip().lower()
    if transport not in ("stdio", "sse", "streamable-http"):
        transport = "stdio"
    mount_path = os.getenv("MCP_MOUNT_PATH") or None
    app.run(transport=transport, mount_path=mount_path)


