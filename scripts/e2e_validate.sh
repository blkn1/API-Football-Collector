#!/usr/bin/env bash
set -euo pipefail

# End-to-end validation for the whole stack (read-only checks + HTTP smoke).
#
# Works in 2 modes:
#  1) Inside collector/mcp/read_api containers (has access to repo path /app):
#     DATABASE_URL=... READ_API_BASE=... MCP_BASE_URL=... bash scripts/e2e_validate.sh
#  2) Locally (repo checkout):
#     source .venv/bin/activate
#     export DATABASE_URL=...
#     export READ_API_BASE=...
#     export MCP_BASE_URL=...
#     bash scripts/e2e_validate.sh
#
# Notes:
# - This script never calls API-Football directly (0 quota).
# - It verifies by evidence: RAW rows, CORE rows, MART coverage.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

echo "== e2e_validate =="
date -u
echo

if [[ -z "${DATABASE_URL:-}" ]]; then
  echo "ERROR: DATABASE_URL is required."
  exit 1
fi

if ! command -v psql >/dev/null 2>&1; then
  echo "ERROR: psql not found in this environment."
  echo "Run this inside the postgres container OR install psql client."
  exit 1
fi

echo "== DB checks (SQL) =="
psql "${DATABASE_URL}" -v ON_ERROR_STOP=1 -v season="${READ_API_DEFAULT_SEASON:-}" -f scripts/db_checks.sql
echo

if [[ -n "${READ_API_BASE:-${SERVICE_URL_READ_API:-}}" ]]; then
  echo "== Read API smoke =="
READ_API_BASE="${READ_API_BASE:-${SERVICE_URL_READ_API:-}}" bash scripts/smoke_read_api.sh
  echo
else
  echo "== Read API smoke skipped (READ_API_BASE not set) =="
  echo
fi

if [[ -n "${MCP_BASE_URL:-${SERVICE_URL_MCP:-}}" ]]; then
  echo "== MCP smoke =="
MCP_BASE_URL="${MCP_BASE_URL:-${SERVICE_URL_MCP:-}}" bash scripts/smoke_mcp.sh
  echo
else
  echo "== MCP smoke skipped (MCP_BASE_URL not set) =="
  echo
fi

echo "OK: e2e_validate completed"


