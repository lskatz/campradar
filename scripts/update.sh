#!/usr/bin/env bash
#
# Local refresh. Runs the tests, fetches camps, rebuilds the site data, and
# then tells you what to commit.
#
#   make update              or  bash scripts/update.sh
#   bash scripts/update.sh --skip-tests
#
# This script never runs git. It only reads git state to show you what changed
# and to print the commands you'd want next — staging, committing and pushing
# stay entirely in your hands.
#
# Ordering matters: tests run *before* the refresh so a broken parser can't
# overwrite good data.

set -euo pipefail

SKIP_TESTS=0
while [ $# -gt 0 ]; do
  case "$1" in
    --skip-tests) SKIP_TESTS=1; shift ;;
    -h|--help)    sed -n '3,14p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

cd "$(dirname "$0")/.."

# Always run the code in this checkout, never a stale install on PATH.
CAMPRADAR="env PYTHONPATH=src python3 -m campradar"

if [ "$SKIP_TESTS" -eq 0 ]; then
  echo "==> Tests"
  PYTHONPATH=src python3 -m pytest -q
  echo
fi

echo "==> Refresh"
mkdir -p data
$CAMPRADAR refresh --verbose 2>&1 | tee data/refresh.log
echo

# --- report ---------------------------------------------------------------
# Read-only git below. Nothing here modifies the index or the working tree.

echo "==> New camps in this run"
if grep -qE "^  NEW" data/refresh.log; then
  grep -E "^  NEW" data/refresh.log
else
  echo "  (none)"
fi
echo

if ! git rev-parse --git-dir >/dev/null 2>&1; then
  echo "Not a git repository — nothing further to do."
  exit 0
fi

echo "==> Files changed"
CHANGED=$(git status --porcelain -- data site 2>/dev/null || true)
if [ -z "$CHANGED" ]; then
  echo "  (none — data is already up to date)"
  echo
  echo "Nothing to commit."
  exit 0
fi
echo "$CHANGED" | sed 's/^/  /'
echo

NEW_COUNT=$(grep -cE "^  NEW" data/refresh.log || true)
NEW_COUNT=${NEW_COUNT:-0}

cat <<EOF
==> Ready to publish

Review the data before committing:

    git diff -- site/assets/data/sessions.json

Then, when it looks right:

    git add data site
    git commit -m "refresh $(date -u +%Y-%m-%d): ${NEW_COUNT} new"
    git push

Pushing to main triggers the deploy workflow, which republishes the site.
EOF
