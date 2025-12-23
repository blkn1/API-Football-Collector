from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.mark.asyncio
async def test_get_rate_limit_status_no_rows(monkeypatch):
    from src.mcp import server

    async def _fake_fetchone(_sql: str, _params: tuple):
        return None

    monkeypatch.setattr(server, "_db_fetchone_async", _fake_fetchone)

    out = await server.get_rate_limit_status()
    assert out["ok"] is True
    assert out["daily_remaining"] is None
    assert out["minute_remaining"] is None


@pytest.mark.asyncio
async def test_get_rate_limit_status_from_headers(monkeypatch):
    from src.mcp import server

    async def _fake_fetchone(_sql: str, _params: tuple):
        # observed_at, daily_remaining, minute_remaining
        from datetime import datetime, timezone

        return (datetime(2025, 1, 1, tzinfo=timezone.utc), "7000", "245")

    monkeypatch.setattr(server, "_db_fetchone_async", _fake_fetchone)

    out = await server.get_rate_limit_status()
    assert out["ok"] is True
    assert out["daily_remaining"] == 7000
    assert out["minute_remaining"] == 245
    assert out["observed_at_utc"].startswith("2025-01-01")


@pytest.mark.asyncio
async def test_query_fixtures_filters_and_shape(monkeypatch):
    from src.mcp import server

    captured = {}

    async def _fake_fetchall(sql_text: str, params: tuple):
        captured["sql"] = sql_text
        captured["params"] = params
        from datetime import datetime, timezone

        return [
            (
                1,
                78,
                2024,
                datetime(2025, 12, 12, 20, 0, tzinfo=timezone.utc),
                "FT",
                "Bayern",
                "Dortmund",
                2,
                1,
                datetime(2025, 12, 12, 22, 0, tzinfo=timezone.utc),
            )
        ]

    monkeypatch.setattr(server, "_db_fetchall_async", _fake_fetchall)

    out = await server.query_fixtures(league_id=78, date="2025-12-12", status="FT", limit=10)
    assert out["ok"] is True
    assert isinstance(out["items"], list) and len(out["items"]) == 1
    assert out["items"][0]["league_id"] == 78
    assert out["items"][0]["status"] == "FT"
    assert out["items"][0]["home_team"] == "Bayern"
    assert "LIMIT %s" in captured["sql"]
    assert captured["params"][-1] == 10


@pytest.mark.asyncio
async def test_get_coverage_status_specific_league(monkeypatch, tmp_path: Path):
    from src.mcp import server

    # Make season come from config (config-driven) for deterministic test.
    cfg_dir = tmp_path / "config" / "jobs"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "daily.yaml").write_text("season: 2024\ntracked_leagues:\n  - id: 78\n    name: Bundesliga\n", encoding="utf-8")
    monkeypatch.setattr(server, "PROJECT_ROOT", tmp_path)

    captured = {}

    async def _fake_fetchall(sql_text: str, params: tuple):
        captured["sql"] = sql_text
        captured["params"] = params
        from datetime import datetime, timezone

        return [
            (
                "Bundesliga",
                78,
                2024,
                "/fixtures",
                90.0,
                80.0,
                100.0,
                90.0,
                None,
                0,
                datetime(2025, 12, 12, tzinfo=timezone.utc),
                {"no_matches_scheduled": False},
            ),
        ]

    monkeypatch.setattr(server, "_db_fetchall_async", _fake_fetchall)

    out = await server.get_coverage_status(league_id=78)
    assert out["ok"] is True
    assert out["season"] == 2024
    assert out["coverage"][0]["league_id"] == 78
    assert out["coverage"][0]["flags"]["no_matches_scheduled"] is False
    assert "AND c.league_id = %s" in captured["sql"]
    assert captured["params"] == (2024, 78)


@pytest.mark.asyncio
async def test_get_coverage_status_tracked_only_filters(monkeypatch, tmp_path: Path):
    from src.mcp import server

    cfg_dir = tmp_path / "config" / "jobs"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "daily.yaml").write_text(
        "season: 2024\ntracked_leagues:\n  - id: 78\n    name: Bundesliga\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(server, "PROJECT_ROOT", tmp_path)

    async def _fake_fetchall(sql_text: str, params: tuple):
        from datetime import datetime, timezone

        # With tracked_only=True, SQL should be using ANY(tracked_ids) and the DB would only return tracked rows.
        assert "ANY(%s)" in sql_text
        assert params[0] == 2024
        assert params[1] == [78]
        return [
            (
                "Bundesliga",
                78,
                2024,
                "/fixtures",
                90.0,
                80.0,
                100.0,
                90.0,
                None,
                0,
                datetime(2025, 12, 12, tzinfo=timezone.utc),
                {"no_matches_scheduled": False},
            ),
        ]

    monkeypatch.setattr(server, "_db_fetchall_async", _fake_fetchall)

    out = await server.get_coverage_status()
    assert out["ok"] is True
    assert [x["league_id"] for x in out["coverage"]] == [78]


@pytest.mark.asyncio
async def test_list_tracked_leagues(monkeypatch, tmp_path: Path):
    from src.mcp import server

    cfg = tmp_path / "daily.yaml"
    cfg.write_text(
        "season: 2024\ntracked_leagues:\n  - id: 39\n    name: Premier League\n  - id: 78\n    name: Bundesliga\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("API_FOOTBALL_DAILY_CONFIG", str(cfg))

    out = await server.list_tracked_leagues()
    assert out["ok"] is True
    assert [x["id"] for x in out["tracked_leagues"]] == [39, 78]


@pytest.mark.asyncio
async def test_get_job_status_merges_logs_and_config(monkeypatch, tmp_path: Path):
    from src.mcp import server

    # Isolate PROJECT_ROOT for this test
    monkeypatch.setattr(server, "PROJECT_ROOT", tmp_path)

    # config/jobs/daily.yaml with one job
    jobs_dir = tmp_path / "config" / "jobs"
    jobs_dir.mkdir(parents=True)
    (jobs_dir / "daily.yaml").write_text(
        "jobs:\n"
        "  - job_id: daily_fixtures_by_date\n"
        "    enabled: true\n"
        "    type: incremental_daily\n"
        "    endpoint: /fixtures\n"
        "    interval: {type: cron, cron: '0 * * * *'}\n",
        encoding="utf-8",
    )
    (jobs_dir / "static.yaml").write_text("jobs: []\n", encoding="utf-8")
    (jobs_dir / "live.yaml").write_text("jobs: []\n", encoding="utf-8")

    # logs/collector.jsonl with a daily_sync_complete event (script != job_id)
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir(parents=True)
    log_file = logs_dir / "collector.jsonl"
    log_file.write_text(
        "\n".join(
            [
                json.dumps({"timestamp": "2025-12-12T00:00:00+00:00", "level": "info", "script": "daily_sync", "event": "daily_sync_started"}),
                json.dumps({"timestamp": "2025-12-12T00:10:00+00:00", "level": "info", "script": "daily_sync", "event": "daily_sync_complete"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("COLLECTOR_LOG_FILE", str(log_file))

    out = await server.get_job_status()
    assert out["ok"] is True
    # Should include config job. Log scripts may be aliased to job_id for better observability.
    ids = sorted([(x.get("job_id") or x.get("job_name")) for x in out["jobs"]])
    assert "daily_fixtures_by_date" in ids
    assert "daily_sync" not in ids


