from __future__ import annotations

from pathlib import Path

from src.jobs.auto_finish_stale_fixtures import _load_config


def test_load_config_reads_params_and_tracked_leagues(tmp_path: Path) -> None:
    """Verify config loading for auto_finish_stale_fixtures job."""
    daily_yaml = tmp_path / "daily.yaml"
    daily_yaml.write_text(
        "\n".join(
            [
                "jobs:",
                "- job_id: auto_finish_stale_fixtures",
                "  type: incremental_daily",
                "  enabled: true",
                "  endpoint: none",
                "  params:",
                "    threshold_hours: 3",
                "    safety_lag_hours: 4",
                "    max_fixtures_per_run: 2500",
                "    dry_run: true",
                "",
                "tracked_leagues:",
                "- id: 39",
                "- id: 206",
                "",
            ]
        ),
        encoding="utf-8",
    )

    cfg = _load_config(daily_yaml)
    assert cfg.threshold_hours == 3
    assert cfg.safety_lag_hours == 4
    assert cfg.max_fixtures_per_run == 2500
    assert cfg.dry_run is True
    assert cfg.scoped_league_ids == {39, 206}


def test_load_config_applies_guardrails(tmp_path: Path) -> None:
    """Verify guardrails clamp extreme values to safe ranges."""
    daily_yaml = tmp_path / "daily.yaml"
    daily_yaml.write_text(
        "\n".join(
            [
                "jobs:",
                "- job_id: auto_finish_stale_fixtures",
                "  type: incremental_daily",
                "  enabled: true",
                "  endpoint: none",
                "  params:",
                "    threshold_hours: 0",  # below min 1h
                "    safety_lag_hours: 200",  # above max 7d (168h)
                "    max_fixtures_per_run: 50000",  # above max 10000",
                "    dry_run: 'true'",  # string should be cast to bool
                "",
                "tracked_leagues:",
                "- id: 78",
                "",
            ]
        ),
        encoding="utf-8",
    )

    cfg = _load_config(daily_yaml)
    # Guardrails: threshold_hours min=1, max=7*24=168
    assert cfg.threshold_hours == 1
    # Guardrails: safety_lag_hours min=1, max=7*24=168
    assert cfg.safety_lag_hours == 168
    # Guardrails: max_fixtures_per_run min=1, max=10000
    assert cfg.max_fixtures_per_run == 10000
    # String 'true' should be cast to True
    assert cfg.dry_run is True
    assert cfg.scoped_league_ids == {78}


def test_load_config_defaults_when_missing(tmp_path: Path) -> None:
    """Verify default values when params are omitted."""
    daily_yaml = tmp_path / "daily.yaml"
    daily_yaml.write_text(
        "\n".join(
            [
                "jobs:",
                "- job_id: auto_finish_stale_fixtures",
                "  type: incremental_daily",
                "  enabled: true",
                "  endpoint: none",
                "",
                "tracked_leagues:",
                "- id: 140",
                "",
            ]
        ),
        encoding="utf-8",
    )

    cfg = _load_config(daily_yaml)
    # Defaults from code: threshold=2, safety_lag=3, max_fixtures=1000, dry_run=False
    assert cfg.threshold_hours == 2
    assert cfg.safety_lag_hours == 3
    assert cfg.max_fixtures_per_run == 1000
    assert cfg.dry_run is False
    assert cfg.scoped_league_ids == {140}


def test_load_config_raises_on_missing_tracked_leagues(tmp_path: Path) -> None:
    """Verify ValueError when tracked_leagues is missing."""
    daily_yaml = tmp_path / "daily.yaml"
    daily_yaml.write_text(
        "\n".join(
            [
                "jobs:",
                "- job_id: auto_finish_stale_fixtures",
                "  type: incremental_daily",
                "  enabled: true",
                "  endpoint: none",
                "  params:",
                "    threshold_hours: 2",
                "  # tracked_leagues: MISSING",
                "",
            ]
        ),
        encoding="utf-8",
    )

    try:
        _load_config(daily_yaml)
        assert False, "Expected ValueError for missing tracked_leagues"
    except ValueError as e:
        assert "Missing tracked_leagues" in str(e)


def test_load_config_filters_invalid_league_ids(tmp_path: Path) -> None:
    """Verify invalid league IDs are filtered out safely."""
    daily_yaml = tmp_path / "daily.yaml"
    daily_yaml.write_text(
        "\n".join(
            [
                "jobs:",
                "- job_id: auto_finish_stale_fixtures",
                "  type: incremental_daily",
                "  enabled: true",
                "  endpoint: none",
                "  params:",
                "    threshold_hours: 2",
                "",
                "tracked_leagues:",
                "- id: 39",
                "- id: 78",
                "- id: not_a_number",  # invalid
                "- id: 140",
                "- id:",  # invalid (missing value)
                "",
            ]
        ),
        encoding="utf-8",
    )

    cfg = _load_config(daily_yaml)
    # Only valid IDs should remain
    assert cfg.scoped_league_ids == {39, 78, 140}

