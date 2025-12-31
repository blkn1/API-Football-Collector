from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from src.utils.db import get_transaction
from src.utils.logging import get_logger


logger = get_logger(component="jobs_auto_finish_stale_fixtures")

# Fixtures in "live" or intermediate states that should have finished.
# These can be safely auto-finished if they're stale.
STALE_STATUSES = ("NS", "HT", "2H", "1H", "LIVE", "BT", "ET", "P", "SUSP", "INT")

# Final statuses we won't auto-finish (already finished or abandoned)
FINAL_STATUSES = ("FT", "AET", "PEN", "AWD", "WO", "ABD", "CANC", "PST")


@dataclass(frozen=True)
class AutoFinishConfig:
    threshold_hours: int
    safety_lag_hours: int
    max_fixtures_per_run: int
    scoped_league_ids: set[int]
    dry_run: bool


def _load_daily_tracked_league_ids(cfg: dict[str, Any], *, config_path: Path) -> set[int]:
    tracked_raw = cfg.get("tracked_leagues") or []
    tracked: set[int] = set()
    if isinstance(tracked_raw, list):
        for x in tracked_raw:
            if not isinstance(x, dict) or x.get("id") is None:
                continue
            try:
                tracked.add(int(x["id"]))
            except Exception:
                continue
    if not tracked:
        raise ValueError(f"Missing tracked_leagues in {config_path}")
    return tracked


def _load_config(config_path: Path) -> AutoFinishConfig:
    cfg = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}

    # defaults (safe + conservative)
    threshold = 2  # 2 hours after kickoff, treat as stale
    safety_lag = 3  # 3 hours since last update (safety check)
    max_fixtures = 1000
    dry_run = False

    for j in cfg.get("jobs") or []:
        if not isinstance(j, dict):
            continue
        if str(j.get("job_id") or "") != "auto_finish_stale_fixtures":
            continue
        params = j.get("params") or {}
        if isinstance(params, dict):
            try:
                if params.get("threshold_hours") is not None:
                    threshold = int(params.get("threshold_hours"))
            except Exception:
                pass
            try:
                if params.get("safety_lag_hours") is not None:
                    safety_lag = int(params.get("safety_lag_hours"))
            except Exception:
                pass
            try:
                if params.get("max_fixtures_per_run") is not None:
                    max_fixtures = int(params.get("max_fixtures_per_run"))
            except Exception:
                pass
            try:
                if params.get("dry_run") is not None:
                    dry_run = bool(params.get("dry_run"))
            except Exception:
                pass
        break

    # Guardrails
    threshold = max(1, min(int(threshold), 7 * 24))  # 1h .. 7d
    safety_lag = max(1, min(int(safety_lag), 7 * 24))  # 1h .. 7d
    max_fixtures = max(1, min(int(max_fixtures), 10000))

    scoped = _load_daily_tracked_league_ids(cfg, config_path=config_path)

    return AutoFinishConfig(
        threshold_hours=threshold,
        safety_lag_hours=safety_lag,
        max_fixtures_per_run=max_fixtures,
        scoped_league_ids=scoped,
        dry_run=dry_run,
    )


def _select_stale_fixture_ids(
    *,
    threshold_hours: int,
    safety_lag_hours: int,
    limit: int,
    tracked_league_ids: set[int],
) -> list[int]:
    """
    Select fixtures that are in stale intermediate states but haven't been updated recently.

    We use a double-threshold safety check:
    1. date_utc < NOW() - threshold_hours: The fixture was scheduled to start N hours ago
    2. updated_at < NOW() - safety_lag_hours: The fixture hasn't been updated in M hours

    This prevents accidentally finishing a live match that's been recently updated.
    """
    sql = """
    SELECT f.id, f.league_id, f.status_short, f.date_utc, f.updated_at
    FROM core.fixtures f
    WHERE f.league_id = ANY(%s)
      AND f.status_short = ANY(%s)
      AND f.date_utc < NOW() - (%s::text || ' hours')::interval
      AND f.updated_at < NOW() - (%s::text || ' hours')::interval
    ORDER BY f.date_utc ASC
    LIMIT %s
    """
    with get_transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                sql,
                (
                    sorted(list(tracked_league_ids)),
                    list(STALE_STATUSES),
                    int(threshold_hours),
                    int(safety_lag_hours),
                    int(limit),
                ),
            )
            rows = cur.fetchall()
            conn.commit()
    return [int(r[0]) for r in rows]


def _auto_finish_fixtures(
    *,
    fixture_ids: list[int],
    dry_run: bool,
) -> dict[str, Any]:
    """
    Update stale fixtures to FT status.

    Returns summary statistics including leagues affected.
    """
    if not fixture_ids:
        return {"updated_count": 0, "leagues_affected": 0}

    if dry_run:
        logger.info("auto_finish_dry_run", fixture_count=len(fixture_ids))
        return {"updated_count": 0, "leagues_affected": 0, "dry_run": True}

    sql = """
    UPDATE core.fixtures
    SET status_short = 'FT',
        status_long = 'Match Finished (Auto-finished)',
        updated_at = NOW()
    WHERE id = ANY(%s)
    RETURNING id, league_id, season, status_short, date_utc
    """

    with get_transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (list(fixture_ids),))
            rows = cur.fetchall()
            conn.commit()

    # Calculate leagues affected
    leagues_affected = len(set(int(r[1]) for r in rows))

    return {
        "updated_count": len(rows),
        "leagues_affected": leagues_affected,
        "dry_run": False,
    }


def run_auto_finish_stale_fixtures(*, config_path: Path) -> None:
    """
    Maintenance job (DB-only, no API calls):
    - Find fixtures in stale intermediate states (NS, HT, 2H, 1H, LIVE, BT, ET, P, SUSP, INT)
    - Apply double-threshold safety check (date_utc < N hours ago AND updated_at < M hours ago)
    - Update status to FT directly in database
    - Log statistics

    This job is safe to run because:
    1. It only affects tracked leagues
    2. It uses two independent time thresholds
    3. It's transaction-wrapped (rollback on error)
    4. It respects max_fixtures_per_run limit
    """
    cfg = _load_config(config_path)

    stale_ids = _select_stale_fixture_ids(
        threshold_hours=cfg.threshold_hours,
        safety_lag_hours=cfg.safety_lag_hours,
        limit=cfg.max_fixtures_per_run,
        tracked_league_ids=cfg.scoped_league_ids,
    )

    if not stale_ids:
        logger.info(
            "auto_finish_no_work",
            threshold_hours=cfg.threshold_hours,
            safety_lag_hours=cfg.safety_lag_hours,
            scoped_leagues=len(cfg.scoped_league_ids),
            dry_run=cfg.dry_run,
        )
        return

    result = _auto_finish_fixtures(fixture_ids=stale_ids, dry_run=cfg.dry_run)

    logger.info(
        "auto_finish_complete",
        threshold_hours=cfg.threshold_hours,
        safety_lag_hours=cfg.safety_lag_hours,
        selected=len(stale_ids),
        updated_count=result["updated_count"],
        leagues_affected=result["leagues_affected"],
        dry_run=cfg.dry_run,
    )

