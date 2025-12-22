#!/usr/bin/env bash
set -euo pipefail

# Smoke test for Read API (REST + SSE).
#
# Usage:
#   READ_API_BASE="https://readapi.example.com" bash scripts/smoke_read_api.sh
#
# Optional (Basic Auth):
#   READ_API_BASIC_USER="user" READ_API_BASIC_PASSWORD="pass" bash scripts/smoke_read_api.sh
#
# Optional (fixtures date):
#   DATE_UTC="2025-12-18" bash scripts/smoke_read_api.sh
#
# Optional (feature store endpoints):
#   SMOKE_LEAGUE_ID=203 SMOKE_SEASON=2025 bash scripts/smoke_read_api.sh
#

BASE_URL="${READ_API_BASE:-${SERVICE_URL_READ_API:-}}"
if [[ -z "${BASE_URL}" ]]; then
  echo "ERROR: READ_API_BASE is not set."
  echo "Set it like: READ_API_BASE=\"https://<your-read-api-domain>\" bash scripts/smoke_read_api.sh"
  exit 1
fi

DATE_UTC="${DATE_UTC:-$(date -u +%F)}"
LIMIT="${LIMIT:-50}"
SMOKE_LEAGUE_ID="${SMOKE_LEAGUE_ID:-}"
SMOKE_SEASON="${SMOKE_SEASON:-${READ_API_DEFAULT_SEASON:-}}"

AUTH_ARGS=()
if [[ -n "${READ_API_BASIC_USER:-}" && -n "${READ_API_BASIC_PASSWORD:-}" ]]; then
  AUTH_ARGS=(-u "${READ_API_BASIC_USER}:${READ_API_BASIC_PASSWORD}")
fi

echo "Read API base: ${BASE_URL}"
echo "Date (UTC): ${DATE_UTC}"

echo
echo "== /v1/health =="
curl -sS "${AUTH_ARGS[@]}" "${BASE_URL}/v1/health" | sed -e 's/^/  /'

echo
echo "== /v1/quota =="
curl -sS "${AUTH_ARGS[@]}" "${BASE_URL}/v1/quota" | sed -e 's/^/  /'

echo
echo "== /v1/fixtures (date=${DATE_UTC}) =="
curl -sS "${AUTH_ARGS[@]}" "${BASE_URL}/v1/fixtures?date=${DATE_UTC}&limit=${LIMIT}" | head -n 5 | sed -e 's/^/  /'
echo "  ... (truncated)"

echo
echo "== /v1/sse/live-scores (5s sample) =="
if command -v timeout >/dev/null 2>&1; then
  timeout 5 curl -sS "${AUTH_ARGS[@]}" "${BASE_URL}/v1/sse/live-scores?interval_seconds=3&limit=300" | head -n 30 | sed -e 's/^/  /' || true
else
  echo "  (timeout not installed) Running a short sample (press Ctrl+C to stop):"
  curl -sS "${AUTH_ARGS[@]}" "${BASE_URL}/v1/sse/live-scores?interval_seconds=3&limit=300" | head -n 30 | sed -e 's/^/  /' || true
fi

if [[ -n "${SMOKE_LEAGUE_ID}" && -n "${SMOKE_SEASON}" ]]; then
  echo
  echo "== /read/leagues (limit=5) =="
  curl -sS "${AUTH_ARGS[@]}" "${BASE_URL}/read/leagues?limit=5" | head -n 20 | sed -e 's/^/  /' || true

  echo
  echo "== /read/top_scorers (league_id=${SMOKE_LEAGUE_ID}, season=${SMOKE_SEASON}) =="
  curl -sS "${AUTH_ARGS[@]}" "${BASE_URL}/read/top_scorers?league_id=${SMOKE_LEAGUE_ID}&season=${SMOKE_SEASON}&limit=10" | head -n 40 | sed -e 's/^/  /' || true

  echo
  echo "== /read/team_statistics (league_id=${SMOKE_LEAGUE_ID}, season=${SMOKE_SEASON}) =="
  curl -sS "${AUTH_ARGS[@]}" "${BASE_URL}/read/team_statistics?league_id=${SMOKE_LEAGUE_ID}&season=${SMOKE_SEASON}&limit=5" | head -n 40 | sed -e 's/^/  /' || true

  echo
  echo "== /read/coverage (league_id=${SMOKE_LEAGUE_ID}, season=${SMOKE_SEASON}) =="
  curl -sS "${AUTH_ARGS[@]}" "${BASE_URL}/read/coverage?league_id=${SMOKE_LEAGUE_ID}&season=${SMOKE_SEASON}&limit=10" | head -n 60 | sed -e 's/^/  /' || true
else
  echo
  echo "== /read/* skipped (set SMOKE_LEAGUE_ID and SMOKE_SEASON to enable) =="
fi

echo
echo "OK: smoke_read_api completed"


