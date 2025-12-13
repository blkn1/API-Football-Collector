from __future__ import annotations

import os
from pathlib import Path

import psycopg2
from psycopg2 import errors as pg_errors
from urllib.parse import urlparse, urlunparse


def _dsn() -> str:
    dsn = os.getenv("DATABASE_URL")
    if dsn:
        return dsn
    host = os.getenv("POSTGRES_HOST", "localhost")
    port = int(os.getenv("POSTGRES_PORT", "5432"))
    user = os.getenv("POSTGRES_USER", "postgres")
    password = os.getenv("POSTGRES_PASSWORD", "postgres")
    db = os.getenv("POSTGRES_DB", "api_football")
    return f"postgresql://{user}:{password}@{host}:{port}/{db}"

def _target_db_name() -> str:
    dsn = os.getenv("DATABASE_URL")
    if dsn:
        u = urlparse(dsn)
        name = (u.path or "").lstrip("/")
        if name:
            return name
    return os.getenv("POSTGRES_DB", "api_football")

def _admin_dsn() -> str:
    """
    Connect to an admin database to create the target DB if missing.
    Prefer 'postgres' db.
    """
    dsn = _dsn()
    u = urlparse(dsn)
    # Replace db name with /postgres
    u2 = u._replace(path="/postgres")
    return urlunparse(u2)

def _ensure_database_exists() -> None:
    """
    Ensure target database exists. This is required in Coolify setups where the Postgres volume
    was initialized earlier with a different default DB name.
    """
    target = _target_db_name()
    admin = _admin_dsn()
    conn = psycopg2.connect(admin, connect_timeout=5)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname=%s", (target,))
            if cur.fetchone():
                return
            # CREATE DATABASE cannot be run inside a transaction.
            cur.execute(f'CREATE DATABASE "{target}"')
            print(f"[OK] created database {target}")
    finally:
        conn.close()

def _schemas_already_applied(cur) -> bool:
    """
    Idempotency guard.

    Our schema files include CREATE TRIGGER statements without IF NOT EXISTS.
    Re-applying them will raise DuplicateObject. In production, schemas should be applied once per DB.
    """
    cur.execute("SELECT to_regclass('raw.api_responses');")
    raw_ok = cur.fetchone()[0] is not None
    cur.execute("SELECT to_regclass('core.countries');")
    core_ok = cur.fetchone()[0] is not None
    cur.execute("SELECT to_regclass('mart.coverage_status');")
    mart_ok = cur.fetchone()[0] is not None
    return bool(raw_ok and core_ok and mart_ok)


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    schemas_dir = root / "db" / "schemas"
    if not schemas_dir.exists():
        raise SystemExit(f"schemas_dir_missing:{schemas_dir}")

    # Apply in a deterministic order and avoid psql meta-commands (\i) in 00_init.sql.
    # raw.sql must run before mart.sql (mart depends on raw.api_responses).
    ordered = ["raw.sql", "core.sql", "mart.sql"]
    sql_files = [schemas_dir / name for name in ordered if (schemas_dir / name).exists()]
    if not sql_files:
        raise SystemExit(f"no_sql_files_found:{schemas_dir}")

    # Ensure DB exists before attempting to connect/apply schemas.
    _ensure_database_exists()
    dsn = _dsn()
    conn = psycopg2.connect(dsn, connect_timeout=5)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            if _schemas_already_applied(cur):
                print("[OK] schemas already applied (skipping)")
                return 0
            for p in sql_files:
                sql_text = p.read_text(encoding="utf-8")
                try:
                    cur.execute(sql_text)
                    print(f"[OK] applied {p.name}")
                except pg_errors.DuplicateObject:
                    # Most common case: triggers already exist. Treat as already applied and exit cleanly.
                    print(f"[OK] schema already present (duplicate objects while applying {p.name}); skipping")
                    return 0
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


