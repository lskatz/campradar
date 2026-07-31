# Convenience targets.
#
# Everything runs the code in this checkout via PYTHONPATH, deliberately NOT
# whatever `campradar` happens to be on PATH. A stale non-editable install
# silently shadows the source tree and makes edits appear to do nothing —
# running from src/ removes that whole class of confusion.
#
# The `campradar` console script still works fine after `pip install -e .`;
# it's just not what these targets use.

CAMPRADAR = PYTHONPATH=src python3 -m campradar

.PHONY: help install test lint fmt probe refresh update serve doctor clean

help:
	@echo "make probe     check every configured source for usable JSON-LD"
	@echo "make refresh   fetch camps and rebuild site data"
	@echo "make update    test + refresh, then print the git commands to run"
	@echo "make serve     preview the dashboard at http://localhost:8000"
	@echo "make test      run the test suite"
	@echo "make lint      check formatting and style (ruff)"
	@echo "make fmt       apply the fixes ruff can make itself"
	@echo "make install   install the package and dev dependencies"
	@echo "make doctor    diagnose which copy of the code is running"

install:
	pip install -e ".[dev]"

test:
	PYTHONPATH=src python3 -m pytest -q

lint:
	python3 -m ruff check src tests

# Applies only the fixes ruff considers safe. Anything left after this
# needs a human decision -- see the SIM103 suppression in models.py for
# an example of a finding that was right to refuse.
fmt:
	python3 -m ruff check src tests --fix

probe:
	$(CAMPRADAR) probe

refresh:
	$(CAMPRADAR) refresh --verbose

# The main loop. Never runs git — it tells you what to run.
update:
	bash scripts/update.sh

serve:
	@echo "http://localhost:8000"
	python3 -m http.server -d site 8000

# For when `campradar` behaves differently from `make`.
doctor:
	@echo "source tree:   $$(PYTHONPATH=src python3 -c 'import campradar; print(campradar.__file__)')"
	@echo "on PATH:       $$(command -v campradar || echo '(not installed)')"
	@echo "PATH resolves: $$(python3 -c 'import campradar; print(campradar.__file__)' 2>/dev/null || echo '(not importable outside src)')"
	@echo
	@echo "If 'PATH resolves' points into site-packages rather than this"
	@echo "directory, you have a stale non-editable install. Fix with:"
	@echo "    pip install -e . --force-reinstall"

clean:
	rm -rf data/raw data/refresh.log .pytest_cache
	find . -name __pycache__ -type d -exec rm -rf {} +
