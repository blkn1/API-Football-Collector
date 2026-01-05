from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

import src.read_api.app as read_api


@pytest.fixture()
def client() -> TestClient:
    return TestClient(read_api.app)


def test_v2_team_breakdown_aggregates_overall_home_away(monkeypatch: pytest.MonkeyPatch, client: TestClient) -> None:
    team_id = 10
    cutoff = datetime(2026, 1, 5, 23, 59, 59, tzinfo=timezone.utc)

    # Fixtures:
    # - F1: team is HOME, wins 2-0, HT 1-0 (score-based halves)
    # - F2: team is AWAY, draws 1-1, no halftime in score -> fallback to events
    fx_rows = [
        (
            1,
            100,
            "LeagueA",
            2025,
            cutoff,
            "FT",
            10,
            "TeamA",
            20,
            "Opp1",
            2,
            0,
            {"halftime": {"home": 1, "away": 0}, "fulltime": {"home": 2, "away": 0}},
            cutoff,
        ),
        (
            2,
            200,
            "LeagueB",
            2025,
            cutoff,
            "FT",
            30,
            "Opp2",
            10,
            "TeamA",
            1,
            1,
            {"halftime": {"home": None, "away": None}, "fulltime": {"home": 1, "away": 1}},
            cutoff,
        ),
    ]

    # Events for fixture 2: team scores at 10' (1H), concedes at 60' (2H), one yellow at 20', one red at 70'
    ev_rows = [
        (2, 10, 10, "Goal", "Normal Goal"),
        (2, 60, 30, "Goal", "Normal Goal"),
        (2, 20, 10, "Card", "Yellow Card"),
        (2, 70, 10, "Card", "Red Card"),
    ]

    # Statistics: corners/offsides for both fixtures, both teams present
    st_rows = [
        (1, 10, [{"type": "Corner Kicks", "value": 5}, {"type": "Offsides", "value": 2}]),
        (1, 20, [{"type": "Corner Kicks", "value": 3}, {"type": "Offsides", "value": 1}]),
        (2, 10, [{"type": "Corner Kicks", "value": 4}, {"type": "Offsides", "value": 0}]),
        (2, 30, [{"type": "Corner Kicks", "value": 6}, {"type": "Offsides", "value": 2}]),
    ]

    # Opponent form rows (LeagueA opp=20, LeagueB opp=30)
    form_rows = [
        (100, 2025, 20, "WWDLW"),  # points last5 = 3+3+1+0+3 = 10
        (200, 2025, 30, "LLLLW"),  # points last5 = 3
    ]

    async def fake_fetchall_async(sql: str, params: tuple):
        if "FROM core.fixtures f" in sql and "f.score" in sql:
            # Ensure cutoff and limit are passed (sanity)
            assert params[0] == team_id
            assert params[1] == team_id
            assert params[4] == 2
            return fx_rows
        if "FROM core.fixture_events" in sql:
            assert params[0] == [1, 2]
            return ev_rows
        if "FROM core.fixture_statistics" in sql:
            assert params[0] == [1, 2]
            return st_rows
        if "FROM core.team_statistics ts" in sql:
            return form_rows
        raise AssertionError(f"Unexpected SQL in fake_fetchall_async: {sql}")

    monkeypatch.setattr(read_api, "_fetchall_async", fake_fetchall_async)
    monkeypatch.setattr(read_api, "_utc_end_of_day", lambda d: cutoff)

    res = client.get(f"/v2/teams/{team_id}/breakdown", params={"last_n": 2, "as_of_date": "2026-01-05"})
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["team_id"] == team_id
    assert body["window"]["last_n"] == 2
    assert body["window"]["played"] == 2

    overall = body["overall"]
    home = body["home"]
    away = body["away"]

    # Overall goals: (2-0) + (1-1) => gf=3 ga=1 total=4
    assert overall["played"] == 2
    assert overall["goals"]["gf"] == 3
    assert overall["goals"]["ga"] == 1
    assert overall["goals"]["total_goals"] == 4
    assert overall["goals"]["over_1_5_rate"] == 1.0  # both matches have >=2 total goals
    assert overall["goals"]["over_2_5_rate"] == 0.0  # no match has >=3 total goals (2-0 and 1-1)

    # Half goals:
    # Fixture1 from score: 1H (1-0), 2H (1-0)
    # Fixture2 from events: 1H (1-0), 2H (0-1)
    assert overall["goals_by_half"]["first_half"]["gf"] == 2
    assert overall["goals_by_half"]["first_half"]["ga"] == 0
    assert overall["goals_by_half"]["second_half"]["gf"] == 1
    assert overall["goals_by_half"]["second_half"]["ga"] == 1

    # Cards by half: only fixture2 has cards (yellow 1H, red 2H)
    assert overall["cards_by_half"]["first_half"]["yellow_for"] == 1
    assert overall["cards_by_half"]["second_half"]["red_for"] == 1

    # Corners/offsides totals (for/against):
    # corners_for = 5+4=9, corners_against = 3+6=9
    assert overall["corners_totals"]["for"] == 9
    assert overall["corners_totals"]["against"] == 9
    # offsides_for = 2+0=2, offsides_against = 1+2=3
    assert overall["offsides_totals"]["for"] == 2
    assert overall["offsides_totals"]["against"] == 3

    # Home vs away split
    assert home["played"] == 1
    assert home["goals"]["gf"] == 2
    assert home["goals"]["ga"] == 0
    assert away["played"] == 1
    assert away["goals"]["gf"] == 1
    assert away["goals"]["ga"] == 1

    # Opponent strength averages
    # overall avg = (10 + 3) / 2 = 6.5
    assert overall["opponent_strength"]["matches_available"] == 2
    assert overall["opponent_strength"]["avg_points_last5"] == 6.5
