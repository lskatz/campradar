"""Polite, cached HTTP.

Two rules this module exists to enforce:

1. **Don't re-download what hasn't changed.** Every response is cached with its
   ETag and Last-Modified headers, and subsequent requests are conditional. A
   weekly run over a hundred provider pages should transfer almost nothing.
2. **Don't hammer anyone.** These are small nonprofits and rec departments
   running on shared hosting. A fixed delay between requests to the same host
   costs us nothing and keeps the tool welcome.

The cache also gives us raw snapshots for free, which means a parser bug can be
fixed and re-run without touching the network again — the same reason you keep
raw reads around rather than only the assembly.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx

from .redact import redact

__all__ = ["FetchResult", "Fetcher"]

log = logging.getLogger(__name__)

# Identifies the bot and gives site owners a way to reach a human. Change the
# URL to your own repo before running this against real sites.
USER_AGENT = (
    "CampRadar/0.1 (+https://github.com/lskatz/campradar; personal school-break camp tracker)"
)


@dataclass(slots=True)
class FetchResult:
    """The outcome of one fetch."""

    url: str
    text: str
    from_cache: bool
    status_code: int

    @property
    def content_hash(self) -> str:
        """Hash of the body, used to skip re-parsing unchanged pages."""
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()[:16]


class Fetcher:
    """Cached HTTP client.

    Example:
        >>> with Fetcher(Path("data/raw")) as f:      # doctest: +SKIP
        ...     result = f.get("https://example.org/camps")
    """

    def __init__(
        self,
        cache_dir: Path,
        *,
        delay_seconds: float = 1.5,
        timeout: float = 20.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.delay_seconds = delay_seconds
        # Injectable so tests can pass a MockTransport client.
        self._client = client or httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        )
        self._last_request_at: dict[str, float] = {}

    # -- context manager ---------------------------------------------------

    def __enter__(self) -> Fetcher:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    # -- internals ---------------------------------------------------------

    def _cache_paths(self, url: str) -> tuple[Path, Path]:
        """Body and metadata paths for a URL.

        Keyed by hash rather than by a sanitised URL because query strings and
        length limits make filesystem-safe URL encoding a losing game.
        """
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
        return self.cache_dir / f"{digest}.body", self.cache_dir / f"{digest}.meta.json"

    def _throttle(self, url: str) -> None:
        """Sleep if we contacted this host too recently."""
        host = urlparse(url).netloc
        last = self._last_request_at.get(host)
        if last is not None:
            elapsed = time.monotonic() - last
            if elapsed < self.delay_seconds:
                time.sleep(self.delay_seconds - elapsed)
        self._last_request_at[host] = time.monotonic()

    # -- public API --------------------------------------------------------

    def get(self, url: str) -> FetchResult:
        """Fetch a URL, using a conditional request when we have it cached.

        A 304 response returns the cached body. Any network or HTTP error is
        raised — callers are expected to catch per-source so that one dead site
        doesn't abort the whole run.
        """
        body_path, meta_path = self._cache_paths(url)
        headers: dict[str, str] = {}
        cached_body: str | None = None

        if body_path.exists() and meta_path.exists():
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            cached_body = body_path.read_text(encoding="utf-8")
            if etag := meta.get("etag"):
                headers["If-None-Match"] = etag
            if last_modified := meta.get("last_modified"):
                headers["If-Modified-Since"] = last_modified

        self._throttle(url)
        response = self._client.get(url, headers=headers)

        if response.status_code == 304 and cached_body is not None:
            log.debug("unchanged: %s", url)
            return FetchResult(url=url, text=cached_body, from_cache=True, status_code=304)

        response.raise_for_status()

        body_path.write_text(response.text, encoding="utf-8")
        meta_path.write_text(
            json.dumps(
                {
                    # Redacted: this file is a debugging aid, not a place to
                    # persist a credential in plaintext.
                    "url": redact(url),
                    "etag": response.headers.get("ETag"),
                    "last_modified": response.headers.get("Last-Modified"),
                }
            ),
            encoding="utf-8",
        )
        return FetchResult(
            url=url, text=response.text, from_cache=False, status_code=response.status_code
        )
