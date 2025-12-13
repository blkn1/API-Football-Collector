from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from utils.db import query_scalar


@dataclass(frozen=True)
class CoverageConfig:
    expected_fixtures: dict[int, int]
    max_lag_minutes_daily: int
    max_lag_minutes_live: int
    weights: dict[str, float]


class CoverageCalculator:
    def __init__(self, config_path: str | Path = "config/coverage.yaml") -> None:
        p = Path(config_path)
        cfg = yaml.safe_load(p.read_text(encoding="utf-8")) or {}

        expected_raw = cfg.get("expected_fixtures") or {}
        expected: dict[int, int] = {}
        for k, v in expected_raw.items():
            try:
                expected[int(k)] = int(v)
            except Exception:
                continue

        max_lag = cfg.get("max_lag_minutes") or {}
        weights = cfg.get("weights") or {}

        self.config = CoverageConfig(
            expected_fixtures=expected,
            max_lag_minutes_daily=int(max_lag.get("daily", 1440)),
            max_lag_minutes_live=int(max_lag.get("live", 5)),
            weights={
                "count_coverage": float(weights.get("count_coverage", 0.5)),
                "freshness_coverage": float(weights.get("freshness_coverage", 0.3)),
                "pipeline_coverage": float(weights.get("pipeline_coverage", 0.2)),
            },
        )

    def calculate_fixtures_coverage(self, league_id: int, season: int) -> dict[str, Any]:
        expected = int(self.config.expected_fixtures.get(int(league_id), 0))
        actual = int(self._query_actual_fixtures(league_id, season) or 0)

        count_cov = (actual / expected * 100.0) if expected > 0 else 0.0

        last_update = self._query_last_update(league_id, season)
        lag_minutes = self._calculate_lag_minutes(last_update)

        max_lag = int(self.config.max_lag_minutes_daily)
        freshness_cov = max(0.0, 100.0 - (lag_minutes / max_lag * 100.0)) if max_lag > 0 else 0.0

        raw_count = int(self._query_raw_count_24h(league_id, season) or 0)
        pipeline_cov = (actual / raw_count * 100.0) if raw_count > 0 else 0.0

        w = self.config.weights
        overall = (
            count_cov * float(w["count_coverage"])
            + freshness_cov * float(w["freshness_coverage"])
            + pipeline_cov * float(w["pipeline_coverage"])
        )

        league_name = self._query_league_name(league_id)
        last_update_iso = last_update.isoformat().replace("+00:00", "Z") if last_update else None

        return {
            "league_id": int(league_id),
            "league_name": league_name,
            "season": int(season),
            "endpoint": "/fixtures",
            "expected_count": expected,
            "actual_count": actual,
            "count_coverage": round(count_cov, 2),
            "last_update": last_update_iso,
            "lag_minutes": int(lag_minutes),
            "freshness_coverage": round(freshness_cov, 2),
            "raw_count": raw_count,
            "core_count": actual,
            "pipeline_coverage": round(pipeline_cov, 2),
            "overall_coverage": round(overall, 2),
        }

    def _query_actual_fixtures(self, league_id: int, season: int) -> int:
        return int(
            query_scalar(
                "SELECT COUNT(*) FROM core.fixtures WHERE league_id = %s AND season = %s",
                (int(league_id), int(season)),
            )
            or 0
        )

    def _query_last_update(self, league_id: int, season: int) -> datetime | None:
        v = query_scalar(
            "SELECT MAX(updated_at) FROM core.fixtures WHERE league_id = %s AND season = %s",
            (int(league_id), int(season)),
        )
        # psycopg2 returns datetime already
        if isinstance(v, datetime):
            if v.tzinfo is None:
                return v.replace(tzinfo=timezone.utc)
            return v.astimezone(timezone.utc)
        return None

    def _query_raw_count_24h(self, league_id: int, season: int) -> int:
        return int(
            query_scalar(
                """
                SELECT COUNT(*)
                FROM raw.api_responses
                WHERE endpoint = '/fixtures'
                  AND fetched_at > NOW() - INTERVAL '24 hours'
                  AND requested_params->>'league' = %s
                  AND requested_params->>'season' = %s
                """,
                (str(int(league_id)), str(int(season))),
            )
            or 0
        )

    def _query_league_name(self, league_id: int) -> str | None:
        v = query_scalar("SELECT name FROM core.leagues WHERE id = %s", (int(league_id),))
        return str(v) if v is not None else None

    def _calculate_lag_minutes(self, last_update: datetime | None) -> int:
        if not last_update:
            return 9999
        now = datetime.now(timezone.utc)
        delta = now - last_update
        return int(delta.total_seconds() / 60)


