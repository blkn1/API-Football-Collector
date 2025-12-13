from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class CountryIn(BaseModel):
    name: str
    code: str | None = None
    flag: str | None = None


def transform_countries(envelope: dict[str, Any]) -> list[dict[str, Any]]:
    """
    RAW -> CORE rows for core.countries
    PK: code (ISO)
    """
    response = envelope.get("response") or []
    rows: list[dict[str, Any]] = []

    for item in response:
        c = CountryIn.model_validate(item)
        if not c.code:
            # skip countries without ISO code
            continue
        rows.append({"code": c.code, "name": c.name, "flag": c.flag})

    return rows


