from __future__ import annotations

from pathlib import Path

from src.jobs.stale_live_refresh import _chunk, _load_config


def test_chunk_respects_max_20() -> None:
    ids = list(range(1, 46))  # 45 ids
    chunks = _chunk(ids, size=20)
    assert len(chunks) == 3
    assert len(chunks[0]) == 20
    assert len(chunks[1]) == 20
    assert len(chunks[2]) == 5


def test_load_config_scope_live_reads_live_yaml(tmp_path: Path) -> None:
    live_yaml = tmp_path / "live.yaml"
    live_yaml.write_text(
        "\n".join(
            [
                "jobs:",
                "- job_id: live_fixtures_all",
                "  type: live_loop",
                "  enabled: false",
                "  endpoint: /fixtures",
                "  filters:",
                "    tracked_leagues: [203, 39, 848]",
                "",
            ]
        ),
        encoding="utf-8",
    )

    daily_yaml = tmp_path / "daily.yaml"
    daily_yaml.write_text(
        "\n".join(
            [
                "jobs:",
                "- job_id: stale_live_refresh",
                "  type: incremental_daily",
                "  enabled: true",
                "  endpoint: /fixtures",
                "  params:",
                "    stale_threshold_minutes: 30",
                "    batch_size: 20",
                "    scope_source: live",
                f"    live_config_path: {live_yaml.as_posix()}",
                "",
            ]
        ),
        encoding="utf-8",
    )

    cfg = _load_config(daily_yaml)
    assert cfg.scope_source == "live"
    assert cfg.scoped_league_ids == {203, 39, 848}


