from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import src.read_api.app as read_api


@pytest.fixture(autouse=True)
def _no_read_api_auth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # Ensure tests don't depend on external env auth.
    monkeypatch.delenv("READ_API_IP_ALLOWLIST", raising=False)
    monkeypatch.delenv("READ_API_BASIC_USER", raising=False)
    monkeypatch.delenv("READ_API_BASIC_PASSWORD", raising=False)


def _assert_unknown_query_params(res, *, unknown: list[str], allowed_contains: list[str]) -> None:
    assert res.status_code == 400
    payload = res.json()
    assert "detail" in payload
    assert isinstance(payload["detail"], dict)
    assert payload["detail"]["error"] == "unknown_query_params"
    assert payload["detail"]["unknown"] == sorted(unknown)
    # allowed is a sorted list; just sanity-check a few expected items
    for k in allowed_contains:
        assert k in payload["detail"]["allowed"]


def test_v1_fixtures_rejects_unknown_query_params() -> None:
    client = TestClient(read_api.app)
    res = client.get("/v1/fixtures?date=2025-12-24&date_from=2025-12-01")
    _assert_unknown_query_params(res, unknown=["date_from"], allowed_contains=["date", "league_id", "status", "limit"])


def test_read_fixtures_rejects_unknown_query_params() -> None:
    client = TestClient(read_api.app)
    res = client.get("/read/fixtures?league_id=39&season=2025&foo=bar")
    _assert_unknown_query_params(
        res,
        unknown=["foo"],
        allowed_contains=["league_id", "country", "season", "date_from", "date_to", "team_id", "status", "limit", "offset"],
    )


def test_team_metrics_rejects_unknown_query_params() -> None:
    client = TestClient(read_api.app)
    res = client.get("/v1/teams/228/metrics?last_n=20&status=FT")
    _assert_unknown_query_params(res, unknown=["status"], allowed_contains=["last_n", "as_of_date"])


