from __future__ import annotations

from pathlib import Path

import pytest

from scripts.daily_sync import sync_daily_fixtures


class _FailIfCalledClient:
    async def get(self, endpoint: str, params=None):
        raise AssertionError("API should not be called in --dry-run")

    async def aclose(self) -> None:
        return None


@pytest.mark.asyncio
async def test_daily_sync_dry_run_makes_no_api_calls(tmp_path: Path) -> None:
    cfg_path = tmp_path / "daily.yaml"
    cfg_path.write_text(
        "season: 2024\ntracked_leagues:\n  - id: 39\n    name: Premier League\n",
        encoding="utf-8",
    )

    summary = await sync_daily_fixtures(
        target_date_utc="2024-12-12",
        dry_run=True,
        config_path=cfg_path,
        client=_FailIfCalledClient(),
    )

    assert summary.api_requests == 0
    assert summary.total_fixtures == 0


