from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_ensure_teams_exist_for_league_cache_completed_but_missing_triggers_refresh(monkeypatch) -> None:
    """
    Regression: team_bootstrap_progress.completed=True used to short-circuit even if core.teams
    was missing some team_ids referenced by standings/fixtures.
    """
    from src.utils import dependencies as dep

    # Pretend cache says completed.
    def fake_query_scalar(_q: str, _params=None):
        return True

    monkeypatch.setattr(dep, "query_scalar", fake_query_scalar)

    # First check: missing teams exist in CORE -> should trigger refresh.
    calls = {"fetch": 0}

    def fake_missing(_team_ids: set[int]) -> set[int]:
        return {18270}

    async def fake_fetch_and_store(*, client, limiter, endpoint: str, params: dict):
        calls["fetch"] += 1

        class _Res:
            status_code = 200
            headers = {}
            data = {"errors": [], "response": []}

        return _Res()

    monkeypatch.setattr(dep, "get_missing_team_ids_in_core", fake_missing)
    monkeypatch.setattr(dep, "_fetch_and_store", fake_fetch_and_store)
    monkeypatch.setattr(dep, "transform_venues_from_teams", lambda _env: [])
    monkeypatch.setattr(dep, "transform_teams", lambda _env: [])
    monkeypatch.setattr(dep, "upsert_core", lambda **_kwargs: None)
    monkeypatch.setattr(dep, "upsert_raw", lambda **_kwargs: 0)

    class _Client:
        async def get(self, endpoint: str, params=None):
            raise AssertionError("should not be called directly")

    class _Limiter:
        def acquire_token(self):
            return None

        def update_from_headers(self, _headers):
            return None

    await dep.ensure_teams_exist_for_league(
        league_id=705,
        season=2024,
        team_ids={18270, 24739},
        client=_Client(),
        limiter=_Limiter(),
    )

    assert calls["fetch"] == 1


@pytest.mark.asyncio
async def test_ensure_teams_exist_for_league_cache_completed_and_no_missing_skips_refresh(monkeypatch) -> None:
    from src.utils import dependencies as dep

    # Pretend cache says completed.
    monkeypatch.setattr(dep, "query_scalar", lambda *_args, **_kwargs: True)

    # No missing => should short-circuit and never call /teams
    monkeypatch.setattr(dep, "get_missing_team_ids_in_core", lambda _ids: set())

    calls = {"fetch": 0}

    async def fake_fetch_and_store(*, client, limiter, endpoint: str, params: dict):
        calls["fetch"] += 1
        raise AssertionError("should not refresh when nothing is missing")

    monkeypatch.setattr(dep, "_fetch_and_store", fake_fetch_and_store)

    class _Client:
        async def get(self, endpoint: str, params=None):
            raise AssertionError("should not be called directly")

    class _Limiter:
        def acquire_token(self):
            return None

        def update_from_headers(self, _headers):
            return None

    await dep.ensure_teams_exist_for_league(
        league_id=39,
        season=2024,
        team_ids={33, 34},
        client=_Client(),
        limiter=_Limiter(),
    )

    assert calls["fetch"] == 0


