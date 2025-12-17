#!/usr/bin/env bash
set -euo pipefail

# MCP streamable-http smoke test:
# - initialize -> tools/list -> tools/call
#
# Works against production MCP behind Traefik/Coolify (stateful session).
#
# Usage:
#   bash scripts/smoke_mcp.sh
#   SERVICE_URL_MCP="https://mcp.example.com" bash scripts/smoke_mcp.sh
#   MCP_BASE_URL="https://mcp.example.com" MCP_PATH="/mcp" bash scripts/smoke_mcp.sh
#
# Notes:
# - After redeploy, the server restarts and old sessions become invalid; rerun this script.
# - streamable-http requires Accept header to include both application/json and text/event-stream.

BASE_URL="${MCP_BASE_URL:-${SERVICE_URL_MCP:-https://mcp.zinalyze.pro}}"
MCP_PATH="${MCP_PATH:-/mcp}"

_require() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "ERROR: missing dependency: $1" >&2
    exit 1
  }
}

_require curl
_require awk
_require tr
_require sed

TMP_INIT_BODY="$(mktemp -t mcp-init-body.XXXXXX)"
trap 'rm -f "$TMP_INIT_BODY"' EXIT

echo "MCP base: ${BASE_URL}"
echo "MCP path: ${MCP_PATH}"
echo "----"

echo "[1/3] initialize (get mcp-session-id)"
SESSION_ID="$(
  curl -sS -D - -o "$TMP_INIT_BODY" \
    -X POST "${BASE_URL}${MCP_PATH}" \
    -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" \
    --data '{"jsonrpc":"2.0","id":0,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"smoke_mcp.sh","version":"0.1"}}}' \
  | awk -F': ' 'BEGIN{IGNORECASE=1} $1=="mcp-session-id" {print $2}' \
  | tr -d '\r'
)"

if [[ -z "${SESSION_ID}" ]]; then
  echo "ERROR: initialize did not return mcp-session-id header." >&2
  echo "Body:" >&2
  cat "$TMP_INIT_BODY" >&2
  exit 1
fi

echo "mcp-session-id=${SESSION_ID}"
echo "initialize body (first lines):"
sed -n '1,20p' "$TMP_INIT_BODY"
echo "----"

echo "[2/3] tools/list"
curl -sS -i -X POST "${BASE_URL}${MCP_PATH}" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "mcp-session-id: ${SESSION_ID}" \
  --data '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{"cursor":null}}' \
  | sed -n '1,120p'
echo "----"

echo "[3/3] tools/call (prod ops smoke set)"

call_tool () {
  local id="$1"
  local name="$2"
  local args="$3"
  echo "--- tools/call: ${name} ---"
  curl -sS -i -X POST "${BASE_URL}${MCP_PATH}" \
    -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" \
    -H "mcp-session-id: ${SESSION_ID}" \
    --data "{\"jsonrpc\":\"2.0\",\"id\":${id},\"method\":\"tools/call\",\"params\":{\"name\":\"${name}\",\"arguments\":${args}}}" \
    | sed -n '1,160p'
  echo
}

# Requested operational order:
call_tool 2 get_backfill_progress '{"job_id":null,"season":null,"include_completed":false,"limit":200}'
call_tool 3 get_raw_error_summary '{"since_minutes":60,"endpoint":null,"top_endpoints_limit":25}'
call_tool 4 get_rate_limit_status '{}'
call_tool 5 get_database_stats '{}'

echo "OK: MCP smoke test completed."


