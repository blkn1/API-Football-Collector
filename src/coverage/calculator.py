from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

try:
    # scripts/ context (adds /src to sys.path)
    from utils.db import query_scalar  # type: ignore
except ImportError:  # pragma: no cover
    # src/ package context
    from src.utils.db import query_scalar  # type: ignore


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
        league_id_i = int(league_id)
        expected_cfg = self.config.expected_fixtures
        expected_known = league_id_i in expected_cfg and int(expected_cfg.get(league_id_i) or 0) > 0
        expected = int(expected_cfg.get(league_id_i, 0))
        actual = int(self._query_actual_fixtures(league_id, season) or 0)

        count_cov: float | None = (actual / expected * 100.0) if expected_known else None

        last_update = self._query_last_update(league_id, season)
        lag_minutes = self._calculate_lag_minutes(last_update)

        max_lag = int(self.config.max_lag_minutes_daily)
        freshness_cov = max(0.0, 100.0 - (lag_minutes / max_lag * 100.0)) if max_lag > 0 else 0.0

        raw_count = int(self._query_raw_count_24h(league_id, season) or 0)
        pipeline_cov = (actual / raw_count * 100.0) if raw_count > 0 else 0.0

        w = self.config.weights
        w_count = float(w["count_coverage"])
        w_fresh = float(w["freshness_coverage"])
        w_pipe = float(w["pipeline_coverage"])
        if expected_known and count_cov is not None:
            overall = count_cov * w_count + freshness_cov * w_fresh + pipeline_cov * w_pipe
        else:
            # If expected fixture count isn't configured, don't punish leagues with a bogus 0% count_coverage.
            # Instead, compute overall from freshness + pipeline only (renormalized to 0..100).
            denom = (w_fresh + w_pipe) or 1.0
            overall = (freshness_cov * w_fresh + pipeline_cov * w_pipe) / denom

        league_name = self._query_league_name(league_id)
        last_update_iso = last_update.isoformat().replace("+00:00", "Z") if last_update else None

        return {
            "league_id": league_id_i,
            "league_name": league_name,
            "season": int(season),
            "endpoint": "/fixtures",
            "expected_count": (expected if expected_known else None),
            "actual_count": actual,
            "count_coverage": (round(float(count_cov), 2) if count_cov is not None else None),
            "last_update": last_update_iso,
            "lag_minutes": int(lag_minutes),
            "freshness_coverage": round(freshness_cov, 2),
            "raw_count": raw_count,
            "core_count": actual,
            "pipeline_coverage": round(pipeline_cov, 2),
            "overall_coverage": round(overall, 2),
        }

    def calculate_injuries_coverage(self, league_id: int, season: int) -> dict[str, Any]:
        """
        Coverage for /injuries (current-only):
        - expected_count = 1 (we only need "present + fresh")
        - actual_count = 1 if we have any injuries rows for league+season, else 0
        """
        core_total = int(
            query_scalar(
                "SELECT COUNT(*) FROM core.injuries WHERE league_id = %s AND season = %s",
                (int(league_id), int(season)),
            )
            or 0
        )
        actual = 1 if core_total > 0 else 0
        expected = 1
        count_cov = 100.0 if actual >= expected else 0.0

        last_update = self._query_last_update_generic(
            table="core.injuries",
            where="league_id = %s AND season = %s",
            params=(int(league_id), int(season)),
        )
        lag_minutes = self._calculate_lag_minutes(last_update)
        max_lag = int(self.config.max_lag_minutes_daily)
        freshness_cov = max(0.0, 100.0 - (lag_minutes / max_lag * 100.0)) if max_lag > 0 else 0.0

        raw_count = int(
            query_scalar(
                """
                SELECT COUNT(*)
                FROM raw.api_responses
                WHERE endpoint = '/injuries'
                  AND fetched_at > NOW() - INTERVAL '24 hours'
                  AND requested_params->>'league' = %s
                  AND requested_params->>'season' = %s
                """,
                (str(int(league_id)), str(int(season))),
            )
            or 0
        )

        # For injuries, "pipeline" is best represented as freshness/presence (counts aren't comparable to RAW envelopes).
        pipeline_cov = 100.0 if raw_count > 0 and core_total >= 0 else 0.0

        w = self.config.weights
        overall = (
            count_cov * float(w["count_coverage"])
            + freshness_cov * float(w["freshness_coverage"])
            + pipeline_cov * float(w["pipeline_coverage"])
        )

        last_update_iso = last_update.isoformat().replace("+00:00", "Z") if last_update else None
        return {
            "league_id": int(league_id),
            "league_name": self._query_league_name(league_id),
            "season": int(season),
            "endpoint": "/injuries",
            "expected_count": expected,
            "actual_count": actual,
            "count_coverage": round(count_cov, 2),
            "last_update": last_update_iso,
            "lag_minutes": int(lag_minutes),
            "freshness_coverage": round(freshness_cov, 2),
            "raw_count": raw_count,
            "core_count": core_total,
            "pipeline_coverage": round(pipeline_cov, 2),
            "overall_coverage": round(overall, 2),
        }

    def calculate_fixture_endpoint_coverage(
        self,
        *,
        league_id: int,
        season: int,
        endpoint: str,
        core_table: str,
        days: int = 90,
    ) -> dict[str, Any]:
        """
        Coverage for per-fixture endpoints (players/events/statistics/lineups) over a rolling window.
        - expected_count = completed fixtures in last N days
        - actual_count   = distinct fixtures with RAW call for endpoint in last N days
        - pipeline_cov   = distinct fixtures with CORE rows / distinct fixtures with RAW call
        """
        expected = int(
            query_scalar(
                """
                SELECT COUNT(*)
                FROM core.fixtures
                WHERE league_id = %s AND season = %s
                  AND date >= NOW() - (%s::text || ' days')::interval
                  AND status_short = ANY(ARRAY['FT','AET','PEN'])
                """,
                (int(league_id), int(season), int(days)),
            )
            or 0
        )

        raw_fixtures = int(
            query_scalar(
                """
                SELECT COUNT(DISTINCT f.id)
                FROM raw.api_responses r
                JOIN core.fixtures f ON f.id = (r.requested_params->>'fixture')::bigint
                WHERE r.endpoint = %s
                  AND f.league_id = %s
                  AND f.season = %s
                  AND f.date >= NOW() - (%s::text || ' days')::interval
                  AND f.status_short = ANY(ARRAY['FT','AET','PEN'])
                """,
                (str(endpoint), int(league_id), int(season), int(days)),
            )
            or 0
        )

        core_fixtures = int(
            query_scalar(
                f"""
                SELECT COUNT(DISTINCT t.fixture_id)
                FROM {core_table} t
                JOIN core.fixtures f ON f.id = t.fixture_id
                WHERE f.league_id = %s
                  AND f.season = %s
                  AND f.date >= NOW() - (%s::text || ' days')::interval
                  AND f.status_short = ANY(ARRAY['FT','AET','PEN'])
                """,
                (int(league_id), int(season), int(days)),
            )
            or 0
        )

        count_cov = (raw_fixtures / expected * 100.0) if expected > 0 else 0.0

        last_update = self._query_last_raw_endpoint_update_joined(endpoint=endpoint, league_id=league_id, season=season)
        lag_minutes = self._calculate_lag_minutes(last_update)
        max_lag = int(self.config.max_lag_minutes_daily)
        freshness_cov = max(0.0, 100.0 - (lag_minutes / max_lag * 100.0)) if max_lag > 0 else 0.0

        raw_count_24h = int(
            query_scalar(
                """
                SELECT COUNT(*)
                FROM raw.api_responses r
                JOIN core.fixtures f ON f.id = (r.requested_params->>'fixture')::bigint
                WHERE r.endpoint = %s
                  AND r.fetched_at > NOW() - INTERVAL '24 hours'
                  AND f.league_id = %s
                  AND f.season = %s
                """,
                (str(endpoint), int(league_id), int(season)),
            )
            or 0
        )

        pipeline_cov = (core_fixtures / raw_fixtures * 100.0) if raw_fixtures > 0 else 0.0

        w = self.config.weights
        overall = (
            count_cov * float(w["count_coverage"])
            + freshness_cov * float(w["freshness_coverage"])
            + pipeline_cov * float(w["pipeline_coverage"])
        )

        last_update_iso = last_update.isoformat().replace("+00:00", "Z") if last_update else None
        return {
            "league_id": int(league_id),
            "league_name": self._query_league_name(league_id),
            "season": int(season),
            "endpoint": str(endpoint),
            "expected_count": expected,
            "actual_count": raw_fixtures,
            "count_coverage": round(count_cov, 2),
            "last_update": last_update_iso,
            "lag_minutes": int(lag_minutes),
            "freshness_coverage": round(freshness_cov, 2),
            "raw_count": raw_count_24h,
            "core_count": core_fixtures,
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

    def _query_last_update_generic(self, *, table: str, where: str, params: tuple[Any, ...]) -> datetime | None:
        v = query_scalar(f"SELECT MAX(updated_at) FROM {table} WHERE {where}", params)
        if isinstance(v, datetime):
            if v.tzinfo is None:
                return v.replace(tzinfo=timezone.utc)
            return v.astimezone(timezone.utc)
        return None

    def _query_last_raw_endpoint_update_joined(self, *, endpoint: str, league_id: int, season: int) -> datetime | None:
        v = query_scalar(
            """
            SELECT MAX(r.fetched_at)
            FROM raw.api_responses r
            JOIN core.fixtures f ON f.id = (r.requested_params->>'fixture')::bigint
            WHERE r.endpoint = %s
              AND f.league_id = %s
              AND f.season = %s
            """,
            (str(endpoint), int(league_id), int(season)),
        )
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


