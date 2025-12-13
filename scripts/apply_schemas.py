from __future__ import annotations

import os
from pathlib import Path

import psycopg2


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


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    schemas_dir = root / "db" / "schemas"
    if not schemas_dir.exists():
        raise SystemExit(f"schemas_dir_missing:{schemas_dir}")

    sql_files = sorted([p for p in schemas_dir.glob("*.sql")])
    if not sql_files:
        raise SystemExit(f"no_sql_files_found:{schemas_dir}")

    dsn = _dsn()
    conn = psycopg2.connect(dsn, connect_timeout=5)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            for p in sql_files:
                sql_text = p.read_text(encoding="utf-8")
                cur.execute(sql_text)
                print(f"[OK] applied {p.name}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


