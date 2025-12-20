from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest
import psycopg2

from collector.api_client import APIResult
from utils.standings import sync_standings
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


class _FakeClient:
    def __init__(self, envelope: dict):
        self._env = envelope

    async def get(self, endpoint: str, params: dict | None = None) -> APIResult:
        assert endpoint == "/standings"
        headers = {"x-ratelimit-requests-remaining": "7400", "X-RateLimit-Remaining": "299"}
        return APIResult(status_code=200, data=self._env, headers=headers)

    async def aclose(self) -> None:
        return None


@pytest.mark.integration
def test_standings_sync_replaces_rows(tmp_path: Path):
    if not _docker_available():
        pytest.skip("docker not available")

    container = "api-football-it-postgres-standings"
    schemas_dir = Path(__file__).resolve().parents[2] / "db" / "schemas"
    port = "54342"

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
        ready = False
        for _ in range(60):
            try:
                conn = psycopg2.connect(dsn)
                conn.close()
                ready = True
                break
            except Exception:
                time.sleep(1)
        if not ready:
            pytest.skip("postgres container started but TCP port is not reachable on host; skipping integration test")

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
        standings = json.loads((fixtures_dir / "standings.json").read_text())

        # Bootstrap deps
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

        cfg_path = tmp_path / "daily.yaml"
        cfg_path.write_text(
            "season: 2024\ntracked_leagues:\n  - id: 39\n    name: Premier League\n",
            encoding="utf-8",
        )

        import asyncio

        # First sync inserts 2 rows
        asyncio.run(
            sync_standings(
                league_filter=39,
                dry_run=False,
                config_path=cfg_path,
                client=_FakeClient(standings),
            )
        )
        assert query_scalar("SELECT COUNT(*) FROM raw.api_responses WHERE endpoint='/standings'") == 1
        assert query_scalar("SELECT COUNT(*) FROM core.standings WHERE league_id=39 AND season=2024") == 2

        # Second sync with only 1 team should replace table (delete-then-insert)
        standings_one = json.loads((fixtures_dir / "standings.json").read_text())
        standings_one["response"][0]["league"]["standings"][0] = standings_one["response"][0]["league"]["standings"][0][:1]
        asyncio.run(
            sync_standings(
                league_filter=39,
                dry_run=False,
                config_path=cfg_path,
                client=_FakeClient(standings_one),
            )
        )
        assert query_scalar("SELECT COUNT(*) FROM core.standings WHERE league_id=39 AND season=2024") == 1
    finally:
        subprocess.run(["docker", "rm", "-f", container], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


