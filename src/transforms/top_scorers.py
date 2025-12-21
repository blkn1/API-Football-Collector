from __future__ import annotations

from typing import Any


def transform_top_scorers(*, envelope: dict[str, Any], league_id: int, season: int) -> list[dict[str, Any]]:
    """
    Transform API-Football /players/topscorers envelope into core.top_scorers rows.

    Envelope shape:
      { get, parameters, errors, results, paging, response: [ {player: {...}, statistics: [...]}, ... ] }
    """
    resp = envelope.get("response") or []
    if not isinstance(resp, list):
        return []

    out: list[dict[str, Any]] = []
    rank = 0
    for item in resp:
        if not isinstance(item, dict):
            continue
        rank += 1

        player = item.get("player") or {}
        if not isinstance(player, dict):
            player = {}
        pid = player.get("id")
        if pid is None:
            continue

        stats = item.get("statistics") or []
        stat0 = stats[0] if isinstance(stats, list) and stats else {}
        if not isinstance(stat0, dict):
            stat0 = {}

        team = stat0.get("team") or {}
        if not isinstance(team, dict):
            team = {}

        goals_obj = stat0.get("goals") or {}
        if not isinstance(goals_obj, dict):
            goals_obj = {}

        def _to_int(v: Any) -> int | None:
            if v is None:
                return None
            try:
                return int(v)
            except Exception:
                return None

        out.append(
            {
                "league_id": int(league_id),
                "season": int(season),
                "player_id": int(pid),
                "rank": int(rank),
                "team_id": _to_int(team.get("id")),
                "team_name": (str(team.get("name")) if team.get("name") is not None else None),
                "goals": _to_int(goals_obj.get("total")),
                "assists": _to_int(goals_obj.get("assists")),
                "raw": item,
            }
        )

    return out


