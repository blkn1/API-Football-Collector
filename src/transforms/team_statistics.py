from __future__ import annotations

from typing import Any


def transform_team_statistics(*, envelope: dict[str, Any], league_id: int, season: int, team_id: int) -> dict[str, Any] | None:
    """
    Transform API-Football /teams/statistics envelope into core.team_statistics row.

    Envelope shape:
      { get, parameters, errors, results, paging, response: { ... } }
    """
    resp = envelope.get("response")
    if not isinstance(resp, dict) or not resp:
        return None

    form = resp.get("form")
    if form is not None:
        form = str(form)

    return {
        "league_id": int(league_id),
        "season": int(season),
        "team_id": int(team_id),
        "form": form,
        "raw": resp,
    }


