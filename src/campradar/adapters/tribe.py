"""Reads sessions from The Events Calendar's WordPress REST API.

Why this exists
---------------
Callanwolde's camps page is prose — no schema.org markup, nothing to parse —
which is why `callanwolde-camps` fetched cleanly and produced zero sessions for
weeks. But the response headers on that same page gave the game away:

    X-Tec-Api-Root: https://callanwolde.org/wp-json/tribe/events/v1/
    X-Tec-Api-Version: v1

The site runs The Events Calendar, a very common WordPress plugin, and its REST
API is public, unauthenticated, documented, and filterable by date. That is a
far better source than either scraping the page or going through a third party:
no key, no quota, no gateway, and the data comes from the provider directly.

The endpoint self-documents. `GET /wp-json/tribe/events/v1/` returns the full
parameter schema, which is how the query parameters below were confirmed rather
than guessed.

Reusability
-----------
The Events Calendar is used by a large number of arts nonprofits, nature
centres and museums, so this adapter is named for the platform rather than for
Callanwolde. Pointing it at a new provider is a `base_url` in config.

To find out whether a site qualifies, look for `X-Tec-Api-Root` in its response
headers, or fetch `/wp-json/` and check whether `tribe/events/v1` appears in
`namespaces`. Zoo Atlanta, for example, runs WordPress but does *not* have the
plugin, so it needs a different approach.

Confidence
----------
The *request* schema below was read from a live discovery call against
Callanwolde. The *response* field names follow The Events Calendar's documented
format; they have not been checked against a live response body. Everything is
therefore read defensively — a field that is missing or oddly shaped is skipped
and logged, not assumed.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from datetime import date
from typing import Any

import httpx
from bs4 import BeautifulSoup

from ..fetch import Fetcher
from ..models import CampSession, RegistrationStatus
from .base import Adapter, AdapterError
from .jsonld import parse_age_text

__all__ = [
    "API_PATH",
    "CATEGORIES_PATH",
    "TribeEventsAdapter",
    "build_events_url",
    "strip_html",
]

log = logging.getLogger(__name__)

#: Paths appended to a site root. Stable across plugin versions since v4.
#: Spelled out separately rather than derived from one another: string-munging
#: API_PATH to reach the categories endpoint hits the '/events' inside
#: 'tribe/events/v1' first and silently produces a 404 route.
API_ROOT = "/wp-json/tribe/events/v1"
API_PATH = f"{API_ROOT}/events"
CATEGORIES_PATH = f"{API_ROOT}/categories"

DEFAULT_PER_PAGE = 50

#: The API reports `total_pages`, but trust it only so far: a misconfigured
#: query should cost a bounded number of requests against someone else's
#: shared hosting, not however many they happen to advertise.
MAX_PAGES = 20


def build_events_url(base_url: str, params: dict[str, Any]) -> str:
    """Assemble an events URL from a site root and query parameters.

        >>> build_events_url("https://example.org", {"per_page": 2})
        'https://example.org/wp-json/tribe/events/v1/events?per_page=2'

    A trailing slash on the base is tolerated, because half the world writes it
    and a doubled slash makes some WordPress installs 404:

        >>> a = build_events_url("https://example.org/", {"per_page": 2})
        >>> a == build_events_url("https://example.org", {"per_page": 2})
        True

    Parameters are sorted so the cache key is stable across runs; a dict
    reordering would otherwise silently invalidate every cached response.

        >>> build_events_url("https://e.org", {"b": 2, "a": 1})
        'https://e.org/wp-json/tribe/events/v1/events?a=1&b=2'

    List values become repeated `key[]=` pairs, which is what WordPress expects
    and what the plugin's own forum threads confirm is required:

        >>> build_events_url("https://e.org", {"categories": ["camps", "youth"]})
        'https://e.org/wp-json/tribe/events/v1/events?categories%5B%5D=camps&categories%5B%5D=youth'
    """
    from urllib.parse import urlencode

    root = base_url.rstrip("/")
    pairs: list[tuple[str, Any]] = []
    for key in sorted(params):
        value = params[key]
        if value in (None, ""):
            continue
        if isinstance(value, (list, tuple, set)):
            # WordPress reads repeated array params as `key[]=a&key[]=b`. Sending
            # a bare `categories=camps` returns everything, silently — the plugin
            # ignores the malformed filter rather than erroring, which is the
            # worst possible failure mode.
            pairs.extend((f"{key}[]", item) for item in value)
        else:
            pairs.append((key, value))
    return f"{root}{API_PATH}?{urlencode(pairs)}"


def strip_html(value: Any) -> str | None:
    """Flatten The Events Calendar's HTML descriptions to plain text.

    Descriptions arrive as rendered HTML. They are used for age parsing and
    shown in the UI, so tags have to go — but the *text* must survive intact,
    including across block boundaries.

        >>> strip_html("<p>Ages 6-11.</p>")
        'Ages 6-11.'
        >>> strip_html("<p>One.</p><p>Two.</p>")
        'One. Two.'
        >>> strip_html("") is None
        True
        >>> strip_html(None) is None
        True

    A separator is required rather than optional: without it, "<p>ages</p><p>6-11</p>"
    would collapse to "ages6-11" and the age parser would miss it.

        >>> strip_html("<p>For ages</p><p>8-13</p>")
        'For ages 8-13'
    """
    if not isinstance(value, str) or not value.strip():
        return None
    text = BeautifulSoup(value, "html.parser").get_text(separator=" ")
    collapsed = " ".join(text.split())
    return collapsed or None


def _parse_tec_date(value: Any) -> date | None:
    """Read a `2027-04-05 09:00:00` timestamp as a date.

        >>> _parse_tec_date("2027-04-05 09:00:00")
        datetime.date(2027, 4, 5)
        >>> _parse_tec_date("2027-04-05T09:00:00")
        datetime.date(2027, 4, 5)
        >>> _parse_tec_date("2027-04-05")
        datetime.date(2027, 4, 5)
        >>> _parse_tec_date("") is None
        True

    The plugin reports a `timezone` alongside, but times are dropped: sessions
    key on dates, and the only thing a half-applied timezone can do here is
    move a camp across midnight into the wrong break week.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError:
        return None


