from __future__ import annotations

from src.transforms.top_scorers import transform_top_scorers


def test_transform_top_scorers_basic() -> None:
    env = {
        "response": [
            {
                "player": {"id": 10, "name": "A"},
                "statistics": [
                    {
                        "team": {"id": 100, "name": "T"},
                        "goals": {"total": 7, "assists": 2},
                    }
                ],
            },
            {
                "player": {"id": 11, "name": "B"},
                "statistics": [
                    {
                        "team": {"id": 101, "name": "U"},
                        "goals": {"total": "5", "assists": None},
                    }
                ],
            },
        ]
    }

    rows = transform_top_scorers(envelope=env, league_id=39, season=2025)
    assert len(rows) == 2

    assert rows[0]["league_id"] == 39
    assert rows[0]["season"] == 2025
    assert rows[0]["player_id"] == 10
    assert rows[0]["rank"] == 1
    assert rows[0]["team_id"] == 100
    assert rows[0]["goals"] == 7
    assert rows[0]["assists"] == 2

    assert rows[1]["player_id"] == 11
    assert rows[1]["rank"] == 2
    assert rows[1]["goals"] == 5
    assert rows[1]["assists"] is None


