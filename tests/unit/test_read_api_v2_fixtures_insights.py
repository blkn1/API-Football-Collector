from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

import src.read_api.app as read_api


@pytest.fixture(autouse=True)
def _no_read_api_auth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # Ensure tests don't depend on external env auth.
    monkeypatch.delenv("READ_API_IP_ALLOWLIST", raising=False)
    monkeypatch.delenv("READ_API_BASIC_USER", raising=False)
    monkeypatch.delenv("READ_API_BASIC_PASSWORD", raising=False)


def test_v2_fixtures_insights_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    # tracked-only scope
    monkeypatch.setattr(read_api, "_get_tracked_league_ids", lambda: {39})

    # Make insights deterministic for tests (avoid reading config file).
    monkeypatch.setattr(
        read_api,
        "_fixture_insights_cfg",
        lambda: {
            "last5_n": 5,
            "last10_n": 10,
            "min_matches_for_scores": 1,
            "final_statuses": ["FT", "AET", "PEN"],
            "first_half_max_minute": 45,
            "late_goal_from_minute": 76,
            "weights": {
                "attack": {"goals": 0.5, "corners": 0.3, "offsides": 0.2, "shots_on_goal": 0.0},
                "defense": {
                    "ga_avg": 0.45,
                    "clean_sheet_rate_pct": 0.35,
                    "corners_against_avg": 0.10,
                    "yellow_cards_avg": 0.07,
                    "red_cards_avg": 0.03,
                },
                "form": {"form_points": 0.60, "goal_diff": 0.25, "opponent_strength": 0.15},
                "winning_drive": {"late_goal_rate_pct": 0.45, "win_streak": 0.35, "second_half_goal_diff": 0.20},
            },
            "normalization": {
                "gf_avg": {"min": 0.0, "max": 3.0},
                "corner_kicks_avg": {"min": 0.0, "max": 8.0},
                "offsides_avg": {"min": 0.0, "max": 4.0},
                "shots_on_goal_avg": {"min": 0.0, "max": 7.0},
                "ga_avg": {"min": 0.0, "max": 3.5, "invert": True},
                "clean_sheet_rate_pct": {"min": 0.0, "max": 100.0},
                "corners_against_avg": {"min": 0.0, "max": 8.0, "invert": True},
                "yellow_cards_avg": {"min": 0.0, "max": 4.0, "invert": True},
                "red_cards_avg": {"min": 0.0, "max": 1.0, "invert": True},
                "form_points_last5": {"min": 0.0, "max": 15.0},
                "goal_diff_per_match": {"min": -2.0, "max": 2.0},
                "opponent_strength_points": {"min": 0.0, "max": 15.0},
                "late_goal_rate_pct": {"min": 0.0, "max": 100.0},
                "win_streak": {"min": 0.0, "max": 5.0},
                "second_half_goal_diff_per_match": {"min": -2.0, "max": 2.0},
            },
        },
    )

    kickoff = datetime(2026, 1, 6, 20, 0, tzinfo=timezone.utc)
    updated = datetime(2026, 1, 6, 10, 0, tzinfo=timezone.utc)

    # One NS fixture row (see SQL in fixtures_insights_v2)
    ns_rows = [
        (
            1001,  # f.id
            39,  # league_id
            "Premier League",  # league_name
            "England",  # country_name
            2025,  # season
            "R20",  # round
            kickoff,  # date_utc
            int(kickoff.timestamp()),  # timestamp_utc
            "NS",  # status_short
            "Not Started",  # status_long
            10,  # home_team_id
            "HomeFC",  # home_team_name
            20,  # away_team_id
            "AwayFC",  # away_team_name
            updated,  # updated_at_utc
        )
    ]

    # History rows for contexts (window function output).
    # We only need a couple of completed fixtures to exercise metrics.
    f1_dt = datetime(2026, 1, 1, 20, 0, tzinfo=timezone.utc)
    f2_dt = datetime(2026, 1, 3, 20, 0, tzinfo=timezone.utc)
    hist_rows = [
        # upcoming_fixture_id, league_id, season, team_id, ctx_side, fixture_id, date_utc, home_team_id, away_team_id,
        # goals_home, goals_away, score, updated_at, rn
        (1001, 39, 2025, 10, "home", 2001, f2_dt, 10, 30, 2, 0, {"halftime": {"home": 1, "away": 0}, "fulltime": {"home": 2, "away": 0}}, updated, 1),
        (1001, 39, 2025, 20, "away", 2002, f1_dt, 40, 20, 1, 1, {"halftime": {"home": 0, "away": 0}, "fulltime": {"home": 1, "away": 1}}, updated, 1),
    ]

    # Events: provide a late goal for team 10 in fixture 2001 at 80'
    ev_rows = [
        (2001, 80, 10, "Goal", "Normal Goal"),
    ]

    # Statistics: provide team+opponent stats so corners_against works
    st_rows = [
        (2001, 10, [{"type": "Corner Kicks", "value": 6}, {"type": "Offsides", "value": 2}, {"type": "Shots on Goal", "value": 4}]),
        (2001, 30, [{"type": "Corner Kicks", "value": 3}, {"type": "Offsides", "value": 1}]),
        (2002, 20, [{"type": "Corner Kicks", "value": 5}, {"type": "Offsides", "value": 1}, {"type": "Shots on Goal", "value": 3}]),
        (2002, 40, [{"type": "Corner Kicks", "value": 4}, {"type": "Offsides", "value": 0}]),
    ]

    async def fake_fetchall_async(sql: str, params: tuple):
        if "WHERE f.status_short = 'NS'" in sql and "JOIN core.leagues" in sql:
            return ns_rows
        if "WITH ctx(upcoming_fixture_id" in sql and "ROW_NUMBER()" in sql:
            # final_statuses + last10_n are appended at end
            assert params[-1] == 10
            return hist_rows
        if "FROM core.fixture_events" in sql:
            return ev_rows
        if "FROM core.fixture_statistics" in sql:
            return st_rows
        if "FROM core.team_statistics" in sql and "WHERE (league_id, season, team_id) IN" in sql:
            return []  # optional
        raise AssertionError(f"Unexpected SQL in fake_fetchall_async: {sql}")

    monkeypatch.setattr(read_api, "_fetchall_async", fake_fetchall_async)

    client = TestClient(read_api.app)
    res = client.get("/v2/fixtures/insights?date_from=2026-01-06&date_to=2026-01-06")
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["total_match_count"] == 1
    match = body["leagues"][0]["matches"][0]
    assert match["id"] == 1001
    assert match["status_short"] == "NS"
    assert match["insights"]["league_id"] == 39
    assert match["insights"]["season"] == 2025
    assert match["insights"]["home_team"]["team_id"] == 10
    assert match["insights"]["away_team"]["team_id"] == 20
    assert match["insights"]["home_team"]["selected_context"] == "home"
    assert match["insights"]["away_team"]["selected_context"] == "away"

    # Normalized indices should be present (numbers or null, but object exists)
    home_sel = match["insights"]["home_team"]["selected_indices_0_10"]
    away_sel = match["insights"]["away_team"]["selected_indices_0_10"]
    assert home_sel is not None
    assert away_sel is not None
    assert "attack_strength" in home_sel
    assert "defensive_solidity" in home_sel


def test_v2_fixtures_insights_strict_query_params(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(read_api, "_get_tracked_league_ids", lambda: {39})
    client = TestClient(read_api.app)
    res = client.get("/v2/fixtures/insights?date_from=2026-01-06&date_to=2026-01-06&extra=1")
    assert res.status_code == 400


def test_v2_fixtures_insights_date_range_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(read_api, "_get_tracked_league_ids", lambda: {39})
    client = TestClient(read_api.app)
    res = client.get("/v2/fixtures/insights?date_from=2026-01-07&date_to=2026-01-06")
    assert res.status_code == 400
    assert res.json()["detail"] == "date_to_must_be_gte_date_from"