def _extract_price(event: dict[str, Any]) -> float | None:
    """Lowest real price for an event.

    Two shapes, checked in order of reliability. `cost_details.values` is a
    parsed list of numbers; `cost` is a display string that may read "$395",
    "395 - 425", "Free", or "" — so the structured field is preferred and the
    string is only a fallback.

    The *lowest* value wins, since camps routinely price as member/non-member
    pairs and the lower bound is the honest headline.

    Returns None rather than 0.0 when nothing usable is found, because the UI
    renders None as "not stated" and 0.0 as free — very different claims. Note
    that a genuine "Free" therefore also lands on None; that is deliberate
    until a provider is seen publishing free camps as a structured zero.
    """
    details = event.get("cost_details")
    if isinstance(details, dict):
        values: list[float] = []
        for raw in details.get("values") or []:
            try:
                number = float(str(raw).replace("$", "").replace(",", "").strip())
            except (TypeError, ValueError):
                continue
            if number > 0:
                values.append(number)
        if values:
            return min(values)

    cost = event.get("cost")
    if isinstance(cost, str) and cost.strip():
        import re

        found = [float(m) for m in re.findall(r"\d+(?:\.\d{1,2})?", cost.replace(",", ""))]
        positive = [n for n in found if n > 0]
        if positive:
            return min(positive)

    return None


