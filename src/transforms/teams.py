from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class TeamIn(BaseModel):
    id: int
    name: str
    code: str | None = None
    country: str | None = None
    founded: int | None = None
    national: bool | None = None
    logo: str | None = None


def transform_teams(envelope: dict[str, Any]) -> list[dict[str, Any]]:
    """
    RAW -> CORE rows for core.teams
    PK: id (API team id)
    """
    response = envelope.get("response") or []
    rows: list[dict[str, Any]] = []

    for item in response:
        t = TeamIn.model_validate(item.get("team") or {})
        venue = item.get("venue") or {}
        venue_id = venue.get("id")
        rows.append(
            {
                "id": t.id,
                "name": t.name,
                "code": t.code,
                "country": t.country,
                "founded": t.founded,
                "national": t.national,
                "logo": t.logo,
                "venue_id": venue_id,
            }
        )

    return rows


