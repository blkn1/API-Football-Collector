from __future__ import annotations

import os
from pathlib import Path

import psycopg2


def _must_exist(path: str) -> None:
    p = Path(path)
    if not p.exists():
        raise SystemExit(f"missing_file:{p}")


def _check_db() -> None:
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        # fall back to POSTGRES_* (same logic style as src/utils/db.py)
        host = os.getenv("POSTGRES_HOST", "localhost")
        port = int(os.getenv("POSTGRES_PORT", "5432"))
        user = os.getenv("POSTGRES_USER", "postgres")
        password = os.getenv("POSTGRES_PASSWORD", "postgres")
        db = os.getenv("POSTGRES_DB", "api_football")
        dsn = f"postgresql://{user}:{password}@{host}:{port}/{db}"

    conn = psycopg2.connect(dsn, connect_timeout=3)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1;")
            cur.fetchone()
    finally:
        conn.close()


def main() -> int:
    # Config must be present inside the image (we do NOT rely on host volume mounts in production).
    _must_exist("/app/config/api.yaml")
    _must_exist("/app/config/rate_limiter.yaml")
    _check_db()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


