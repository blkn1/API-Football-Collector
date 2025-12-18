from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import fakeredis
import pytest
import psycopg2

from collector.api_client import APIResult
from collector.delta_detector import DeltaDetector
from collector.rate_limiter import RateLimiter
from scripts.live_loop import run_iteration
from transforms.countries import transform_countries
from transforms.leagues import transform_leagues
from transforms.teams import transform_teams
from transforms.timezones import transform_timezones
from transforms.venues import transform_venues_from_teams
from utils.db import init_pool, query_scalar, reset_pool, upsert_core, upsert_raw


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    # Ensure the docker daemon is reachable (CI/prod hosts often have docker client but no daemon access).
    try:
        subprocess.run(
            ["docker", "info"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except Exception:
        return False


class _FakeClientSeq:
    def __init__(self, envelopes: list[dict]):
        self._envs = envelopes
        self._i = 0

    async def get(self, endpoint: str, params: dict | None = None) -> APIResult:
        assert endpoint == "/fixtures"
        assert (params or {}).get("live") == "all"
        env = self._envs[min(self._i, len(self._envs) - 1)]
        self._i += 1
        headers = {"x-ratelimit-requests-remaining": "7400", "X-RateLimit-Remaining": "299"}
        return APIResult(status_code=200, data=env, headers=headers)

    async def aclose(self) -> None:
        return None


@pytest.mark.integration
def test_live_loop_once_delta_detection_prevents_second_write(tmp_path: Path):
    if not _docker_available():
        pytest.skip("docker not available")

    # Start postgres
    container = "api-football-it-postgres-live"
    schemas_dir = Path(__file__).resolve().parents[2] / "db" / "schemas"
    port = "54341"

    subprocess.run(["docker", "rm", "-f", container], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            container,
            "-e",
            "POSTGRES_PASSWORD=postgres",
            "-e",
            "POSTGRES_USER=postgres",
            "-e",
            "POSTGRES_DB=api_football",
            "-p",
            f"{port}:5432",
            "-v",
            f"{schemas_dir}:/docker-entrypoint-initdb.d:ro",
            "postgres:15-alpine",
            "-c",
            "listen_addresses=*",
        ],
        check=True,
        stdout=subprocess.DEVNULL,
    )

    try:
        dsn = f"postgresql://postgres:postgres@localhost:{port}/api_football"
        for _ in range(60):
            try:
                conn = psycopg2.connect(dsn)
                conn.close()
                break
            except Exception:
                time.sleep(1)

        os.environ["DATABASE_URL"] = dsn
        os.environ["POSTGRES_HOST"] = "localhost"
        os.environ["POSTGRES_PORT"] = port
        os.environ["POSTGRES_USER"] = "postgres"
        os.environ["POSTGRES_PASSWORD"] = "postgres"
        os.environ["POSTGRES_DB"] = "api_football"
        reset_pool()
        init_pool(minconn=1, maxconn=2)

        fixtures_dir = Path(__file__).resolve().parents[1] / "fixtures" / "api_responses"
        countries = json.loads((fixtures_dir / "countries.json").read_text())
        timezones = json.loads((fixtures_dir / "timezone.json").read_text())
        leagues = json.loads((fixtures_dir / "leagues_2024.json").read_text())
        teams = json.loads((fixtures_dir / "teams_39_2024.json").read_text())

        env1 = json.loads((fixtures_dir / "fixtures_live_all_1.json").read_text())
        env2 = json.loads((fixtures_dir / "fixtures_live_all_1.json").read_text())  # identical second poll

        # Bootstrap minimal dependencies (countries, timezones, leagues, teams, venues)
        upsert_raw(endpoint="/countries", requested_params={}, status_code=200, response_headers={}, body=countries)
        upsert_raw(endpoint="/timezone", requested_params={}, status_code=200, response_headers={}, body=timezones)
        upsert_raw(endpoint="/leagues", requested_params={"season": 2024}, status_code=200, response_headers={}, body=leagues)
        upsert_raw(endpoint="/teams", requested_params={"league": 39, "season": 2024}, status_code=200, response_headers={}, body=teams)

        upsert_core(full_table_name="core.countries", rows=transform_countries(countries), conflict_cols=["code"], update_cols=["name", "flag"])
        upsert_core(full_table_name="core.timezones", rows=transform_timezones(timezones), conflict_cols=["name"], update_cols=["name"])
        upsert_core(
            full_table_name="core.leagues",
            rows=transform_leagues(leagues, tracked_league_ids={39}),
            conflict_cols=["id"],
            update_cols=["name", "type", "logo", "country_name", "country_code", "country_flag", "seasons"],
        )
        upsert_core(
            full_table_name="core.venues",
            rows=transform_venues_from_teams(teams),
            conflict_cols=["id"],
            update_cols=["name", "address", "city", "country", "capacity", "surface", "image"],
        )
        upsert_core(
            full_table_name="core.teams",
            rows=transform_teams(teams),
            conflict_cols=["id"],
            update_cols=["name", "code", "country", "founded", "national", "logo", "venue_id"],
        )

        # Fake client + fake redis + limiter
        import asyncio

        client = _FakeClientSeq([env1, env2])
        limiter = RateLimiter(max_tokens=300, refill_rate=1000.0)
        redis_client = fakeredis.FakeRedis(decode_responses=True)
        detector = DeltaDetector(redis_client)

        stats1 = asyncio.run(
            run_iteration(
                client=client,
                limiter=limiter,
                detector=detector,
                tracked_leagues={39},
                dry_run=False,
            )
        )
        assert stats1.fixtures_tracked == 1
        assert stats1.fixtures_written == 1
        assert query_scalar("SELECT COUNT(*) FROM core.fixtures") == 1

        updated_at_1 = query_scalar("SELECT updated_at FROM core.fixtures WHERE id = 1234567")

        stats2 = asyncio.run(
            run_iteration(
                client=client,
                limiter=limiter,
                detector=detector,
                tracked_leagues={39},
                dry_run=False,
            )
        )
        assert stats2.fixtures_written == 0
        updated_at_2 = query_scalar("SELECT updated_at FROM core.fixtures WHERE id = 1234567")
        assert updated_at_2 == updated_at_1
    finally:
        subprocess.run(["docker", "rm", "-f", container], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


