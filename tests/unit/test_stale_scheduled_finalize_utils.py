from __future__ import annotations

from pathlib import Path

from src.jobs.stale_scheduled_finalize import _chunk, _load_config


def test_chunk_respects_max_20() -> None:
    ids = list(range(1, 46))  # 45 ids
    chunks = _chunk(ids, size=20)
    assert len(chunks) == 3
    assert len(chunks[0]) == 20
    assert len(chunks[1]) == 20
    assert len(chunks[2]) == 5


def test_load_config_reads_params_and_tracked_leagues(tmp_path: Path) -> None:
    daily_yaml = tmp_path / "daily.yaml"
    daily_yaml.write_text(
        "\n".join(
            [
                "jobs:",
                "- job_id: stale_scheduled_finalize",
                "  type: incremental_daily",
                "  enabled: true",
                "  endpoint: /fixtures",
                "  params:",
                "    stale_threshold_minutes: 120",
                "    lookback_days: 2",
                "    batch_size: 25   # should be capped to 20",
                "    max_fixtures_per_run: 9999  # should be capped to 2000",
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
    assert cfg.threshold_minutes == 120
    assert cfg.lookback_days == 2
    assert cfg.batch_size == 20
    assert cfg.max_fixtures_per_run == 2000
    assert cfg.scoped_league_ids == {39, 206}


