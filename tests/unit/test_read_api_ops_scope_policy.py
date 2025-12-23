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


def test_ops_scope_policy_proxies_mcp(monkeypatch: pytest.MonkeyPatch) -> None:
    import src.mcp.server as mcp_server

    async def fake_get_scope_policy(*, league_id: int, season: int | None = None) -> dict:
        assert league_id == 206
        assert season == 2025
        return {
            "ok": True,
            "league_id": league_id,
            "season": season,
            "league_type": "Cup",
            "decisions": [{"endpoint": "/standings", "in_scope": False, "reason": "override_disabled", "policy_version": 1}],
            "ts_utc": "2025-12-23T00:00:00+00:00",
        }

    monkeypatch.setattr(mcp_server, "get_scope_policy", fake_get_scope_policy)

    client = TestClient(read_api.app)
    res = client.get("/ops/api/scope_policy?league_id=206&season=2025")
    assert res.status_code == 200
    payload = res.json()
    assert payload["ok"] is True
    assert payload["league_id"] == 206
    assert payload["season"] == 2025
    assert payload["decisions"][0]["endpoint"] == "/standings"


