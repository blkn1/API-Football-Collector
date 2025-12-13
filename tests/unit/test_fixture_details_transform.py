from __future__ import annotations

import json
from pathlib import Path

from transforms.fixture_details import transform_fixture_details
from transforms.fixtures import transform_fixtures


def test_transform_fixture_details_from_fixtures_full() -> None:
    p = (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "api_responses"
        / "fixtures_full.json"
    )
    envelope = json.loads(p.read_text(encoding="utf-8"))
    item = envelope["response"][0]

    details = transform_fixture_details(item)
    assert details is not None
    assert details["fixture_id"] == 1234567
    assert isinstance(details["events"], list)
    assert isinstance(details["lineups"], list)
    assert isinstance(details["statistics"], list)
    assert isinstance(details["players"], list)

    # Ensure nested naive ISO datetime is normalized to UTC (+00:00)
    team_update = details["players"][0]["team"]["update"]
    assert team_update.endswith("+00:00")


def test_transform_fixtures_returns_details_list() -> None:
    p = (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "api_responses"
        / "fixtures_full.json"
    )
    envelope = json.loads(p.read_text(encoding="utf-8"))

    fixtures_rows, details_rows = transform_fixtures(envelope)
    assert len(fixtures_rows) == 1
    assert len(details_rows) == 1
    assert details_rows[0]["fixture_id"] == fixtures_rows[0]["id"]


