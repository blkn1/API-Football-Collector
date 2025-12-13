from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class VenueIn(BaseModel):
    id: int | None = None
    name: str | None = None
    address: str | None = None
    city: str | None = None
    country: str | None = None
    capacity: int | None = None
    surface: str | None = None
    image: str | None = None


def transform_venues(envelope: dict[str, Any]) -> list[dict[str, Any]]:
    """
    RAW -> CORE rows for core.venues from GET /venues response.
    """
    response = envelope.get("response") or []
    rows: list[dict[str, Any]] = []
    seen: set[int] = set()

    for item in response:
        v = VenueIn.model_validate(item)
        if v.id is None:
            continue
        if v.id in seen:
            continue
        seen.add(v.id)
        rows.append(
            {
                "id": v.id,
                "name": v.name,
                "address": v.address,
                "city": v.city,
                "country": v.country,
                "capacity": v.capacity,
                "surface": v.surface,
                "image": v.image,
            }
        )

    return rows


def transform_venues_from_teams(envelope: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Extract venues from /teams response items.
    """
    response = envelope.get("response") or []
    rows: list[dict[str, Any]] = []
    seen: set[int] = set()

    for item in response:
        venue = item.get("venue") or {}
        v = VenueIn.model_validate(venue)
        if v.id is None:
            continue
        if v.id in seen:
            continue
        seen.add(v.id)
        rows.append(
            {
                "id": v.id,
                "name": v.name,
                "address": v.address,
                "city": v.city,
                "country": v.country,
                "capacity": v.capacity,
                "surface": v.surface,
                "image": v.image,
            }
        )

    return rows


