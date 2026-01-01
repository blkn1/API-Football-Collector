from __future__ import annotations

import os
import shutil
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import psycopg2

import asyncio

from src.jobs.auto_finish_stale_fixtures import run_auto_finish_stale_fixtures
from src.utils.db import get_db_connection


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
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
def test_auto_finish_stale_fixtures_integration(tmp_path: Path):
    """
    Integration test for auto_finish_stale_fixtures job.

    Scenario:
    1. Start ephemeral Postgres
    2. Insert test fixtures in stale states (NS, HT, 2H)
    3. Run auto_finish job
    4. Verify status changed to FT
    """
    if not _docker_available():
        pytest.skip("docker not available")

    # Start postgres
    container = "api-football-it-stale-fixture"
    schemas_dir = Path(__file__).resolve().parents[2] / "db" / "schemas"
    port = "54340"

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
        # Wait for postgres to be ready
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

        # Set environment for DB connection
        os.environ["POSTGRES_HOST"] = "localhost"
        os.environ["POSTGRES_PORT"] = port
        os.environ["POSTGRES_USER"] = "postgres"
        os.environ["POSTGRES_PASSWORD"] = "postgres"
        os.environ["POSTGRES_DB"] = "api_football"
        os.environ["DATABASE_URL"] = dsn

        # Create minimal test data
        now_utc = datetime.now(timezone.utc)

        # Insert test fixture that should be auto-finished
        # date_utc = 3 hours ago, updated_at = 4 hours ago
        stale_fixture_date = now_utc - timedelta(hours=3)
        stale_fixture_updated = now_utc - timedelta(hours=4)

        with get_db_connection() as conn:
            with conn.cursor() as cur:
                # Insert test league
                cur.execute(
                    """
                    INSERT INTO core.leagues (id, name, type)
                    VALUES (9999, 'Test League', 'League')
                    ON CONFLICT (id) DO NOTHING
                    """
                )

                # Insert test teams
                cur.execute(
                    """
                    INSERT INTO core.teams (id, name)
                    VALUES (9999, 'Test Home'), (10000, 'Test Away')
                    ON CONFLICT (id) DO NOTHING
                    """
                )

                # Insert stale fixture (should be auto-finished)
                cur.execute(
                    """
                    INSERT INTO core.fixtures (
                        id, league_id, season, home_team_id, away_team_id,
                        date, status_short, status_long, updated_at
                    )
                    VALUES (
                        999999, 9999, 2025, 9999, 10000,
                        %s, '2H', 'Second Half', %s
                    )
                    ON CONFLICT (id) DO UPDATE SET
                        status_short = EXCLUDED.status_short,
                        status_long = EXCLUDED.status_long,
                        updated_at = EXCLUDED.updated_at
                    """,
                    (stale_fixture_date, stale_fixture_updated),
                )

                # Insert fixture that's too recent (should NOT be auto-finished)
                # date_utc = 1 hour ago, updated_at = 2 hours ago
                recent_fixture_date = now_utc - timedelta(hours=1)
                recent_fixture_updated = now_utc - timedelta(hours=2)

                cur.execute(
                    """
                    INSERT INTO core.fixtures (
                        id, league_id, season, home_team_id, away_team_id,
                        date, status_short, status_long, updated_at
                    )
                    VALUES (
                        1000000, 9999, 2025, 9999, 10000,
                        %s, 'HT', 'Half Time', %s
                    )
                    ON CONFLICT (id) DO UPDATE SET
                        status_short = EXCLUDED.status_short,
                        status_long = EXCLUDED.status_long,
                        updated_at = EXCLUDED.updated_at
                    """,
                    (recent_fixture_date, recent_fixture_updated),
                )

                conn.commit()

        # Create test config
        test_config = tmp_path / "test_daily.yaml"
        test_config.write_text(
            "\n".join(
                [
                    "jobs:",
                    "- job_id: auto_finish_stale_fixtures",
                    "  type: incremental_daily",
                    "  enabled: true",
                    "  endpoint: none",
                    "  params:",
                    "    threshold_hours: 2",
                    "    safety_lag_hours: 3",
                    "    max_fixtures_per_run: 1000",
                    "    dry_run: false",
                    "",
                    "tracked_leagues:",
                    "- id: 9999",
                    "",
                ]
            ),
            encoding="utf-8",
        )

        # Run auto_finish job
        asyncio.run(run_auto_finish_stale_fixtures(config_path=test_config))

        # Verify results
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                # Check stale fixture was auto-finished
                cur.execute(
                    "SELECT status_short, status_long FROM core.fixtures WHERE id = 999999"
                )
                stale_result = cur.fetchone()
                assert stale_result is not None, "Stale fixture not found"
                assert stale_result[0] == "FT", f"Expected status FT, got {stale_result[0]}"
                assert "Auto-finished" in (stale_result[1] or ""), \
                    f"Expected auto-finished status_long, got {stale_result[1]}"

                # Check recent fixture was NOT auto-finished
                cur.execute(
                    "SELECT status_short FROM core.fixtures WHERE id = 1000000"
                )
                recent_result = cur.fetchone()
                assert recent_result is not None, "Recent fixture not found"
                assert recent_result[0] == "HT", \
                    f"Expected status HT (unchanged), got {recent_result[0]}"

    finally:
        subprocess.run(["docker", "rm", "-f", container], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