class TribeEventsAdapter(Adapter):
    """Pulls camp sessions from a site running The Events Calendar.

    Config keys:
        id, provider_slug   as for every adapter
        base_url            site root, e.g. https://callanwolde.org
        params              dict passed through as query parameters
        per_page            results per request (default 50)
        max_pages           safety ceiling (default 20)
        category_slugs      optional: keep only events in these categories

    `params` is passed through, so anything the plugin documents works without
    touching this file. Confirmed available: `page`, `per_page`, `start_date`,
    `end_date`, `starts_before`, `starts_after`, `ends_before`, `ends_after`,
    `search`, `categories`, `tags`, `venue`, `organizer`, `featured`, `status`,
    `ticketed`.

    Example source:

        - id: callanwolde-tribe
          provider_slug: callanwolde
          adapter: tribe
          base_url: https://callanwolde.org
          params:
            search: camp
            start_date: 2026-08-01
            end_date: 2027-08-31

    Note on filtering: `search` is the plugin's own full-text filter and is
    cheap, but it matches descriptions too. `category_slugs` is applied here
    after fetching and is exact, so prefer it once the provider's category
    slugs are known — `/wp-json/tribe/events/v1/categories` lists them.
    """

    name = "tribe"

    def parse(self, fetcher: Fetcher) -> Iterator[CampSession]:
        base_url = str(self.config.get("base_url") or "").strip()
        if not base_url:
            raise AdapterError(f"{self.source_id}: no 'base_url' configured")

        params: dict[str, Any] = dict(self.config.get("params") or {})
        per_page = int(self.config.get("per_page", DEFAULT_PER_PAGE))
        max_pages = int(self.config.get("max_pages", MAX_PAGES))
        wanted = {str(s).lower() for s in (self.config.get("category_slugs") or [])}

        seen_ids: set[str] = set()
        page = 1
        while page <= max_pages:
            url = build_events_url(base_url, {**params, "per_page": per_page, "page": page})
            payload = self._fetch_json(fetcher, url, page)
            if payload is None:
                break

            events = payload.get("events")
            if not isinstance(events, list) or not events:
                break

            for event in events:
                if not isinstance(event, dict):
                    continue
                # The plugin can return the same event on a page boundary as
                # the archive shifts under us. Dedupe on the post ID.
                event_id = str(event.get("id") or "")
                if event_id and event_id in seen_ids:
                    continue
                if event_id:
                    seen_ids.add(event_id)
                if wanted and not self._in_categories(event, wanted):
                    continue
                session = self._to_session(event)
                if session is not None:
                    yield session

            total_pages = payload.get("total_pages")
            if isinstance(total_pages, int) and page >= total_pages:
                break
            page += 1
        else:
            log.warning(
                "%s: stopped at the %d-page ceiling; narrow 'params' if you expected more",
                self.source_id,
                max_pages,
            )

    def _fetch_json(self, fetcher: Fetcher, url: str, page: int) -> dict[str, Any] | None:
        """Fetch one page. Returns None when the archive has simply run out.

        The plugin answers 404 for a page past the end of the archive, which is
        a normal end-of-pagination signal rather than an error — but only after
        page 1. A 404 on the first page means the endpoint is wrong, and that
        must fail loudly instead of looking like an empty calendar.
        """
        try:
            result = fetcher.get(url)
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            if code == 404 and page > 1:
                return None
            if code == 404:
                raise AdapterError(
                    f"{self.source_id}: no Events Calendar API at this base_url "
                    f"(HTTP 404). Check /wp-json/ lists 'tribe/events/v1'."
                ) from exc
            raise AdapterError(f"{self.source_id}: HTTP {code} from the events API") from exc

        try:
            payload = json.loads(result.text)
        except json.JSONDecodeError as exc:
            raise AdapterError(f"{self.source_id}: events API returned non-JSON ({exc})") from exc
        if not isinstance(payload, dict):
            raise AdapterError(
                f"{self.source_id}: events API returned "
                f"{type(payload).__name__}, expected an object"
            )
        return payload

    @staticmethod
    def _in_categories(event: dict[str, Any], wanted: set[str]) -> bool:
        """Whether an event carries one of the wanted category slugs."""
        for category in event.get("categories") or []:
            if not isinstance(category, dict):
                continue
            slug = str(category.get("slug") or "").lower()
            name = str(category.get("name") or "").lower()
            if slug in wanted or name in wanted:
                return True
        return False

    def _to_session(self, event: dict[str, Any]) -> CampSession | None:
        """Convert one event. Returns None when it can't be a session."""
        start = _parse_tec_date(event.get("start_date"))
        if start is None:
            return None
        end = _parse_tec_date(event.get("end_date")) or start
        if end < start:
            end = start

        title = strip_html(event.get("title")) or ""
        if not title:
            return None

        description = strip_html(event.get("description")) or strip_html(event.get("excerpt"))

        min_age, max_age = parse_age_text(f"{title} {description or ''}")

        url = event.get("url") or event.get("website")
        if not isinstance(url, str) or not url.strip():
            return None

        return CampSession(
            provider_slug=self.provider_slug,
            title=title,
            start_date=start,
            end_date=end,
            min_age=min_age,
            max_age=max_age,
            price_usd=_extract_price(event),
            # The plugin exposes no registration state. UNKNOWN is the honest
            # answer; inferring "open" from the event merely existing is the
            # kind of false confidence this project exists to avoid.
            registration_status=RegistrationStatus.UNKNOWN,
            url=url.strip(),
            description=description,
            source_id=self.source_id,
        )
