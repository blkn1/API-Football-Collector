from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from collector.api_client import APIResult
from scripts import daily_sync as daily_sync_mod
from scripts.daily_sync import sync_daily_fixtures


@dataclass
class _Quota:
    daily_remaining: int | None = None
    minute_remaining: int | None = None


class _FakeLimiter:
    def __init__(self) -> None:
        self.tokens = 0
        self.quota = _Quota()

    def acquire_token(self) -> None:
        self.tokens += 1

    def update_from_headers(self, headers: dict[str, str]) -> None:
        try:
            self.quota.daily_remaining = int(headers.get("x-ratelimit-requests-remaining")) if headers.get("x-ratelimit-requests-remaining") else None
        except Exception:
            self.quota.daily_remaining = None
        try:
            self.quota.minute_remaining = int(headers.get("X-RateLimit-Remaining")) if headers.get("X-RateLimit-Remaining") else None
        except Exception:
            self.quota.minute_remaining = None


class _FakePagedClient:
    def __init__(self, pages: dict[int, dict[str, Any]]) -> None:
        self.pages = pages
        self.calls: list[dict[str, Any]] = []

    async def get(self, endpoint: str, params: dict | None = None) -> APIResult:
        assert endpoint == "/fixtures"
        p = params or {}
        self.calls.append(p)
        page = int(p.get("page", 1))
        env = self.pages[page]
        headers = {"x-ratelimit-requests-remaining": "7400", "X-RateLimit-Remaining": "299"}
        return APIResult(status_code=200, data=env, headers=headers)

    async def aclose(self) -> None:
        return None


def _fixture_item(*, fixture_id: int, league_id: int, season: int, home_id: int, away_id: int) -> dict[str, Any]:
    return {
        "fixture": {
            "id": fixture_id,
            "date": "2025-12-18T20:30:00+00:00",
            "timestamp": 1766099400,
            "referee": None,
            "timezone": "UTC",
            "venue": {"id": 1, "name": "X", "city": "Y"},
            "status": {"long": "Not Started", "short": "NS", "elapsed": None},
        },
        "league": {"id": league_id, "season": season, "round": None},
        "teams": {"home": {"id": home_id}, "away": {"id": away_id}},
        "goals": {"home": None, "away": None},
        "score": {},
        # Include at least one nested block so fixture_details row is created
        "events": [{}],
    }


@pytest.mark.asyncio
async def test_daily_sync_global_by_date_paging_groups_and_dedups(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg_path = tmp_path / "daily.yaml"
    cfg_path.write_text(
        "\n".join(
            [
                "fixtures_fetch_mode: global_by_date",
                "season: 2025",
                "tracked_leagues:",
                "  - id: 39",
                "    name: Premier League",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    # page1: fixture 100 (league 203), fixture 101 (league 140)
    # page2: fixture 100 again (duplicate across pages), fixture 102 (league 140)
    page1_items = [
        _fixture_item(fixture_id=100, league_id=203, season=2025, home_id=10, away_id=11),
        _fixture_item(fixture_id=101, league_id=140, season=2025, home_id=20, away_id=21),
    ]
    page2_items = [
        _fixture_item(fixture_id=100, league_id=203, season=2025, home_id=10, away_id=11),
        _fixture_item(fixture_id=102, league_id=140, season=2025, home_id=22, away_id=23),
    ]

    client = _FakePagedClient(
        pages={
            1: {"response": page1_items, "paging": {"current": 1, "total": 2}, "results": len(page1_items), "errors": []},
            2: {"response": page2_items, "paging": {"current": 2, "total": 2}, "results": len(page2_items), "errors": []},
        }
    )
    limiter = _FakeLimiter()

    raw_calls: list[dict[str, Any]] = []
    core_calls: dict[str, list[list[dict[str, Any]]]] = {"core.fixtures": [], "core.fixture_details": []}
    dep_calls: list[tuple[int, int, int]] = []

    def _fake_upsert_raw(*, endpoint: str, requested_params: dict[str, Any], status_code: int, response_headers: dict[str, Any], body: dict[str, Any]) -> None:
        raw_calls.append({"endpoint": endpoint, "requested_params": requested_params, "status_code": status_code})

    def _fake_upsert_core(*, full_table_name: str, rows: list[dict[str, Any]], conflict_cols: list[str], update_cols: list[str], conn=None) -> None:
        if full_table_name in core_calls:
            core_calls[full_table_name].append(rows)

    async def _fake_ensure_fixtures_dependencies(*, league_id: int, season: int | None, fixtures_envelope: dict[str, Any], client, limiter, log_venues: bool = True) -> None:
        dep_calls.append((int(league_id), int(season or 0), len(fixtures_envelope.get("response") or [])))

    async def _fake_backfill_missing_venues_for_fixtures(*, venue_ids: list[int], client, limiter, dry_run: bool, max_to_fetch: int) -> int:
        return 0

    class _FakeCovCalc:
        def calculate_fixtures_coverage(self, league_id: int, season: int) -> dict[str, Any]:
            return {"league_id": league_id, "season": season, "endpoint": "/fixtures", "overall_coverage": None}

    monkeypatch.setattr(daily_sync_mod, "upsert_raw", _fake_upsert_raw)
    monkeypatch.setattr(daily_sync_mod, "upsert_core", _fake_upsert_core)
    monkeypatch.setattr(daily_sync_mod, "upsert_mart_coverage", lambda *args, **kwargs: None)
    monkeypatch.setattr(daily_sync_mod, "CoverageCalculator", _FakeCovCalc)
    monkeypatch.setattr(daily_sync_mod, "backfill_missing_venues_for_fixtures", _fake_backfill_missing_venues_for_fixtures)
    monkeypatch.setattr(daily_sync_mod, "ensure_fixtures_dependencies", _fake_ensure_fixtures_dependencies)
    monkeypatch.setattr(daily_sync_mod, "_refresh_mart_views", lambda conn: None)
    monkeypatch.setattr(daily_sync_mod, "_count_existing", lambda conn, table, id_col, ids: 0)

    # Dummy transaction context manager
    class _Tx:
        def __enter__(self):
            return object()

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(daily_sync_mod, "get_transaction", lambda: _Tx())

    summary = await sync_daily_fixtures(
        target_date_utc="2025-12-18",
        dry_run=False,
        config_path=cfg_path,
        client=client,
        limiter=limiter,
    )

    # Two page requests were made
    assert summary.api_requests == 2
    assert len(client.calls) == 2
    assert limiter.tokens == 2

    # RAW stored per page with (date,page)
    assert [c["requested_params"] for c in raw_calls] == [
        {"date": "2025-12-18", "timezone": "UTC"},
        {"date": "2025-12-18", "timezone": "UTC", "page": 2},
    ]

    # Dedup across pages: 3 unique fixture ids
    assert summary.total_fixtures == 3

    # Dependencies called per (league_id, season) group
    assert sorted(dep_calls) == sorted([(140, 2025, 2), (203, 2025, 1)])

    # CORE upserts grouped (2 groups for fixtures; total rows == 3)
    assert len(core_calls["core.fixtures"]) == 2
    assert sum(len(batch) for batch in core_calls["core.fixtures"]) == 3



