#!/usr/bin/env bash
#
# Local refresh-and-publish.
#
#   scripts/update.sh          test, refresh, show what changed, commit, push
#   scripts/update.sh -n       stop before committing (dry run)
#   scripts/update.sh -m "..." custom commit message
#
# Everything happens on your machine. CI only publishes what you push, so the
# data in the repo is exactly what you reviewed — no scheduled job scraping
# behind your back and no write access granted to Actions.
#
# Ordering matters here: tests run *before* the refresh so a broken parser
# can't overwrite good data, and the summary prints *before* the commit so you
# get a chance to abort.

set -euo pipefail

DRY_RUN=0
MESSAGE=""

while [ $# -gt 0 ]; do
  case "$1" in
    -n|--dry-run) DRY_RUN=1; shift ;;
    -m|--message) MESSAGE="${2:-}"; shift 2 ;;
    -h|--help)    sed -n '3,12p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

cd "$(dirname "$0")/.."

# Prefer the installed script, fall back to running from source. This is what
# makes the script work inside a pixi or conda shell without `pip install -e .`.
if command -v campradar >/dev/null 2>&1; then
  CAMPRADAR="campradar"
else
  CAMPRADAR="env PYTHONPATH=src python3 -m campradar"
  echo "note: campradar not on PATH, running from source"
fi

echo "==> Tests"
if command -v pytest >/dev/null 2>&1; then
  pytest -q
else
  python3 -m pytest -q
fi

echo
echo "==> Refresh"
# No --previous-url: locally, data/state.json is the state store.
$CAMPRADAR refresh --verbose 2>&1 | tee data/refresh.log

echo
echo "==> Changes to be committed"
git add -A
if git diff --staged --quiet; then
  echo "Nothing changed. No commit needed."
  exit 0
fi
git diff --staged --stat

# A quick human-readable summary of what actually moved, rather than a JSON diff.
echo
echo "==> New camps in this run"
grep -E "^  NEW" data/refresh.log || echo "  (none)"

if [ "$DRY_RUN" -eq 1 ]; then
  echo
  echo "Dry run — staged but not committed. Review with: git diff --staged"
  echo "Commit when ready:  git commit -m 'refresh' && git push"
  exit 0
fi

if [ -z "$MESSAGE" ]; then
  COUNT=$(grep -cE "^  NEW" data/refresh.log || true)
  MESSAGE="refresh $(date -u +%Y-%m-%d): ${COUNT:-0} new"
fi

echo
echo "==> Commit and push"
git commit -m "$MESSAGE"
git push
echo
echo "Pushed. GitHub Actions will publish the site in a minute or two."
