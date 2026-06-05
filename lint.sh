#!/usr/bin/env bash
# Run all linters and type-checker.  Exit non-zero if any check fails.
# Usage: ./lint.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$SCRIPT_DIR/.venv/bin"

echo "=== ruff ==="
"$VENV/ruff" check claude_visualizer/ tests/

echo "=== black ==="
"$VENV/black" --check claude_visualizer/ tests/

echo "=== mypy ==="
"$VENV/mypy" claude_visualizer/ --ignore-missing-imports

echo ""
echo "All lint checks passed."
