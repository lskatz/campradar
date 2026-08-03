"""Polite, cached HTTP.

Two rules this module exists to enforce:

1. **Don't re-download what hasn't changed.** Responses are cached with their
   ETag, and later requests are conditional. A weekly run over a hundred pages
   should transfer almost nothing.
2. **Don't hammer anyone.** These are small nonprofits and rec departments on
   shared hosting. A fixed delay between requests costs us nothing and keeps
   the tool welcome.

The cache also gives us raw snapshots for free, so a parser bug can be fixed
and re-run without touching the network again.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

__all__ = ["FetchError", "FetchResult", "Fetcher"]

log = logging.getLogger(__name__)

# Identifies the bot and gives site owners a way to reach a human.
USER_AGENT = (
    "CampRadar/0.2 (+https://github.com/lskatz/campradar; personal school-break camp tracker)"
)


class FetchError(RuntimeError):
    """A page could not be read at all."""


@dataclass(slots=True)
class FetchResult:
    url: str
    text: str
    status_code: int
    from_cache: bool


class Fetcher:
    """Cached, throttled HTTP client.

    The `client` argument is injectable so tests can pass an
    `httpx.MockTransport` and exercise the real code path without a socket.
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
        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        )
        self._last_request: float = 0.0

    def _paths(self, url: str) -> tuple[Path, Path]:
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:20]
        return self.cache_dir / f"{digest}.body", self.cache_dir / f"{digest}.meta"

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request
        if self._last_request and elapsed < self.delay_seconds:
            time.sleep(self.delay_seconds - elapsed)
        self._last_request = time.monotonic()

    def get(self, url: str) -> FetchResult:
        """Fetch a URL, using the cache when the server says nothing changed."""
        body_path, meta_path = self._paths(url)
        headers: dict[str, str] = {}
        if meta_path.exists() and body_path.exists():
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if etag := meta.get("etag"):
                headers["If-None-Match"] = etag

        self._throttle()
        try:
            response = self._client.get(url, headers=headers)
        except httpx.HTTPError as exc:
            raise FetchError(f"{url}: {exc}") from exc

        if response.status_code == 304 and body_path.exists():
            log.debug("%s: not modified", url)
            return FetchResult(
                url=url,
                text=body_path.read_text(encoding="utf-8"),
                status_code=304,
                from_cache=True,
            )

        if response.status_code >= 400:
            raise FetchError(f"{url}: HTTP {response.status_code}")

        body_path.write_text(response.text, encoding="utf-8")
        meta_path.write_text(
            json.dumps({"url": url, "etag": response.headers.get("etag")}),
            encoding="utf-8",
        )
        return FetchResult(
            url=url, text=response.text, status_code=response.status_code, from_cache=False
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> Fetcher:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
