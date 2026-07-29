"""The adapter contract.

An adapter's only job is: given a source config and a `Fetcher`, yield
`CampSession` objects. It must not write files, must not consult the clock for
anything that ends up in a record, and must not raise on a single malformed
listing — one bad row should be skipped and logged, not abort the source.

Failing loudly at the *source* level but softly at the *row* level is the whole
error-handling philosophy here: a dead site is worth a red build, a single camp
with an unparseable date is not.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Iterator
from typing import Any

from ..fetch import Fetcher
from ..models import CampSession

__all__ = ["Adapter", "AdapterError"]

log = logging.getLogger(__name__)


class AdapterError(RuntimeError):
    """Raised when a source is unusable as a whole (404, layout change, etc.)."""


class Adapter(ABC):
    """Base class for all source adapters.

    Subclasses implement `parse`. The pipeline calls `run`, which wraps `parse`
    so that per-row failures are counted and logged rather than propagated.
    """

    #: Registry name, matched against the `adapter:` key in sources.yaml.
    name: str = "base"

    def __init__(self, source_config: dict[str, Any]) -> None:
        self.config = source_config
        self.source_id: str = source_config["id"]
        self.provider_slug: str = source_config.get("provider_slug", self.source_id)

    @abstractmethod
    def parse(self, fetcher: Fetcher) -> Iterator[CampSession]:
        """Yield sessions from this source.

        Implementations should yield rather than return a list so that a
        partially-parsed page still contributes what it managed to read.
        """
        raise NotImplementedError

    def run(self, fetcher: Fetcher) -> list[CampSession]:
        """Execute `parse`, tolerating individual bad rows.

        Returns whatever parsed successfully. Raises `AdapterError` only if the
        source could not be read at all.
        """
        sessions: list[CampSession] = []
        skipped = 0
        try:
            iterator = self.parse(fetcher)
            while True:
                try:
                    sessions.append(next(iterator))
                except StopIteration:
                    break
                except (ValueError, KeyError, TypeError) as exc:
                    # One malformed listing. Note it and keep going.
                    skipped += 1
                    log.warning("%s: skipped a listing (%s)", self.source_id, exc)
        except AdapterError:
            raise
        except Exception as exc:  # noqa: BLE001 - deliberately broad at source level
            raise AdapterError(f"{self.source_id} failed: {exc}") from exc

        if skipped:
            log.info("%s: %d sessions, %d skipped", self.source_id, len(sessions), skipped)
        return sessions
