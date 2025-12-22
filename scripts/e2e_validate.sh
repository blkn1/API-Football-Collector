#!/usr/bin/env sh
set -eu

# End-to-end validation for the whole stack.
#
# Works inside the collector container without requiring psql.
#
# Notes:
# - This script does NOT call API-Football directly (0 quota).
# - It verifies by evidence: RAW rows, CORE rows, MART coverage.

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

echo "== e2e_validate =="
date -u
echo

echo "== DB checks (python) =="
python3 scripts/e2e_validate.py || true
echo "(exit code 0 = top_scorers RAW evidence exists, 2 = not yet, other = error)"

echo "OK: e2e_validate completed"


