from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel


def _ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class TeamRef(BaseModel):
    id: int
    name: str | None = None


class StandingEntry(BaseModel):
    league_id: int
    season: int
    team_id: int

    rank: int | None = None
    points: int | None = None
    goalsDiff: int | None = None
    goals_for: int | None = None
    goals_against: int | None = None
    form: str | None = None

    # API provides these as nested objects; we store them in JSONB
    all_stats: dict[str, Any] | None = None
    home_stats: dict[str, Any] | None = None
    away_stats: dict[str, Any] | None = None

    # Extra fields that exist in core.standings schema
    status: str | None = None
    description: str | None = None
    group_name: str | None = None
    updated_api: datetime | None = None


def transform_standings(envelope: dict[str, Any]) -> list[dict[str, Any]]:
    """
    RAW -> CORE rows for core.standings

    CRITICAL: response structure is nested:
      envelope.response[0].league.standings[0] -> list of entries
    """
    resp = envelope.get("response") or []
    rows: list[dict[str, Any]] = []

    for item in resp:
        league = item.get("league") or {}
        league_id = league.get("id")
        season = league.get("season")
        if league_id is None or season is None:
            continue

        standings_groups = league.get("standings") or []
        if not standings_groups or not isinstance(standings_groups, list):
            continue

        # standings is a nested array: [ [ {...}, {...} ] ]
        table = standings_groups[0] if standings_groups else []
        if not isinstance(table, list):
            continue

        for e in table:
            team = (e.get("team") or {}) if isinstance(e, dict) else {}
            team_id = team.get("id")
            if team_id is None:
                continue

            updated = e.get("update")
            updated_dt: datetime | None = None
            if updated:
                try:
                    updated_dt = _ensure_utc(datetime.fromisoformat(str(updated).replace("Z", "+00:00")))
                except Exception:
                    updated_dt = None

            all_stats = e.get("all") or {}
            goals_block = (all_stats.get("goals") or {}) if isinstance(all_stats, dict) else {}
            gf = None
            ga = None
            try:
                gf = (goals_block.get("for") if isinstance(goals_block, dict) else None)
                ga = (goals_block.get("against") if isinstance(goals_block, dict) else None)
                gf = int(gf) if gf is not None else None
                ga = int(ga) if ga is not None else None
            except Exception:
                gf, ga = None, None

            se = StandingEntry.model_validate(
                {
                    "league_id": int(league_id),
                    "season": int(season),
                    "team_id": int(team_id),
                    "rank": e.get("rank"),
                    "points": e.get("points"),
                    "goalsDiff": e.get("goalsDiff"),
                    "goals_for": gf,
                    "goals_against": ga,
                    "form": e.get("form"),
                    "status": e.get("status"),
                    "description": e.get("description"),
                    "group_name": e.get("group"),
                    "all_stats": all_stats,
                    "home_stats": e.get("home"),
                    "away_stats": e.get("away"),
                    "updated_api": updated_dt,
                }
            )

            rows.append(
                {
                    "league_id": se.league_id,
                    "season": se.season,
                    "team_id": se.team_id,
                    "rank": se.rank,
                    "points": se.points,
                    "goals_diff": se.goalsDiff,
                    "goals_for": se.goals_for,
                    "goals_against": se.goals_against,
                    "form": se.form,
                    "status": se.status,
                    "description": se.description,
                    "group_name": se.group_name,
                    "all_stats": se.all_stats,
                    "home_stats": se.home_stats,
                    "away_stats": se.away_stats,
                    "updated_api": se.updated_api,
                }
            )

    return rows


