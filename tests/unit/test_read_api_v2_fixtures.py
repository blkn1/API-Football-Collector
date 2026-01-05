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


def test_v2_fixtures_groups_by_league_selects_earliest_and_sorts(monkeypatch: pytest.MonkeyPatch) -> None:
    # tracked-only scope
    monkeypatch.setattr(read_api, "_get_tracked_league_ids", lambda: {39, 95})

    async def fake_fetchall_async(_sql: str, params: tuple) -> list[tuple]:
        # params: (tracked_ids, dt_from, dt_to)
        tracked_ids = set(params[0])
        assert tracked_ids == {39, 95}

        dt1 = datetime(2026, 1, 5, 18, 0, tzinfo=timezone.utc)
        dt2 = datetime(2026, 1, 5, 19, 45, tzinfo=timezone.utc)
        dt3 = datetime(2026, 1, 5, 20, 15, tzinfo=timezone.utc)

        # Columns (see fixtures_v2 SQL):
        # id, league_id, league_name, country_name, season, round, date_utc, ts, status_short, status_long,
        # home_team_id, home_team_name, away_team_id, away_team_name, updated_at
        return [
            # League 95: two matches at earliest (18:00), and one later (20:15)
            (1398121, 95, "Segunda Liga", "Portugal", 2025, "Regular Season - 17", dt1, int(dt1.timestamp()), "NS", "Not Started", 229, "Benfica B", 243, "FC Porto B", dt1),
            (1398125, 95, "Segunda Liga", "Portugal", 2025, "Regular Season - 17", dt1, int(dt1.timestamp()), "NS", "Not Started", 702, "Feirense", 806, "Leixões", dt1),
            (1398122, 95, "Segunda Liga", "Portugal", 2025, "Regular Season - 17", dt3, int(dt3.timestamp()), "NS", "Not Started", 810, "Vizela", 4799, "Torreense", dt3),
            # League 39: one earliest (19:45), one later (22:00) -> only earliest should be returned
            (1489123, 39, "Premier League", "England", 2025, "Regular Season - 20", dt2, int(dt2.timestamp()), "NS", "Not Started", 42, "Liverpool", 33, "Manchester United", dt2),
            (1489124, 39, "Premier League", "England", 2025, "Regular Season - 20", datetime(2026, 1, 5, 22, 0, tzinfo=timezone.utc), 1762318800, "NS", "Not Started", 40, "Manchester City", 49, "Tottenham", dt2),
            # Non-tracked league: should be dropped even if returned by DB (safety net)
            (999, 9999, "Bangladesh League", "Bangladesh", 2025, "R1", dt1, int(dt1.timestamp()), "NS", "Not Started", 1, "A", 2, "B", dt1),
            # Non-NS status: should be dropped (safety net)
            (888, 39, "Premier League", "England", 2025, "R", dt1, int(dt1.timestamp()), "FT", "Match Finished", 1, "A", 2, "B", dt1),
        ]

    monkeypatch.setattr(read_api, "_fetchall_async", fake_fetchall_async)

    client = TestClient(read_api.app)
    res = client.get("/v2/fixtures?date_from=2026-01-05&date_to=2026-01-05")
    assert res.status_code == 200
    payload = res.json()

    assert payload["ok"] is True
    assert payload["date_range"] == {"from": "2026-01-05", "to": "2026-01-05"}

    leagues = payload["leagues"]
    assert [l["league_id"] for l in leagues] == [95, 39]  # sorted by earliest kickoff (18:00 then 19:45)

    l95 = leagues[0]
    assert l95["league_id"] == 95
    assert l95["match_count"] == 3  # total NS matches for league in window
    assert l95["has_matches"] is True
    assert len(l95["matches"]) == 2  # only earliest kickoff time, tie included
    assert {m["id"] for m in l95["matches"]} == {1398121, 1398125}

    l39 = leagues[1]
    assert l39["league_id"] == 39
    assert l39["match_count"] == 2
    assert len(l39["matches"]) == 1
    assert l39["matches"][0]["id"] == 1489123

    assert payload["total_match_count"] == 3  # sum of matches.length across leagues (2 + 1)
    assert payload["updated_at_utc"]  # present


def test_v2_fixtures_invalid_date_format(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(read_api, "_get_tracked_league_ids", lambda: {39})
    client = TestClient(read_api.app)
    res = client.get("/v2/fixtures?date_from=2026/01/05&date_to=2026-01-05")
    assert res.status_code == 400


def test_v2_fixtures_date_range_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(read_api, "_get_tracked_league_ids", lambda: {39})
    client = TestClient(read_api.app)
    res = client.get("/v2/fixtures?date_from=2026-01-06&date_to=2026-01-05")
    assert res.status_code == 400
    assert res.json()["detail"] == "date_to_must_be_gte_date_from"


def test_v2_fixtures_empty_when_no_tracked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(read_api, "_get_tracked_league_ids", lambda: set())
    client = TestClient(read_api.app)
    res = client.get("/v2/fixtures?date_from=2026-01-05&date_to=2026-01-05")
    assert res.status_code == 200
    payload = res.json()
    assert payload["ok"] is True
    assert payload["leagues"] == []
    assert payload["total_match_count"] == 0


