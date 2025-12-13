from __future__ import annotations

import asyncio
import importlib
import json
import os
import sys
from dataclasses import dataclass
from datetime import date as date_type
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

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


app = FastMCP("api-football-mcp")


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
    except Exception:
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

    last_by_script: dict[str, dict[str, Any]] = {}
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        if not ln.startswith("{"):
            # ignore non-JSON noise (e.g. httpx request logs)
            continue
        try:
            obj = json.loads(ln)
        except Exception:
            continue

        script = obj.get("script") or obj.get("component") or obj.get("job_id") or "unknown"
        script = str(script)
        if job_name and script != job_name:
            continue

        ts = obj.get("timestamp")
        event = obj.get("event")
        level = obj.get("level")

        # Track "last event" per script
        prev = last_by_script.get(script)
        if prev is None or (ts and prev.get("timestamp") and ts >= prev.get("timestamp")) or prev is None:
            last_by_script[script] = {
                "job_name": script,
                "timestamp": ts,
                "event": event,
                "level": level,
                "raw": obj,
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
            cfg_path = PROJECT_ROOT / "config" / "jobs" / "daily.yaml"
            cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
            season_cfg = cfg.get("season")
            if season_cfg is None:
                return _ok_error("season_required", details=f"Missing season in {cfg_path}. Pass season explicitly or set top-level 'season:'")
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
            cfg_path = PROJECT_ROOT / "config" / "jobs" / "daily.yaml"
            cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
            season_cfg = cfg.get("season")
            if season_cfg is None:
                return _ok_error("season_required", details=f"Missing season in {cfg_path}. Pass season explicitly or set top-level 'season:'")
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
                "core_standings": int(row[6]),
                "raw_last_fetched_at_utc": _to_iso_or_none(row[7]),
                "core_fixtures_last_updated_at_utc": _to_iso_or_none(row[8]),
            },
            "ts_utc": _utc_now_iso(),
        }
    except Exception as e:
        return _ok_error("get_database_stats_failed", details=str(e))


@app.tool()
async def list_tracked_leagues() -> dict:
    """
    List tracked leagues from config (config/jobs/daily.yaml -> tracked_leagues).
    """
    try:
        leagues = _tracked_leagues_from_daily_config()
        return {"ok": True, "tracked_leagues": leagues, "ts_utc": _utc_now_iso()}
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


async def main() -> None:
    # stdio is the default transport for Claude Desktop MCP integration
    app.run(transport="stdio")


if __name__ == "__main__":
    # FastMCP.run() manages its own AnyIO event loop; do not wrap it in asyncio.run().
    app.run(transport="stdio")


