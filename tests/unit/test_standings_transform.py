from __future__ import annotations

import json
from pathlib import Path

from transforms.standings import transform_standings


def test_transform_standings_nested_structure() -> None:
    p = Path(__file__).resolve().parents[1] / "fixtures" / "api_responses" / "standings.json"
    env = json.loads(p.read_text(encoding="utf-8"))
    rows = transform_standings(env)

    assert len(rows) == 2
    r1 = rows[0]
    assert r1["league_id"] == 39
    assert r1["season"] == 2024
    assert r1["team_id"] == 33
    assert r1["rank"] == 1
    assert r1["points"] == 30
    assert r1["goals_diff"] == 15
    assert r1["goals_for"] == 28
    assert r1["goals_against"] == 13
    assert r1["form"] == "WWDWL"
    assert isinstance(r1["all_stats"], dict)


