from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.mark.asyncio
async def test_get_backfill_progress_shapes_and_filters(monkeypatch):
    from src.mcp import server

    captured = {"calls": []}

    async def _fake_fetchall(sql_text: str, params: tuple):
        captured["calls"].append((sql_text, params))
        if "GROUP BY job_id" in sql_text:
            # summary rows
            return [
                ("fixtures_backfill_league_season", 10, 6, 4, None),
            ]
        # task rows
        return [
            ("fixtures_backfill_league_season", 39, 2024, 3, False, None, None, None),
            ("fixtures_backfill_league_season", 78, 2024, 7, True, "last_error", None, None),
        ]

    monkeypatch.setattr(server, "_db_fetchall_async", _fake_fetchall)

    out = await server.get_backfill_progress(job_id="fixtures_backfill_league_season", season=2024, include_completed=False, limit=50)
    assert out["ok"] is True
    assert out["filters"]["job_id"] == "fixtures_backfill_league_season"
    assert out["filters"]["season"] == 2024
    assert out["filters"]["include_completed"] is False
    assert out["filters"]["limit"] == 50

    assert isinstance(out["summaries"], list) and out["summaries"][0]["job_id"] == "fixtures_backfill_league_season"
    assert isinstance(out["tasks"], list) and out["tasks"][0]["league_id"] == 39

    # Ensure include_completed is applied via SQL param (5th param in list query)
    assert len(captured["calls"]) == 2
    _sql1, params1 = captured["calls"][0]
    _sql2, params2 = captured["calls"][1]
    assert params1 == ("fixtures_backfill_league_season", "fixtures_backfill_league_season", 2024, 2024)
    assert params2[0:4] == ("fixtures_backfill_league_season", "fixtures_backfill_league_season", 2024, 2024)
    assert params2[4] is False


@pytest.mark.asyncio
async def test_get_raw_error_summary_shapes(monkeypatch):
    from src.mcp import server

    captured = {"fetchone": None, "fetchall": None}

    async def _fake_fetchone(sql_text: str, params: tuple):
        captured["fetchone"] = (sql_text, params)
        return (100, 95, 4, 1, 2, None)

    async def _fake_fetchall(sql_text: str, params: tuple):
        captured["fetchall"] = (sql_text, params)
        return [
            ("/fixtures", 60, 59, 1, 0, None),
            ("/standings", 40, 36, 4, 2, None),
        ]

    monkeypatch.setattr(server, "_db_fetchone_async", _fake_fetchone)
    monkeypatch.setattr(server, "_db_fetchall_async", _fake_fetchall)

    out = await server.get_raw_error_summary(since_minutes=120, endpoint=None, top_endpoints_limit=10)
    assert out["ok"] is True
    assert out["window"]["since_minutes"] == 120
    assert out["summary"]["total_requests"] == 100
    assert len(out["by_endpoint"]) == 2

    # Ensure parameterization (mins, endpoint, endpoint, limit)
    assert captured["fetchone"][1] == (120, None, None)
    assert captured["fetchall"][1] == (120, None, None, 10)


@pytest.mark.asyncio
async def test_get_recent_log_errors_reads_jsonl(monkeypatch, tmp_path: Path):
    from src.mcp import server

    log_file = tmp_path / "collector.jsonl"
    log_file.write_text(
        "\n".join(
            [
                json.dumps({"timestamp": "2025-12-12T00:00:00+00:00", "level": "info", "script": "daily_sync", "event": "daily_sync_started"}),
                json.dumps({"timestamp": "2025-12-12T00:01:00+00:00", "level": "error", "script": "daily_sync", "event": "daily_sync_failed", "err": "boom"}),
                json.dumps({"timestamp": "2025-12-12T00:02:00+00:00", "level": "info", "script": "standings_sync", "event": "standings_sync_complete"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("COLLECTOR_LOG_FILE", str(log_file))

    out = await server.get_recent_log_errors(job_name="daily_sync", limit=10)
    assert out["ok"] is True
    assert out["log_file"] == str(log_file)
    assert len(out["errors"]) == 1
    assert out["errors"][0]["job_name"] == "daily_sync"
    assert out["errors"][0]["event"] == "daily_sync_failed"
