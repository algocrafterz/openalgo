#!/usr/bin/env bash
# Run the BreakingTrade documented trend scans against whatever .xlsx exports are
# currently in signal_engine/pinescripts/intraday/breaking-trade/excel/ - no arguments
# needed. Drop a fresh "save page as Excel" snapshot into that folder and re-run this.
#
# Extra arguments are passed straight through, e.g.:
#   ./scan.sh --broad          # also show the broad composite-score watchlist
#   ./scan.sh --dir some/path  # scan a different folder
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "$REPO_ROOT"
PYTHONPATH=. exec uv run python -m signal_engine.analysis.breakingtrade "$@"
