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


def test_read_h2h_returns_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_fetchall_async(_sql: str, _params: tuple) -> list[tuple]:
        # Columns expected by /read/h2h:
        # id, league_id, season, date, status_short,
        # home_team_id, home_team_name, away_team_id, away_team_name,
        # goals_home, goals_away, updated_at
        now = datetime(2025, 12, 21, tzinfo=timezone.utc)
        return [
            (101, 39, 2025, now, "FT", 1, "A", 2, "B", 2, 0, now),
            (102, 39, 2025, now, "FT", 2, "B", 1, "A", 1, 1, now),
        ]

    monkeypatch.setattr(read_api, "_fetchall_async", fake_fetchall_async)

    client = TestClient(read_api.app)
    res = client.get("/read/h2h?team1_id=1&team2_id=2&limit=20")
    assert res.status_code == 200
    payload = res.json()
    assert payload["ok"] is True
    assert payload["summary_team1"]["played"] == 2
    assert payload["summary_team1"]["wins"] == 1
    assert payload["summary_team1"]["draws"] == 1
    assert payload["summary_team1"]["losses"] == 0
    assert payload["summary_team1"]["goals_for"] == 3
    assert payload["summary_team1"]["goals_against"] == 1
    assert len(payload["items"]) == 2


def test_read_coverage_requires_season_when_no_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("READ_API_DEFAULT_SEASON", raising=False)
    client = TestClient(read_api.app)
    res = client.get("/read/coverage")
    assert res.status_code == 400
    assert res.json()["detail"] == "season_required"


def test_read_coverage_returns_items(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("READ_API_DEFAULT_SEASON", "2025")

    def fake_resolve_league_ids(*, league_id: int | None, country: str | None, season: int | None) -> list[int]:
        assert league_id == 39
        return [39]

    async def fake_fetchall_async(_sql: str, _params: tuple) -> list[tuple]:
        now = datetime(2025, 12, 21, tzinfo=timezone.utc)
        return [
            (39, "Premier League", 2025, "/players/topscorers", 1, 1, 100.0, now, 10, 99.0, 5, 20, 100.0, 99.5, now),
        ]

    monkeypatch.setattr(read_api, "_resolve_league_ids", fake_resolve_league_ids)
    monkeypatch.setattr(read_api, "_fetchall_async", fake_fetchall_async)

    client = TestClient(read_api.app)
    res = client.get("/read/coverage?league_id=39")
    assert res.status_code == 200
    payload = res.json()
    assert payload["ok"] is True
    assert payload["filters"]["season"] == 2025
    assert len(payload["items"]) == 1
    assert payload["items"][0]["league_id"] == 39


def test_read_top_scorers(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_fetchall_async(_sql: str, params: tuple) -> list[tuple]:
        # league_id, season, limit, offset
        assert params[0] == 39
        assert params[1] == 2025
        now = datetime(2025, 12, 21, tzinfo=timezone.utc)
        return [
            (39, "Premier League", 2025, 9001, 1, 50, "TeamX", 12, 3, {"player": {"id": 9001}}, now),
        ]

    monkeypatch.setattr(read_api, "_fetchall_async", fake_fetchall_async)

    client = TestClient(read_api.app)
    res = client.get("/read/top_scorers?league_id=39&season=2025")
    assert res.status_code == 200
    payload = res.json()
    assert payload["ok"] is True
    assert payload["league_id"] == 39
    assert payload["season"] == 2025
    assert payload["items"][0]["player_id"] == 9001


def test_read_team_statistics(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_fetchall_async(_sql: str, params: tuple) -> list[tuple]:
        # league_id, season, [team_id?], limit, offset
        assert params[0] == 39
        assert params[1] == 2025
        now = datetime(2025, 12, 21, tzinfo=timezone.utc)
        return [
            (39, "Premier League", 2025, 50, "TeamX", "WWDLW", {"form": "WWDLW"}, now),
        ]

    monkeypatch.setattr(read_api, "_fetchall_async", fake_fetchall_async)
    monkeypatch.setenv("READ_API_DEFAULT_SEASON", "2025")

    client = TestClient(read_api.app)
    res = client.get("/read/team_statistics?league_id=39")
    assert res.status_code == 200
    payload = res.json()
    assert payload["ok"] is True
    assert payload["items"][0]["team_id"] == 50


