#!/usr/bin/env bash
# Run the top-level test tree.
#
# Each area runs as its OWN process on purpose: agentic-framework and fabric both
# define top-level modules named `config`/`db`/`cache`. Collecting them in a
# single pytest process would let one app's modules shadow the other's, and the
# failure surfaces as a confusing unrelated import error. Each area has its own
# conftest putting the right source root on sys.path.
#
# Usage:
#   bash tests/run.sh
#   PYTHON=path/to/python bash tests/run.sh
#
# Requires pytest, plus each app's own requirements for the area being run.
# In CI these are separate jobs — see .github/workflows/ci.yml.

set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-python3}"

PASS=0; FAIL=0
run() {  # run <label> <cmd...>
  local label="$1"; shift
  echo ""
  echo "▶ $label"
  if "$@"; then echo "  PASS  $label"; PASS=$((PASS+1))
  else          echo "  FAIL  $label"; FAIL=$((FAIL+1)); fi
}

run "agentic-framework" "$PYTHON" -m pytest "$ROOT/tests/agentic_framework" -q
run "fabric"            "$PYTHON" -m pytest "$ROOT/tests/fabric" -q

echo ""
echo "── $PASS area(s) passed · $FAIL failed ──"
[ "$FAIL" -eq 0 ]
