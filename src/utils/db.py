from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Iterable

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv
from psycopg2 import sql
from psycopg2.pool import ThreadedConnectionPool

try:
    # scripts/ context (scripts add src/ to sys.path)
    from utils.logging import get_logger  # type: ignore
except Exception:  # pragma: no cover
    from src.utils.logging import get_logger


_POOL: ThreadedConnectionPool | None = None
logger = get_logger(component="db")


def _build_dsn() -> str:
    load_dotenv()
    # Prefer explicit POSTGRES_* params when provided (useful for local/docker overrides),
    # otherwise fall back to DATABASE_URL if present.
    host = os.getenv("POSTGRES_HOST")
    port = os.getenv("POSTGRES_PORT")
    user = os.getenv("POSTGRES_USER")
    password = os.getenv("POSTGRES_PASSWORD")
    db = os.getenv("POSTGRES_DB")

    if host and port and user and password and db:
        return f"postgresql://{user}:{password}@{host}:{int(port)}/{db}"

    database_url = os.getenv("DATABASE_URL")
    if database_url:
        return database_url

    user = os.getenv("POSTGRES_USER", "postgres")
    password = os.getenv("POSTGRES_PASSWORD", "postgres")
    db = os.getenv("POSTGRES_DB", "api_football")
    host = os.getenv("POSTGRES_HOST", "localhost")
    port = int(os.getenv("POSTGRES_PORT", "5432"))

    return f"postgresql://{user}:{password}@{host}:{port}/{db}"


def init_pool(minconn: int = 1, maxconn: int = 5) -> None:
    global _POOL
    if _POOL is not None:
        return
    dsn = _build_dsn()
    _POOL = ThreadedConnectionPool(minconn=minconn, maxconn=maxconn, dsn=dsn)


def reset_pool() -> None:
    """Close and reset the global connection pool (useful for tests)."""
    global _POOL
    if _POOL is not None:
        try:
            _POOL.closeall()
        finally:
            _POOL = None


@contextmanager
def get_db_connection():
    """
    Get a pooled psycopg2 connection.
    Usage:
      with get_db_connection() as conn:
          ...
    """
    if _POOL is None:
        init_pool()
    assert _POOL is not None
    conn = _POOL.getconn()
    try:
        yield conn
    finally:
        _POOL.putconn(conn)


@contextmanager
def get_transaction():
    """
    Get a pooled psycopg2 connection with a transaction scope.
    - Commits on success
    - Rolls back on exception
    """
    if _POOL is None:
        init_pool()
    assert _POOL is not None
    conn = _POOL.getconn()
    try:
        # psycopg2 default autocommit is False; enforce explicitly.
        conn.autocommit = False
        yield conn
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception as e:  # rollback failure should be visible
            logger.warning("db_rollback_failed", err=str(e))
        raise
    finally:
        _POOL.putconn(conn)


