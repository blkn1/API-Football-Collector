from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest
import psycopg2

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


@pytest.mark.integration
def test_bootstrap_with_mock_responses_and_postgres(tmp_path: Path):
    """
    Integration test:
    - Start ephemeral Postgres (docker)
    - Apply schemas via mounted init scripts
    - Insert RAW + UPSERT CORE using transforms with mock API responses
    """
    if not _docker_available():
        pytest.skip("docker not available")

    # Start postgres
    container = "api-football-it-postgres"
    schemas_dir = Path(__file__).resolve().parents[2] / "db" / "schemas"
    port = "54339"

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
        # wait until host TCP is reachable (not just unix socket inside container)
        dsn = f"postgresql://postgres:postgres@localhost:{port}/api_football"
        for _ in range(60):
            try:
                conn = psycopg2.connect(dsn)
                conn.close()
                break
            except Exception:
                time.sleep(1)

        os.environ["POSTGRES_HOST"] = "localhost"
        os.environ["POSTGRES_PORT"] = port
        os.environ["POSTGRES_USER"] = "postgres"
        os.environ["POSTGRES_PASSWORD"] = "postgres"
        os.environ["POSTGRES_DB"] = "api_football"
        os.environ["DATABASE_URL"] = dsn

        reset_pool()
        init_pool(minconn=1, maxconn=2)

        fixtures = Path(__file__).resolve().parents[1] / "fixtures" / "api_responses"
        countries = json.loads((fixtures / "countries.json").read_text())
        timezones = json.loads((fixtures / "timezone.json").read_text())
        leagues = json.loads((fixtures / "leagues_2024.json").read_text())
        teams = json.loads((fixtures / "teams_39_2024.json").read_text())

        # RAW inserts
        upsert_raw(endpoint="/countries", requested_params={}, status_code=200, response_headers={}, body=countries)
        upsert_raw(endpoint="/timezone", requested_params={}, status_code=200, response_headers={}, body=timezones)
        upsert_raw(endpoint="/leagues", requested_params={"season": 2024}, status_code=200, response_headers={}, body=leagues)
        upsert_raw(endpoint="/teams", requested_params={"league": 39, "season": 2024}, status_code=200, response_headers={}, body=teams)

        # CORE upserts
        upsert_core(
            full_table_name="core.countries",
            rows=transform_countries(countries),
            conflict_cols=["code"],
            update_cols=["name", "flag"],
        )
        upsert_core(
            full_table_name="core.timezones",
            rows=transform_timezones(timezones),
            conflict_cols=["name"],
            update_cols=["name"],
        )

        tracked = {39, 140}
        upsert_core(
            full_table_name="core.leagues",
            rows=transform_leagues(leagues, tracked_league_ids=tracked),
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

        assert query_scalar("SELECT COUNT(*) FROM raw.api_responses") == 4
        assert query_scalar("SELECT COUNT(*) FROM core.countries") == 3
        assert query_scalar("SELECT COUNT(*) FROM core.timezones") == 3
        # only tracked leagues written to core
        assert query_scalar("SELECT COUNT(*) FROM core.leagues") == 2
        assert query_scalar("SELECT COUNT(*) FROM core.teams") == 2
        assert query_scalar("SELECT COUNT(*) FROM core.venues") == 2
    finally:
        subprocess.run(["docker", "rm", "-f", container], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


