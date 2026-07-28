"""Shared pytest configuration.

Adds `src/` to the path so tests run against the package without requiring an
editable install first — useful in CI and for a fresh clone.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
