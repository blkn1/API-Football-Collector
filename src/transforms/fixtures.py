from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel

try:
    # scripts/ context (scripts add src/ to sys.path)
    from transforms.fixture_details import transform_fixture_details  # type: ignore
except Exception:  # pragma: no cover
    from src.transforms.fixture_details import transform_fixture_details

def _ensure_utc(dt: datetime) -> datetime:
    """
    API-Football fixture dates are UTC by default and typically include an offset.
    If the parsed datetime is naive, treat it as UTC to satisfy the project rule:
    DB timestamps are always stored in UTC.
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class FixtureStatusIn(BaseModel):
    long: str | None = None
    short: str | None = None
    elapsed: int | None = None


class FixtureVenueIn(BaseModel):
    id: int | None = None
    name: str | None = None
    city: str | None = None


class FixtureIn(BaseModel):
    id: int
    referee: str | None = None
    timezone: str | None = None
    date: datetime
    timestamp: int | None = None
    venue: FixtureVenueIn | None = None
    status: FixtureStatusIn | None = None


class LeagueIn(BaseModel):
    id: int
    season: int | None = None
    round: str | None = None


class TeamSideIn(BaseModel):
    id: int


class TeamsIn(BaseModel):
    home: TeamSideIn
    away: TeamSideIn


class GoalsIn(BaseModel):
    home: int | None = None
    away: int | None = None


class FixtureResponseItemIn(BaseModel):
    fixture: FixtureIn
    league: LeagueIn
    teams: TeamsIn
    goals: GoalsIn | None = None
    score: dict[str, Any] | None = None


def transform_fixtures(
    envelope: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """
    RAW -> CORE rows for core.fixtures
    PK: id (API fixture id)

    Expected response item schema (subset):
      - response[].fixture.{id,date,timestamp,referee,timezone,venue,status}
      - response[].league.{id,season,round}
      - response[].teams.{home.id,away.id}
      - response[].goals.{home,away}
      - response[].score (nested dict, stored as JSONB)
    """
    response = envelope.get("response") or []
    fixtures_by_id: dict[int, dict[str, Any]] = {}
    details_by_id: dict[int, dict[str, Any]] = {}

    for item in response:
        r = FixtureResponseItemIn.model_validate(item)
        fixture_id = r.fixture.id

        status = r.fixture.status or FixtureStatusIn()
        venue = r.fixture.venue or FixtureVenueIn()
        goals = r.goals or GoalsIn()

        fixtures_by_id[fixture_id] = {
            "id": fixture_id,
            "league_id": r.league.id,
            "season": r.league.season,
            "round": r.league.round,
            "date": _ensure_utc(r.fixture.date),
            "api_timestamp": r.fixture.timestamp,
            "referee": r.fixture.referee,
            "timezone": r.fixture.timezone,
            # API sometimes returns venue.id=0 (or missing) meaning "unknown / not set".
            # FK requires referenced venues to exist, so treat 0 as NULL.
            "venue_id": (int(venue.id) if venue.id not in (None, 0) else None),
            "home_team_id": r.teams.home.id,
            "away_team_id": r.teams.away.id,
            "status_short": status.short,
            "status_long": status.long,
            "elapsed": status.elapsed,
            "goals_home": goals.home,
            "goals_away": goals.away,
            "score": r.score,
        }

        details_row = transform_fixture_details(item)
        if details_row is not None:
            details_by_id[fixture_id] = details_row

    # Deterministic ordering for tests/reproducibility
    fixtures_rows = [fixtures_by_id[k] for k in sorted(fixtures_by_id)]
    details_rows = [details_by_id[k] for k in sorted(details_by_id)]
    return fixtures_rows, details_rows


