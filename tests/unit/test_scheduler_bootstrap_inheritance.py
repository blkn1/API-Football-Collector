from __future__ import annotations

from pathlib import Path

import yaml

from src.utils.job_config import apply_bootstrap_scope_inheritance


def _write_yaml(path: Path, obj: dict) -> None:
    path.write_text(yaml.safe_dump(obj, sort_keys=False), encoding="utf-8")


def test_bootstrap_jobs_inherit_tracked_leagues_and_season_from_daily_yaml(tmp_path: Path) -> None:
    jobs_dir = tmp_path / "config" / "jobs"
    jobs_dir.mkdir(parents=True)

    _write_yaml(
        jobs_dir / "daily.yaml",
        {
            "jobs": [],
            "tracked_leagues": [
                {"id": 39, "name": "Premier League", "season": 2025},
                {"id": 140, "name": "La Liga", "season": 2025},
            ],
        },
    )

    _write_yaml(
        jobs_dir / "static.yaml",
        {
            "jobs": [
                {
                    "job_id": "bootstrap_leagues",
                    "type": "static_bootstrap",
                    "enabled": True,
                    "endpoint": "/leagues",
                    "params": {"season": None},
                    "filters": {"tracked_leagues": []},
                    "interval": {"type": "cron", "cron": "0 2 * * 0"},
                },
                {
                    "job_id": "bootstrap_teams",
                    "type": "static_bootstrap",
                    "enabled": True,
                    "endpoint": "/teams",
                    "params": {"season": None},
                    "mode": {"type": "per_league", "tracked_leagues": []},
                    "interval": {"type": "cron", "cron": "0 3 * * 0"},
                },
            ]
        },
    )

    leagues_job = apply_bootstrap_scope_inheritance(
        {
            "job_id": "bootstrap_leagues",
            "type": "static_bootstrap",
            "enabled": True,
            "endpoint": "/leagues",
            "params": {"season": None},
            "filters": {"tracked_leagues": []},
            "interval": {"type": "cron", "cron": "0 2 * * 0"},
        },
        jobs_dir=jobs_dir,
    )
    teams_job = apply_bootstrap_scope_inheritance(
        {
            "job_id": "bootstrap_teams",
            "type": "static_bootstrap",
            "enabled": True,
            "endpoint": "/teams",
            "params": {"season": None},
            "mode": {"type": "per_league", "tracked_leagues": []},
            "interval": {"type": "cron", "cron": "0 3 * * 0"},
        },
        jobs_dir=jobs_dir,
    )

    assert set(leagues_job["filters"]["tracked_leagues"]) == {39, 140}
    assert set(teams_job["mode"]["tracked_leagues"]) == {39, 140}
    assert leagues_job["params"]["season"] == 2025
    assert teams_job["params"]["season"] == 2025


def test_bootstrap_season_not_inferred_when_daily_has_multiple_seasons(tmp_path: Path) -> None:
    jobs_dir = tmp_path / "config" / "jobs"
    jobs_dir.mkdir(parents=True)

    _write_yaml(
        jobs_dir / "daily.yaml",
        {
            "jobs": [],
            "tracked_leagues": [
                {"id": 39, "name": "Premier League", "season": 2025},
                {"id": 399, "name": "NPFL", "season": 2026},
            ],
        },
    )

    _write_yaml(
        jobs_dir / "static.yaml",
        {
            "jobs": [
                {
                    "job_id": "bootstrap_leagues",
                    "type": "static_bootstrap",
                    "enabled": True,
                    "endpoint": "/leagues",
                    "params": {"season": None},
                    "filters": {"tracked_leagues": []},
                    "interval": {"type": "cron", "cron": "0 2 * * 0"},
                }
            ]
        },
    )

    leagues_job = apply_bootstrap_scope_inheritance(
        {
            "job_id": "bootstrap_leagues",
            "type": "static_bootstrap",
            "enabled": True,
            "endpoint": "/leagues",
            "params": {"season": None},
            "filters": {"tracked_leagues": []},
            "interval": {"type": "cron", "cron": "0 2 * * 0"},
        },
        jobs_dir=jobs_dir,
    )
    assert set(leagues_job["filters"]["tracked_leagues"]) == {39, 399}
    # Multiple seasons present -> inference is unsafe, so leave as None (caller must configure explicitly)
    assert leagues_job["params"].get("season") is None


