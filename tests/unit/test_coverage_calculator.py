from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from coverage.calculator import CoverageCalculator


class _Calc(CoverageCalculator):
    def __init__(self, cfg_path: Path, *, actual: int, raw: int, last_update: datetime | None, league_name: str = "L"):
        super().__init__(cfg_path)
        self._actual = actual
        self._raw = raw
        self._last_update = last_update
        self._league_name = league_name
        self._scheduled_in_window = 1

    def _query_actual_fixtures(self, league_id: int, season: int) -> int:
        return self._actual

    def _query_raw_count_24h(self, league_id: int, season: int) -> int:
        return self._raw

    def _query_last_update(self, league_id: int, season: int):
        return self._last_update

    def _query_league_name(self, league_id: int):
        return self._league_name

    def _query_scheduled_fixtures_in_window(
        self, *, league_id: int, season: int, lookback_days: int, lookahead_days: int
    ) -> int:
        return int(self._scheduled_in_window)


def test_coverage_formula_basic(tmp_path: Path) -> None:
    cfg = tmp_path / "coverage.yaml"
    cfg.write_text(
        "expected_fixtures:\n  39: 380\nmax_lag_minutes:\n  daily: 1440\n  live: 5\nweights:\n  count_coverage: 0.5\n  freshness_coverage: 0.3\n  pipeline_coverage: 0.2\n",
        encoding="utf-8",
    )

    last_update = datetime.now(timezone.utc) - timedelta(minutes=15)
    calc = _Calc(cfg, actual=375, raw=380, last_update=last_update, league_name="Premier League")
    cov = calc.calculate_fixtures_coverage(39, 2024)

    assert cov["expected_count"] == 380
    assert cov["actual_count"] == 375
    assert cov["count_coverage"] == round(375 / 380 * 100, 2)
    assert cov["raw_count"] == 380
    assert cov["pipeline_coverage"] == round(375 / 380 * 100, 2)
    assert cov["lag_minutes"] >= 15
    assert cov["freshness_coverage"] > 0
    assert cov["overall_coverage"] > 0
    assert cov["flags"]["no_matches_scheduled"] is False


def test_coverage_edge_cases_no_expected_or_raw(tmp_path: Path) -> None:
    cfg = tmp_path / "coverage.yaml"
    cfg.write_text(
        "expected_fixtures:\n  39: 0\nmax_lag_minutes:\n  daily: 1440\n  live: 5\nweights:\n  count_coverage: 0.5\n  freshness_coverage: 0.3\n  pipeline_coverage: 0.2\n",
        encoding="utf-8",
    )
    calc = _Calc(cfg, actual=0, raw=0, last_update=None, league_name="L")
    cov = calc.calculate_fixtures_coverage(39, 2024)

    # expected_count=0 is treated as "unknown"; we don't emit a misleading 0% count coverage.
    assert cov["expected_count"] is None
    assert cov["count_coverage"] is None
    assert cov["pipeline_coverage"] == 0.0
    assert cov["lag_minutes"] == 9999
    # actual_count=0 must NOT be masked as "no_matches_scheduled"
    assert cov["flags"]["no_matches_scheduled"] is False


