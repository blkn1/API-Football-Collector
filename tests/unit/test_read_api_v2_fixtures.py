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
        dt4 = datetime(2026, 1, 5, 22, 0, tzinfo=timezone.utc)

        # Columns (see fixtures_v2 SQL):
        # id, league_id, league_name, country_name, season, round, date_utc, ts, status_short, status_long,
        # home_team_id, home_team_name, away_team_id, away_team_name, updated_at
        return [
            # League 95: two matches at earliest (18:00), and one later (20:15)
            (1398121, 95, "Segunda Liga", "Portugal", 2025, "Regular Season - 17", dt1, int(dt1.timestamp()), "NS", "Not Started", 229, "Benfica B", 243, "FC Porto B", dt1),
            (1398125, 95, "Segunda Liga", "Portugal", 2025, "Regular Season - 17", dt1, int(dt1.timestamp()), "NS", "Not Started", 702, "Feirense", 806, "Leixões", dt1),
            (1398122, 95, "Segunda Liga", "Portugal", 2025, "Regular Season - 17", dt3, int(dt3.timestamp()), "NS", "Not Started", 810, "Vizela", 4799, "Torreense", dt3),
            # League 39: two kickoff times (19:45 and 22:00)
            (1489123, 39, "Premier League", "England", 2025, "Regular Season - 20", dt2, int(dt2.timestamp()), "NS", "Not Started", 42, "Liverpool", 33, "Manchester United", dt2),
            (1489124, 39, "Premier League", "England", 2025, "Regular Season - 20", dt4, int(dt4.timestamp()), "NS", "Not Started", 40, "Manchester City", 49, "Tottenham", dt2),
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
    # Grouping is per (league_id, kickoff_time). Same league can appear multiple times.
    assert [l["league_id"] for l in leagues] == [95, 39, 95, 39]  # sorted by kickoff time

    # 95 @ 18:00 (two matches)
    l95_early = leagues[0]
    assert l95_early["league_id"] == 95
    assert l95_early["match_count"] == 2
    assert {m["id"] for m in l95_early["matches"]} == {1398121, 1398125}

    # 39 @ 19:45 (one match)
    l39_early = leagues[1]
    assert l39_early["league_id"] == 39
    assert l39_early["match_count"] == 1
    assert l39_early["matches"][0]["id"] == 1489123

    # 95 @ 20:15 (one match)
    l95_late = leagues[2]
    assert l95_late["league_id"] == 95
    assert l95_late["match_count"] == 1
    assert l95_late["matches"][0]["id"] == 1398122

    # 39 @ 22:00 (one match)
    l39_late = leagues[3]
    assert l39_late["league_id"] == 39
    assert l39_late["match_count"] == 1
    assert l39_late["matches"][0]["id"] == 1489124

    assert payload["total_match_count"] == 5  # total matches returned across all kickoff buckets
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


