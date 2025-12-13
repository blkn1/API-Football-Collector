from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class TimezoneIn(BaseModel):
    name: str


def transform_timezones(envelope: dict[str, Any]) -> list[dict[str, Any]]:
    """
    RAW -> CORE rows for core.timezones
    PK: name (timezone string)
    """
    response = envelope.get("response") or []
    rows: list[dict[str, Any]] = []

    for tz in response:
        t = TimezoneIn.model_validate({"name": tz})
        rows.append({"name": t.name})

    return rows


