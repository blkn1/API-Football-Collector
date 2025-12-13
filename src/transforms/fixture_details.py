from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel


_ISO_DT_RE = re.compile(
    # very small heuristic: YYYY-MM-DDTHH:MM:SS(.sss)?(Z|±HH:MM)?
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
    r"(?:\.\d{1,6})?"
    r"(?:Z|[+-]\d{2}:\d{2})?$"
)


def _ensure_utc_dt(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _normalize_nested_timestamps(obj: Any) -> Any:
    """
    Ensure any ISO-8601 datetime strings inside nested JSON are UTC.

    - If a datetime string has no timezone suffix, treat it as UTC and append +00:00.
    - If it has an offset or Z, convert to UTC.
    - Non-datetime strings are returned as-is.

    This keeps JSONB payloads consistent with the project rule: ALWAYS UTC.
    """
    if isinstance(obj, dict):
        return {k: _normalize_nested_timestamps(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_normalize_nested_timestamps(v) for v in obj]
    if isinstance(obj, str) and _ISO_DT_RE.match(obj):
        # datetime.fromisoformat doesn't accept Z in py<3.11; handle consistently anyway.
        s = obj[:-1] + "+00:00" if obj.endswith("Z") else obj
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return obj
        return _ensure_utc_dt(dt).isoformat()
    return obj


class FixtureDetailsIn(BaseModel):
    fixture_id: int
    events: list[dict[str, Any]] | None = None
    lineups: list[dict[str, Any]] | None = None
    statistics: list[dict[str, Any]] | None = None
    players: list[dict[str, Any]] | None = None


def transform_fixture_details(
    api_response_item: dict[str, Any],
) -> dict[str, Any] | None:
    """
    One /fixtures response item -> one CORE row for core.fixture_details.

    Expected (optional) keys on the /fixtures item:
      - fixture.id
      - events, lineups, statistics, players

    Returns a dict suitable for UPSERT into:
      core.fixture_details(fixture_id PK, events JSONB, lineups JSONB, statistics JSONB, players JSONB)
    """
    fixture = api_response_item.get("fixture") or {}
    fixture_id = fixture.get("id")
    if fixture_id is None:
        return None

    events_raw = api_response_item.get("events")
    lineups_raw = api_response_item.get("lineups")
    statistics_raw = api_response_item.get("statistics")
    players_raw = api_response_item.get("players")

    # If the /fixtures payload doesn't include any nested blocks, skip creating a details row.
    # This avoids unnecessary writes of all-NULL JSONB payloads.
    def _present(v: Any) -> bool:
        if v is None:
            return False
        if isinstance(v, list):
            return len(v) > 0
        if isinstance(v, dict):
            return len(v) > 0
        return True

    if not any(map(_present, [events_raw, lineups_raw, statistics_raw, players_raw])):
        return None

    details = FixtureDetailsIn.model_validate(
        {
            "fixture_id": fixture_id,
            "events": events_raw,
            "lineups": lineups_raw,
            "statistics": statistics_raw,
            "players": players_raw,
        }
    )

    # Normalize nested timestamps to UTC (JSONB payload should be UTC-consistent)
    events = _normalize_nested_timestamps(details.events) if details.events is not None else None
    lineups = (
        _normalize_nested_timestamps(details.lineups) if details.lineups is not None else None
    )
    statistics = (
        _normalize_nested_timestamps(details.statistics)
        if details.statistics is not None
        else None
    )
    players = _normalize_nested_timestamps(details.players) if details.players is not None else None

    return {
        "fixture_id": details.fixture_id,
        "events": events,
        "lineups": lineups,
        "statistics": statistics,
        "players": players,
    }


