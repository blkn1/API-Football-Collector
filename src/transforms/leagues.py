from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class LeagueObj(BaseModel):
    id: int
    name: str
    type: str | None = None
    logo: str | None = None


class CountryObj(BaseModel):
    name: str | None = None
    code: str | None = None
    flag: str | None = None


class LeagueResponseItem(BaseModel):
    league: LeagueObj
    country: CountryObj | None = None
    seasons: list[dict[str, Any]] | None = None


def transform_leagues(
    envelope: dict[str, Any],
    *,
    tracked_league_ids: set[int] | None = None,
) -> list[dict[str, Any]]:
    """
    RAW -> CORE rows for core.leagues
    PK: id (API league id)

    If tracked_league_ids is provided, filters to those league IDs.
    """
    response = envelope.get("response") or []
    rows: list[dict[str, Any]] = []

    for item in response:
        lr = LeagueResponseItem.model_validate(item)
        league_id = lr.league.id
        if tracked_league_ids is not None and league_id not in tracked_league_ids:
            continue

        country = lr.country or CountryObj()
        rows.append(
            {
                "id": league_id,
                "name": lr.league.name,
                "type": lr.league.type,
                "logo": lr.league.logo,
                "country_name": country.name,
                "country_code": country.code,
                "country_flag": country.flag,
                "seasons": lr.seasons,
            }
        )

    return rows


