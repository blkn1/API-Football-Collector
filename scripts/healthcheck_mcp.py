from __future__ import annotations

import os
import socket
import sys
from urllib.parse import urlparse

import psycopg2


def _db_dsn() -> str:
    dsn = os.getenv("DATABASE_URL")
    if dsn:
        return dsn
    host = os.getenv("POSTGRES_HOST", "localhost")
    port = int(os.getenv("POSTGRES_PORT", "5432"))
    user = os.getenv("POSTGRES_USER", "postgres")
    password = os.getenv("POSTGRES_PASSWORD", "postgres")
    db = os.getenv("POSTGRES_DB", "api_football")
    return f"postgresql://{user}:{password}@{host}:{port}/{db}"


def _check_db() -> None:
    conn = psycopg2.connect(_db_dsn(), connect_timeout=5)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1;")
            cur.fetchone()
    finally:
        conn.close()


def _check_listening(host: str, port: int) -> None:
    # Healthcheck runs inside the container; use localhost.
    with socket.create_connection((host, int(port)), timeout=2.5):
        return


def main() -> int:
    # 1) DB connectivity (MCP tools depend on DB)
    _check_db()

    # 2) If MCP runs over HTTP (sse / streamable-http), ensure the port is listening.
    transport = str(os.getenv("MCP_TRANSPORT", "stdio")).strip().lower()
    if transport in ("sse", "streamable-http"):
        host = "127.0.0.1"
        port = int(os.getenv("FASTMCP_PORT", "8000"))
        _check_listening(host, port)

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as e:
        # Healthcheck must be terse and machine-readable for Docker.
        print(f"healthcheck_failed:{type(e).__name__}:{e}", file=sys.stderr)
        raise


