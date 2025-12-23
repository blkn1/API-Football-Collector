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
from src.mcp import schemas as mcp_schemas  # noqa: E402
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
    return mcp_schemas.ErrorEnvelope(error=message, ts_utc=_utc_now_iso(), details=details).model_dump(exclude_none=True)


def _ok(model: mcp_schemas.MCPModel) -> dict[str, Any]:
    """
    Serialize a validated MCP schema model into JSON-serializable dict.
    IMPORTANT: Do NOT drop None fields. Keeping explicit nulls makes tool schemas
    deterministic and reduces client-side branching (OpenAPI-like contract).
    """
    return model.model_dump()


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


LIVE_STATUSES = ("1H", "2H", "HT", "ET", "BT", "P", "LIVE", "SUSP", "INT")


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
async def get_coverage_status(league_id: int | None = None, season: int | None = None, tracked_only: bool = True) -> dict:
    """
    Get coverage metrics (mart.coverage_status) for all leagues or a specific league.

    Args:
        league_id: League ID (None = all)
        season: Season year (if omitted, uses config/jobs/daily.yaml season). If missing in config, request is rejected.
        tracked_only: When league_id is omitted, restrict results to config/jobs/daily.yaml -> tracked_leagues (default True).
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
            # Explicit league_id always allowed (even if not tracked) for debugging.
            sql_text = sql_text.format(league_filter="AND c.league_id = %s")
            rows = await _db_fetchall_async(sql_text, (int(season), int(league_id)))
        else:
            if tracked_only:
                tracked_ids = [int(x["id"]) for x in _tracked_leagues_from_daily_config()]
                if not tracked_ids:
                    return _ok_error(
                        "tracked_leagues_required",
                        details="tracked_only=true requires tracked_leagues in daily config. Set tracked_leagues or call with tracked_only=false.",
                    )
                sql_text = sql_text.format(league_filter="AND c.league_id = ANY(%s)")
                rows = await _db_fetchall_async(sql_text, (int(season), tracked_ids))
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
                    "flags": (r[11] if len(r) > 11 else None),
                }
            )

        return _ok(mcp_schemas.CoverageStatus(season=int(season), coverage=[mcp_schemas.CoverageRow.model_validate(x) for x in out], ts_utc=_utc_now_iso()))
    except Exception as e:
        return _ok_error("get_coverage_status_failed", details=str(e))


@app.tool()
async def get_coverage_summary(season: int | None = None, tracked_only: bool = True) -> dict:
    """
    Quick overview of coverage for a season.

    Args:
        season: Season year (defaults like get_coverage_status)
        tracked_only: Restrict summary to tracked_leagues (default True).
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

        if tracked_only:
            tracked_ids = [int(x["id"]) for x in _tracked_leagues_from_daily_config()]
            if not tracked_ids:
                return _ok_error(
                    "tracked_leagues_required",
                    details="tracked_only=true requires tracked_leagues in daily config. Set tracked_leagues or call with tracked_only=false.",
                )
            row = await _db_fetchone_async(
                """
                SELECT
                  c.season,
                  COUNT(*) AS rows,
                  COUNT(DISTINCT c.league_id) AS leagues,
                  COUNT(DISTINCT c.endpoint) AS endpoints,
                  ROUND(AVG(c.overall_coverage)::numeric, 2) AS avg_overall_coverage,
                  MAX(c.calculated_at) AS last_calculated_at
                FROM mart.coverage_status c
                WHERE c.season = %s
                  AND c.league_id = ANY(%s)
                GROUP BY c.season
                """,
                (int(season), tracked_ids),
            )
        else:
            row = await _db_fetchone_async(queries.COVERAGE_SUMMARY, (int(season),))
        if not row:
            return _ok(mcp_schemas.CoverageSummary(season=int(season), summary=None, ts_utc=_utc_now_iso()))
        summary = mcp_schemas.CoverageSummaryRow(
            rows=int(row[1]),
            leagues=int(row[2]),
            endpoints=int(row[3]),
            avg_overall_coverage=_to_float_or_none(row[4]),
            last_calculated_at_utc=_to_iso_or_none(row[5]),
        )
        return _ok(mcp_schemas.CoverageSummary(season=int(row[0]), summary=summary, ts_utc=_utc_now_iso()))
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
            return _ok(
                mcp_schemas.RateLimitStatus(
                    source="raw.api_responses (no rows with quota headers yet)",
                    daily_remaining=None,
                    minute_remaining=None,
                    observed_at_utc=None,
                    ts_utc=_utc_now_iso(),
                )
            )

        observed_at, daily_raw, minute_raw = row
        return _ok(
            mcp_schemas.RateLimitStatus(
                source="raw.api_responses.response_headers",
                daily_remaining=_to_int_or_none(daily_raw),
                minute_remaining=_to_int_or_none(minute_raw),
                observed_at_utc=_to_iso_or_none(observed_at),
                ts_utc=_utc_now_iso(),
            )
        )
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
            return _ok(
                mcp_schemas.LiveLoopStatus(
                    window={"since_minutes": mins},
                    running=False,
                    requests=0,
                    last_fetched_at_utc=None,
                    ts_utc=_utc_now_iso(),
                )
            )
        reqs, last_dt = row
        requests = _to_int_or_none(reqs) or 0
        return _ok(
            mcp_schemas.LiveLoopStatus(
                window={"since_minutes": mins},
                running=bool(requests > 0),
                requests=int(requests),
                last_fetched_at_utc=_to_iso_or_none(last_dt),
                ts_utc=_utc_now_iso(),
            )
        )
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
        row = await _db_fetchone_async(queries.DAILY_FIXTURES_BY_DATE_PAGING_METRICS_QUERY, (mins,))
        if not row:
            return _ok(
                mcp_schemas.DailyFixturesByDateStatus(
                    window={"since_minutes": mins},
                    running=False,
                    requests=0,
                    global_requests=0,
                    pages_fetched=0,
                    max_page=None,
                    results_sum=0,
                    last_fetched_at_utc=None,
                    ts_utc=_utc_now_iso(),
                )
            )
        reqs, last_dt, global_reqs, global_pages_distinct, global_max_page, global_results_sum = row
        requests = _to_int_or_none(reqs) or 0
        return _ok(
            mcp_schemas.DailyFixturesByDateStatus(
                window={"since_minutes": mins},
                running=bool(requests > 0),
                requests=int(requests),
                global_requests=int(_to_int_or_none(global_reqs) or 0),
                pages_fetched=int(_to_int_or_none(global_pages_distinct) or 0),
                max_page=_to_int_or_none(global_max_page),
                results_sum=int(_to_int_or_none(global_results_sum) or 0),
                last_fetched_at_utc=_to_iso_or_none(last_dt),
                ts_utc=_utc_now_iso(),
            )
        )
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
        return _ok(
            mcp_schemas.LastSyncTime(
                endpoint=endpoint,
                last_fetched_at_utc=_to_iso_or_none(last_dt),
                ts_utc=_utc_now_iso(),
            )
        )
    except Exception as e:
        return _ok_error("get_last_sync_time_failed", details=str(e))


