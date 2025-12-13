from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlparse

import psycopg2
import redis


def _must_exist(path: str) -> None:
    p = Path(path)
    if not p.exists():
        raise SystemExit(f"missing_file:{p}")


def _check_db() -> None:
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
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


def _check_redis() -> None:
    url = os.getenv("REDIS_URL", "")
    if not url:
        raise SystemExit("missing_env:REDIS_URL")
    u = urlparse(url)
    if u.scheme not in ("redis", "rediss"):
        raise SystemExit("invalid_redis_url")
    r = redis.Redis.from_url(url, socket_connect_timeout=3, socket_timeout=3, decode_responses=True)
    r.ping()


def main() -> int:
    # If live loop isn't enabled, report healthy (service is intentionally idle).
    if os.getenv("ENABLE_LIVE_LOOP", "0") != "1":
        return 0

    _must_exist("/app/config/api.yaml")
    _must_exist("/app/config/rate_limiter.yaml")
    _must_exist("/app/config/jobs/live.yaml")
    _check_db()
    _check_redis()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