def upsert_raw(
    *,
    endpoint: str,
    requested_params: dict[str, Any],
    status_code: int,
    response_headers: dict[str, Any],
    body: dict[str, Any],
) -> int:
    """
    Insert an API response into RAW archive.
    Returns inserted raw.api_responses.id
    """
    errors = body.get("errors") or []
    results = body.get("results")

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO raw.api_responses (
                  endpoint, requested_params, status_code, response_headers, body, errors, results
                )
                VALUES (%s, %s::jsonb, %s, %s::jsonb, %s::jsonb, %s::jsonb, %s)
                RETURNING id
                """,
                (
                    endpoint,
                    psycopg2.extras.Json(requested_params),
                    status_code,
                    psycopg2.extras.Json(response_headers),
                    psycopg2.extras.Json(body),
                    psycopg2.extras.Json(errors),
                    results,
                ),
            )
            inserted_id = cur.fetchone()[0]
        conn.commit()
    return int(inserted_id)


def upsert_core(
    *,
    full_table_name: str,
    rows: list[dict[str, Any]],
    conflict_cols: list[str],
    update_cols: list[str],
    conn=None,
) -> None:
    """
    Generic bulk UPSERT helper for CORE tables using INSERT ... ON CONFLICT DO UPDATE.
    full_table_name: e.g. "core.countries"
    """
    if not rows:
        return

    # Basic identifier sanitization (defense-in-depth)
    def _ok_ident(s: str) -> bool:
        return s.replace("_", "").isalnum()

    if not _ok_ident(full_table_name.replace(".", "_")):
        raise ValueError("Unsafe table name")
    for c in conflict_cols + update_cols:
        if not _ok_ident(c):
            raise ValueError(f"Unsafe column name: {c}")

    cols = list(rows[0].keys())
    for r in rows:
        if set(r.keys()) != set(cols):
            raise ValueError("All rows must have the same columns")

    insert_cols_sql = sql.SQL(", ").join(map(sql.Identifier, cols))
    conflict_cols_sql = sql.SQL(", ").join(map(sql.Identifier, conflict_cols))

    update_set_sql = sql.SQL(", ").join(
        sql.Composed(
            [sql.Identifier(c), sql.SQL(" = EXCLUDED."), sql.Identifier(c)]
        )
        for c in update_cols
    )

    stmt = sql.SQL(
        "INSERT INTO {table} ({cols}) VALUES %s "
        "ON CONFLICT ({conflict}) DO UPDATE SET {update_set}, updated_at = NOW()"
    ).format(
        table=sql.SQL(full_table_name),
        cols=insert_cols_sql,
        conflict=conflict_cols_sql,
        update_set=update_set_sql,
    )

    values: list[tuple[Any, ...]] = [tuple(r[c] for c in cols) for r in rows]
    # Adapt JSONB values (dict/list) for psycopg2
    adapted_values: list[tuple[Any, ...]] = []
    for row in values:
        adapted_row: list[Any] = []
        for v in row:
            if isinstance(v, (dict, list)):
                adapted_row.append(psycopg2.extras.Json(v))
            else:
                adapted_row.append(v)
        adapted_values.append(tuple(adapted_row))

    if conn is None:
        with get_db_connection() as conn2:
            with conn2.cursor() as cur:
                psycopg2.extras.execute_values(cur, stmt.as_string(conn2), adapted_values)
            conn2.commit()
        return

    # Transaction-managed caller provided a connection.
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(cur, stmt.as_string(conn), adapted_values)


def query_scalar(query: str, params: tuple[Any, ...] | None = None) -> Any:
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params or ())
            row = cur.fetchone()
        conn.commit()
    return row[0] if row else None


def upsert_mart_coverage(*, coverage_data: dict[str, Any], conn=None) -> None:
    """
    Insert/update mart.coverage_status (Phase 3 table).
    """
    sql_stmt = """
    INSERT INTO mart.coverage_status (
      league_id, season, endpoint,
      expected_count, actual_count,
      count_coverage, last_update, lag_minutes, freshness_coverage,
      raw_count, core_count, pipeline_coverage, overall_coverage,
      flags,
      calculated_at
    )
    VALUES (
      %s, %s, %s,
      %s, %s,
      %s, %s, %s, %s,
      %s, %s, %s, %s,
      %s,
      NOW()
    )
    ON CONFLICT (league_id, season, endpoint) DO UPDATE SET
      expected_count = EXCLUDED.expected_count,
      actual_count = EXCLUDED.actual_count,
      count_coverage = EXCLUDED.count_coverage,
      last_update = EXCLUDED.last_update,
      lag_minutes = EXCLUDED.lag_minutes,
      freshness_coverage = EXCLUDED.freshness_coverage,
      raw_count = EXCLUDED.raw_count,
      core_count = EXCLUDED.core_count,
      pipeline_coverage = EXCLUDED.pipeline_coverage,
      overall_coverage = EXCLUDED.overall_coverage,
      flags = EXCLUDED.flags,
      calculated_at = NOW()
    """

    vals = (
        coverage_data.get("league_id"),
        coverage_data.get("season"),
        coverage_data.get("endpoint"),
        coverage_data.get("expected_count"),
        coverage_data.get("actual_count"),
        coverage_data.get("count_coverage"),
        coverage_data.get("last_update"),
        coverage_data.get("lag_minutes"),
        coverage_data.get("freshness_coverage"),
        coverage_data.get("raw_count"),
        coverage_data.get("core_count"),
        coverage_data.get("pipeline_coverage"),
        coverage_data.get("overall_coverage"),
        coverage_data.get("flags"),
    )

    if conn is None:
        with get_db_connection() as conn2:
            with conn2.cursor() as cur:
                cur.execute(sql_stmt, vals)
            conn2.commit()
        return

    with conn.cursor() as cur:
        cur.execute(sql_stmt, vals)


