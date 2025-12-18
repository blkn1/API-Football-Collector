from __future__ import annotations

from src.jobs.season_rollover import _extract_league_ids_from_leagues_response


def test_extract_league_ids_from_leagues_response() -> None:
    env = {
        "response": [
            {"league": {"id": 39, "name": "Premier League"}},
            {"league": {"id": "140", "name": "La Liga"}},
            {"league": {"id": None}},
            {"league": {}},
            {},
            "bad",
        ]
    }
    assert _extract_league_ids_from_leagues_response(env) == {39, 140}


