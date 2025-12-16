from __future__ import annotations

import os
from pathlib import Path

import psycopg2
from psycopg2 import errors as pg_errors
from urllib.parse import urlparse, urlunparse


def _split_sql_statements(sql_text: str) -> list[str]:
    """
    Split SQL into individual statements by semicolons, while respecting:
    - single quotes: '...'
    - double quotes: "..."
    - dollar-quoted strings: $$...$$ or $tag$...$tag$
    - SQL comments:
      - line comments: -- ... \\n
      - block comments: /* ... */

    This allows us to continue applying later statements even if one statement
    fails with DuplicateObject (e.g. triggers already exist).
    """
    s = sql_text
    out: list[str] = []
    buf: list[str] = []

    in_single = False
    in_double = False
    dollar_tag: str | None = None
    i = 0
    n = len(s)

    def _flush() -> None:
        stmt = "".join(buf).strip()
        buf.clear()
        if stmt:
            out.append(stmt)

    while i < n:
        ch = s[i]
        nxt = s[i + 1] if i + 1 < n else ""

        # Skip comments when not inside a quoted string/dollar-quote.
        if dollar_tag is None and not in_single and not in_double:
            # Line comment: -- ... (until newline)
            if ch == "-" and nxt == "-":
                # consume until newline (but keep newline to avoid merging lines)
                i += 2
                while i < n and s[i] != "\n":
                    i += 1
                # newline will be handled by normal flow
                continue

            # Block comment: /* ... */
            if ch == "/" and nxt == "*":
                i += 2
                while i + 1 < n and not (s[i] == "*" and s[i + 1] == "/"):
                    i += 1
                i = i + 2 if i + 1 < n else n
                continue

        # Dollar-quote start/end when not inside normal quotes
        if not in_single and not in_double and ch == "$":
            # Parse a dollar tag: $...$
            j = i + 1
            while j < n and s[j] != "$" and (s[j].isalnum() or s[j] == "_"):
                j += 1
            if j < n and s[j] == "$":
                tag = s[i : j + 1]  # includes both '$'
                if dollar_tag is None:
                    dollar_tag = tag
                    buf.append(tag)
                    i = j + 1
                    continue
                if dollar_tag == tag:
                    dollar_tag = None
                    buf.append(tag)
                    i = j + 1
                    continue

        if dollar_tag is not None:
            buf.append(ch)
            i += 1
            continue

        # Single quotes (handle escaped '' inside strings)
        if not in_double and ch == "'":
            if in_single and nxt == "'":
                buf.append("''")
                i += 2
                continue
            in_single = not in_single
            buf.append(ch)
            i += 1
            continue

        # Double quotes (identifiers)
        if not in_single and ch == '"':
            in_double = not in_double
            buf.append(ch)
            i += 1
            continue

        # Statement terminator
        if not in_single and not in_double and ch == ";":
            _flush()
            i += 1
            continue

        buf.append(ch)
        i += 1

    _flush()
    return out


def _apply_sql_file(cur, path: Path) -> None:
    """
    Apply a SQL file statement-by-statement, ignoring DuplicateObject errors.
    This prevents partial schema application when a later statement (e.g. trigger)
    already exists.
    """
    sql_text = path.read_text(encoding="utf-8")
    stmts = _split_sql_statements(sql_text)
    for stmt in stmts:
        if not stmt.strip():
            continue
        try:
            cur.execute(stmt)
        except pg_errors.DuplicateObject:
            # Triggers/indexes already exist -> safe to continue.
            continue
        except psycopg2.ProgrammingError as e:
            # Some splits may yield comment-only statements (e.g. "-- ...") which PostgreSQL treats
            # as an empty query. Skip those safely.
            if "can't execute an empty query" in str(e):
                continue
            raise


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
    # Guard against partially-applied schemas. We must ensure critical tables exist.
    checks = [
        "raw.api_responses",
        "core.countries",
        "core.fixtures",
        "core.standings",
        "core.backfill_progress",
        "mart.coverage_status",
    ]
    for name in checks:
        cur.execute("SELECT to_regclass(%s);", (name,))
        if cur.fetchone()[0] is None:
            return False
    return True


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    schemas_dir = root / "db" / "schemas"
    if not schemas_dir.exists():
        raise SystemExit(f"schemas_dir_missing:{schemas_dir}")

    # Apply base schemas in a deterministic order and avoid psql meta-commands (\i) in 00_init.sql.
    # raw.sql must run before mart.sql (mart depends on raw.api_responses).
    base = ["raw.sql", "core.sql", "mart.sql"]
    base_files = [schemas_dir / name for name in base if (schemas_dir / name).exists()]
    if not base_files:
        raise SystemExit(f"no_sql_files_found:{schemas_dir}")

    # Ensure DB exists before attempting to connect/apply schemas.
    _ensure_database_exists()
    dsn = _dsn()
    conn = psycopg2.connect(dsn, connect_timeout=5)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            base_applied = _schemas_already_applied(cur)

            # Always apply extension/migration files (idempotent). This allows schema evolution without
            # re-running trigger-heavy core.sql on every startup.
            extras = sorted(
                [
                    p
                    for p in schemas_dir.glob("*.sql")
                    if p.name not in set(base + ["00_init.sql"])
                ]
            )

            if not base_applied:
                for p in base_files:
                    try:
                        _apply_sql_file(cur, p)
                        print(f"[OK] applied {p.name}")
                    except pg_errors.DuplicateObject:
                        # Triggers already exist -> safe to continue.
                        print(f"[OK] base already present (duplicate objects while applying {p.name}); continuing")

            for p in extras:
                try:
                    _apply_sql_file(cur, p)
                    print(f"[OK] applied {p.name}")
                except pg_errors.DuplicateObject:
                    print(f"[OK] already present (duplicate objects while applying {p.name}); continuing")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


