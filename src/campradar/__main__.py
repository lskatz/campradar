"""Allow `python -m campradar` as an alternative to the installed script.

Useful when you don't want to (or can't) run `pip install -e .` — inside a
pixi or conda shell without pip, in a container, or when you just want to run
the code straight out of a checkout:

    PYTHONPATH=src python -m campradar refresh --verbose

Behaves identically to the `campradar` console script.
"""

from .cli import main

raise SystemExit(main())