@app.tool()
async def query_fixtures(
    league_id: int | None = None,
    date: str | None = None,
    status: str | None = None,
    limit: int = 10,
) -> dict:
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
        items = [mcp_schemas.FixtureRow.model_validate(x) for x in out]
        return _ok(mcp_schemas.FixturesQuery(items=items, ts_utc=_utc_now_iso()))
    except Exception as e:
        return _ok_error("query_fixtures_failed", details=str(e))


@app.tool()
async def get_stale_live_fixtures_status(
    threshold_minutes: int = 30,
    tracked_only: bool = True,
    scope_source: str = "daily",
    live_config_path: str = "config/jobs/live.yaml",
    limit: int = 50,
) -> dict:
    """
    Ops tool: find fixtures that still look "live" but haven't updated recently.

    Args:
        threshold_minutes: Consider a fixture stale if updated_at < now - threshold (default 30)
        tracked_only: If true, only count/return fixtures whose league_id is in the chosen scope.
        scope_source: Which "tracked" definition to use when tracked_only=true:
            - "daily": config/jobs/daily.yaml -> tracked_leagues
            - "live":  config/jobs/live.yaml -> jobs[live_fixtures_all].filters.tracked_leagues
        live_config_path: Path to live config (only used when scope_source="live")
        limit: Max fixtures to return (default 50, capped at 200)
    """
    try:
        mins = max(5, min(int(threshold_minutes), 24 * 60))
        safe_limit = max(1, min(int(limit), 200))

        rows = await _db_fetchall_async(queries.STALE_LIVE_FIXTURES_QUERY, (list(LIVE_STATUSES), int(mins), int(safe_limit)))

        tracked_set: set[int] = set()
        if tracked_only:
            src = (scope_source or "daily").strip().lower()
            if src not in {"daily", "live"}:
                return _ok_error("invalid_scope_source", details="scope_source must be 'daily' or 'live'")
            if src == "live":
                # NOTE: live.yaml uses a plain list of ints under filters.tracked_leagues
                try:
                    p = Path(str(live_config_path))
                    live_cfg = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
                    jobs = live_cfg.get("jobs") or []
                    for j in jobs:
                        if not isinstance(j, dict):
                            continue
                        if str(j.get("job_id") or "") != "live_fixtures_all":
                            continue
                        filters = j.get("filters") or {}
                        tl = (filters or {}).get("tracked_leagues") or []
                        tracked_set = {int(x) for x in tl}
                        break
                except Exception as e:
                    return _ok_error("live_config_parse_failed", details=str(e))
                if not tracked_set:
                    return _ok_error(
                        "tracked_leagues_required",
                        details="tracked_only=true scope_source=live requires live_fixtures_all.filters.tracked_leagues in live config",
                    )
            else:
                tracked_set = {int(x["id"]) for x in _tracked_leagues_from_daily_config()}
                if not tracked_set:
                    return _ok_error("tracked_leagues_required", details="tracked_only=true requires tracked_leagues in daily config.")

        out: list[dict[str, Any]] = []
        ignored_untracked = 0
        for r in rows:
            league_id = int(r[1])
            if tracked_only and league_id not in tracked_set:
                ignored_untracked += 1
                continue
            out.append(
                {
                    "id": int(r[0]),
                    "league_id": league_id,
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

        fixtures_items = [mcp_schemas.FixtureRow.model_validate(x) for x in out]
        return _ok(
            mcp_schemas.StaleLiveFixturesStatus(
                threshold_minutes=int(mins),
                tracked_only=bool(tracked_only),
                scope_source=(scope_source or "daily").strip().lower(),
                stale_count=len(fixtures_items),
                ignored_untracked=int(ignored_untracked),
                fixtures=fixtures_items,
                ts_utc=_utc_now_iso(),
            )
        )
    except Exception as e:
        return _ok_error("get_stale_live_fixtures_status_failed", details=str(e))


@app.tool()
async def query_standings(league_id: int, season: int) -> dict:
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
        items = [mcp_schemas.StandingsRow.model_validate(x) for x in out]
        return _ok(mcp_schemas.StandingsQuery(items=items, ts_utc=_utc_now_iso()))
    except Exception as e:
        return _ok_error("query_standings_failed", details=str(e))


@app.tool()
async def query_teams(league_id: int | None = None, search: str | None = None, limit: int = 20) -> dict:
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
        items = [mcp_schemas.TeamRow.model_validate(x) for x in out]
        return _ok(mcp_schemas.TeamsQuery(items=items, ts_utc=_utc_now_iso()))
    except Exception as e:
        return _ok_error("query_teams_failed", details=str(e))


@app.tool()
async def get_league_info(league_id: int) -> dict:
    """
    Get league info (core.leagues).
    """
    try:
        row = await _db_fetchone_async(queries.LEAGUE_INFO_QUERY, (int(league_id),))
        if not row:
            return _ok(mcp_schemas.LeagueInfoResponse(league=None, ts_utc=_utc_now_iso()))
        league = mcp_schemas.LeagueInfo(
            id=int(row[0]),
            name=row[1],
            type=row[2],
            logo=row[3],
            country_name=row[4],
            country_code=row[5],
            country_flag=row[6],
            updated_at_utc=_to_iso_or_none(row[7]),
        )
        return _ok(mcp_schemas.LeagueInfoResponse(league=league, ts_utc=_utc_now_iso()))
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
            return _ok(mcp_schemas.DatabaseStatsResponse(stats=None, ts_utc=_utc_now_iso()))
        stats = mcp_schemas.DatabaseStats(
            raw_api_responses=int(row[0]),
            core_leagues=int(row[1]),
            core_teams=int(row[2]),
            core_venues=int(row[3]),
            core_fixtures=int(row[4]),
            core_fixture_details=int(row[5]),
            core_injuries=int(row[6]),
            core_fixture_players=int(row[7]),
            core_fixture_events=int(row[8]),
            core_fixture_statistics=int(row[9]),
            core_fixture_lineups=int(row[10]),
            core_standings=int(row[11]),
            core_top_scorers=int(row[12]),
            core_team_statistics=int(row[13]),
            raw_last_fetched_at_utc=_to_iso_or_none(row[14]),
            core_fixtures_last_updated_at_utc=_to_iso_or_none(row[15]),
        )
        return _ok(mcp_schemas.DatabaseStatsResponse(stats=stats, ts_utc=_utc_now_iso()))
    except Exception as e:
        return _ok_error("get_database_stats_failed", details=str(e))


@app.tool()
async def query_injuries(
    league_id: int | None = None,
    season: int | None = None,
    team_id: int | None = None,
    player_id: int | None = None,
    limit: int = 50,
) -> dict:
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
        items = [mcp_schemas.InjuryRow.model_validate(x) for x in out]
        return _ok(mcp_schemas.InjuriesQuery(items=items, ts_utc=_utc_now_iso()))
    except Exception as e:
        return _ok_error("query_injuries_failed", details=str(e))


@app.tool()
async def get_fixture_detail_status(fixture_id: int) -> dict:
    """
    For a fixture_id, report whether CORE has players/events/statistics/lineups,
    and the last RAW fetch time for each corresponding endpoint.
    """
    try:
        row = await _db_fetchone_async(queries.FIXTURE_DETAIL_STATUS_QUERY, (int(fixture_id),))
        if not row:
            return _ok(mcp_schemas.FixtureDetailStatusResponse(fixture=None, ts_utc=_utc_now_iso()))

        fx = mcp_schemas.FixtureDetailStatus(
            fixture_id=int(row[0]),
            league_id=int(row[1]),
            season=_to_int_or_none(row[2]),
            date_utc=_to_iso_or_none(row[3]),
            status_short=row[4],
            has_players=bool(row[5]),
            has_events=bool(row[6]),
            has_statistics=bool(row[7]),
            has_lineups=bool(row[8]),
            last_players_fetch_utc=_to_iso_or_none(row[9]),
            last_events_fetch_utc=_to_iso_or_none(row[10]),
            last_statistics_fetch_utc=_to_iso_or_none(row[11]),
            last_lineups_fetch_utc=_to_iso_or_none(row[12]),
        )

        return _ok(mcp_schemas.FixtureDetailStatusResponse(fixture=fx, ts_utc=_utc_now_iso()))
    except Exception as e:
        return _ok_error("get_fixture_detail_status_failed", details=str(e))


@app.tool()
async def query_fixture_players(fixture_id: int, team_id: int | None = None, limit: int = 300) -> dict:
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
        items = [mcp_schemas.FixturePlayerRow.model_validate(x) for x in out]
        return _ok(mcp_schemas.FixturePlayersQuery(items=items, ts_utc=_utc_now_iso()))
    except Exception as e:
        return _ok_error("query_fixture_players_failed", details=str(e))


@app.tool()
async def query_fixture_events(fixture_id: int, limit: int = 300) -> dict:
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
        items = [mcp_schemas.FixtureEventRow.model_validate(x) for x in out]
        return _ok(mcp_schemas.FixtureEventsQuery(items=items, ts_utc=_utc_now_iso()))
    except Exception as e:
        return _ok_error("query_fixture_events_failed", details=str(e))


@app.tool()
async def query_fixture_statistics(fixture_id: int) -> dict:
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
        items = [mcp_schemas.FixtureStatisticsRow.model_validate(x) for x in out]
        return _ok(mcp_schemas.FixtureStatisticsQuery(items=items, ts_utc=_utc_now_iso()))
    except Exception as e:
        return _ok_error("query_fixture_statistics_failed", details=str(e))


@app.tool()
async def query_fixture_lineups(fixture_id: int) -> dict:
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
        items = [mcp_schemas.FixtureLineupRow.model_validate(x) for x in out]
        return _ok(mcp_schemas.FixtureLineupsQuery(items=items, ts_utc=_utc_now_iso()))
    except Exception as e:
        return _ok_error("query_fixture_lineups_failed", details=str(e))


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

        return _ok(
            mcp_schemas.TrackedLeaguesResponse(
                tracked_leagues=[mcp_schemas.TrackedLeagueRow.model_validate(x) for x in leagues],
                league_overrides=[mcp_schemas.LeagueOverrideRow.model_validate(x) for x in overrides],
                configured_leagues_union=[
                    mcp_schemas.ConfiguredLeagueUnionRow.model_validate(x)
                    for x in sorted(union.values(), key=lambda d: int(d["league_id"]))
                ],
                ts_utc=_utc_now_iso(),
            )
        )
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
        ]
        cfg_jobs: list[dict[str, Any]] = []
        for p in cfg_files:
            if not p.exists():
                continue
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

        async def _raw_last_seen_for_job(job: dict[str, Any]) -> dict[str, Any]:
            """
            Fallback evidence for 'unknown' jobs:
            - Read RAW to find the most recent fetch for the job's endpoint pattern.
            - Adds:
              - last_raw_fetched_at_utc
              - last_raw_endpoints (for fixture_details jobs)
            """
            jid = str(job.get("job_id") or "")
            ep = job.get("endpoint")

            # Disabled jobs: don't spend DB queries.
            if not bool(job.get("enabled", False)):
                return {"last_raw_fetched_at_utc": None, "last_raw_endpoints": None}

            # Job-specific RAW patterns (avoid ambiguity for /fixtures shared endpoint).
            if jid == "daily_fixtures_by_date":
                row = await _db_fetchone_async(queries.LAST_SYNC_FIXTURES_DAILY_QUERY, ())
                dt = row[0] if row else None
                return {"last_raw_fetched_at_utc": _to_iso_or_none(dt), "last_raw_endpoints": ["/fixtures"]}

            if jid == "fixtures_backfill_league_season":
                row = await _db_fetchone_async(queries.LAST_SYNC_FIXTURES_BACKFILL_QUERY, ())
                dt = row[0] if row else None
                return {"last_raw_fetched_at_utc": _to_iso_or_none(dt), "last_raw_endpoints": ["/fixtures(from/to)"]}

            if jid == "stale_live_refresh":
                row = await _db_fetchone_async(queries.LAST_SYNC_FIXTURES_IDS_QUERY, ())
                dt = row[0] if row else None
                return {"last_raw_fetched_at_utc": _to_iso_or_none(dt), "last_raw_endpoints": ["/fixtures(ids)"]}

            # Fixture details jobs use endpoint "/fixtures/*" in config, but RAW uses concrete endpoints.
            if (ep == "/fixtures/*") or jid.startswith("fixture_details_"):
                row_any = await _db_fetchone_async(queries.LAST_SYNC_FIXTURE_DETAILS_ANY_QUERY, ())
                dt_any = row_any[0] if row_any else None
                # Per-endpoint breakdown (best-effort)
                per: dict[str, str | None] = {}
                for e in ("/fixtures/players", "/fixtures/events", "/fixtures/statistics", "/fixtures/lineups"):
                    r = await _db_fetchone_async(queries.LAST_SYNC_TIME_QUERY, (e,))
                    per[e] = _to_iso_or_none(r[0]) if r else None
                return {"last_raw_fetched_at_utc": _to_iso_or_none(dt_any), "last_raw_endpoints": per}

            # Generic endpoint: use last sync time for the endpoint itself.
            if isinstance(ep, str) and ep.startswith("/"):
                row = await _db_fetchone_async(queries.LAST_SYNC_TIME_QUERY, (ep,))
                dt = row[0] if row else None
                return {"last_raw_fetched_at_utc": _to_iso_or_none(dt), "last_raw_endpoints": [ep]}

            return {"last_raw_fetched_at_utc": None, "last_raw_endpoints": None}

        # Enrich with RAW evidence when logs are missing.
        enriched: list[dict[str, Any]] = []
        for j in merged:
            # Prefer logs when present
            last_log = j.get("last_event_ts_utc")
            if last_log:
                enriched.append({**j, "last_seen_at_utc": last_log, "last_seen_source": "logs"})
                continue

            raw_ev = await _raw_last_seen_for_job(j)
            last_raw = raw_ev.get("last_raw_fetched_at_utc")
            if last_raw:
                enriched.append({**j, **raw_ev, "last_seen_at_utc": last_raw, "last_seen_source": "raw"})
            else:
                enriched.append({**j, **raw_ev, "last_seen_at_utc": None, "last_seen_source": None})

        return _ok(
            mcp_schemas.JobStatusResponse(
                jobs=[mcp_schemas.JobStatusRow.model_validate(x) for x in enriched],
                log_file=logs.get("log_file"),
                ts_utc=_utc_now_iso(),
            )
        )
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

        return _ok(
            mcp_schemas.BackfillProgressResponse(
                filters={"job_id": job_id_str, "season": season_int, "include_completed": bool(include_completed), "limit": safe_limit},
                summaries=[mcp_schemas.BackfillSummaryRow.model_validate(x) for x in summaries],
                tasks=[mcp_schemas.BackfillTaskRow.model_validate(x) for x in tasks],
                ts_utc=_utc_now_iso(),
            )
        )
    except Exception as e:
        return _ok_error("get_backfill_progress_failed", details=str(e))


@app.tool()
async def get_standings_refresh_progress(job_id: str = "daily_standings") -> dict:
    """
    Read cursor-based batching progress for standings refresh.

    Source: core.standings_refresh_progress

    Useful when daily_standings uses mode.max_leagues_per_run to run "parça parça".
    """
    try:
        jid = str(job_id or "daily_standings")
        row = await _db_fetchone_async(queries.STANDINGS_REFRESH_PROGRESS_QUERY, (jid,))
        if not row:
            return _ok(mcp_schemas.StandingsRefreshProgressResponse(job_id=jid, exists=False, progress=None, ts_utc=_utc_now_iso()))
        progress = mcp_schemas.StandingsRefreshProgress(
            cursor=_to_int_or_none(row[1]),
            total_pairs=_to_int_or_none(row[2]),
            last_run_at_utc=_to_iso_or_none(row[3]),
            last_error=row[4],
            lap_count=_to_int_or_none(row[5]),
            last_full_pass_at_utc=_to_iso_or_none(row[6]),
            updated_at_utc=_to_iso_or_none(row[7]),
        )
        return _ok(mcp_schemas.StandingsRefreshProgressResponse(job_id=jid, exists=True, progress=progress, ts_utc=_utc_now_iso()))
    except Exception as e:
        return _ok_error("get_standings_refresh_progress_failed", details=str(e))


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

        summary_m = mcp_schemas.RawErrorSummaryRow.model_validate(summary) if summary is not None else None
        endpoints_m = [mcp_schemas.RawErrorsByEndpointRow.model_validate(x) for x in endpoints]
        return _ok(
            mcp_schemas.RawErrorSummaryResponse(
                window={"since_minutes": mins},
                filters={"endpoint": ep, "top_endpoints_limit": safe_top},
                summary=summary_m,
                by_endpoint=endpoints_m,
                ts_utc=_utc_now_iso(),
            )
        )
    except Exception as e:
        return _ok_error("get_raw_error_summary_failed", details=str(e))


@app.tool()
async def get_raw_error_samples(
    since_minutes: int = 60,
    endpoint: str | None = None,
    limit: int = 25,
) -> dict:
    """
    Return recent RAW rows where the API envelope "errors" is non-empty.

    Use this to debug cases where status_code is 2xx but the envelope contains errors
    (e.g., rateLimit, invalid params, partial failures).
    """
    try:
        mins = max(1, min(int(since_minutes), 60 * 24 * 14))  # cap at 14 days
        ep = str(endpoint) if endpoint is not None else None
        safe_limit = max(1, min(int(limit), 200))

        rows = await _db_fetchall_async(queries.RAW_ERROR_SAMPLES_QUERY, (mins, ep, ep, safe_limit))
        samples: list[dict[str, Any]] = []
        for r in rows:
            samples.append(
                {
                    "id": _to_int_or_none(r[0]),
                    "endpoint": r[1],
                    "requested_params": r[2],
                    "status_code": _to_int_or_none(r[3]),
                    "errors": r[4],
                    "results": _to_int_or_none(r[5]),
                    "fetched_at_utc": _to_iso_or_none(r[6]),
                }
            )

        return _ok(
            mcp_schemas.RawErrorSamplesResponse(
                window={"since_minutes": mins},
                filters={"endpoint": ep, "limit": safe_limit},
                samples=[mcp_schemas.RawErrorSampleRow.model_validate(x) for x in samples],
                ts_utc=_utc_now_iso(),
            )
        )
    except Exception as e:
        return _ok_error("get_raw_error_samples_failed", details=str(e))


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
        return _ok(
            mcp_schemas.RecentLogErrorsResponse(
                job_name=job_name,
                log_file=parsed.get("log_file"),
                errors=[mcp_schemas.RecentLogErrorRow.model_validate(x) for x in (parsed.get("errors") or [])],
                ts_utc=_utc_now_iso(),
            )
        )
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


