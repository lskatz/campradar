# Convenience targets. Everything here is a thin wrapper — the underlying
# commands work fine on their own, this just saves typing and sidesteps the
# executable bit on scripts/update.sh, which some checkouts don't preserve.

.PHONY: help install test probe refresh update publish clean serve

help:
	@echo "make install   install the package and dev dependencies"
	@echo "make test      run the test suite"
	@echo "make probe     check every configured source for usable JSON-LD"
	@echo "make refresh   fetch camps and rebuild site data (no commit)"
	@echo "make update    test, refresh, review, commit and push"
	@echo "make publish   same as update but stops before committing"
	@echo "make serve     serve the dashboard at http://localhost:8000"

install:
	pip install -e ".[dev]"

test:
	pytest -q

probe:
	campradar probe

refresh:
	campradar refresh --verbose

# The main entry point for the local-first workflow.
update:
	bash scripts/update.sh

publish:
	bash scripts/update.sh --dry-run

serve:
	@echo "http://localhost:8000"
	python3 -m http.server -d site 8000

clean:
	rm -rf data/raw data/refresh.log .pytest_cache
	find . -name __pycache__ -type d -exec rm -rf {} +
