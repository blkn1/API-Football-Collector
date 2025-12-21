from __future__ import annotations

from src.transforms.team_statistics import transform_team_statistics


def test_transform_team_statistics_basic() -> None:
    env = {"response": {"form": "WWDL", "fixtures": {"played": {"total": 10}}}}
    row = transform_team_statistics(envelope=env, league_id=39, season=2025, team_id=33)
    assert row is not None
    assert row["league_id"] == 39
    assert row["season"] == 2025
    assert row["team_id"] == 33
    assert row["form"] == "WWDL"
    assert isinstance(row["raw"], dict)


