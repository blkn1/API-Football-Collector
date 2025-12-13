from __future__ import annotations

import json
from datetime import timezone
from pathlib import Path

from transforms.fixtures import transform_fixtures


def test_transform_fixtures_single() -> None:
    p = (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "api_responses"
        / "fixtures_single.json"
    )
    envelope = json.loads(p.read_text(encoding="utf-8"))
    fixtures_rows, details_rows = transform_fixtures(envelope)

    assert len(fixtures_rows) == 1
    r = fixtures_rows[0]

    assert r["id"] == 1234567
    assert r["league_id"] == 39
    assert r["season"] == 2024
    assert r["round"] == "Regular Season - 14"

    assert r["home_team_id"] == 33
    assert r["away_team_id"] == 34

    assert r["status_short"] == "FT"
    assert r["status_long"] == "Match Finished"
    assert r["elapsed"] == 90

    assert r["goals_home"] == 2
    assert r["goals_away"] == 1

    # Ensure UTC + tz-aware datetime
    assert r["date"].tzinfo is not None
    assert r["date"].tzinfo == timezone.utc

    # Single fixture envelope doesn't include nested blocks; details should be empty
    assert details_rows == []


