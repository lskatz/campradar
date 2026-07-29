"""Adapter for sites that publish schema.org JSON-LD.

This is the single highest-leverage adapter in the project. A large share of
camp providers run on platforms (Squarespace, Wix, WordPress with an events
plugin, Sawyer, ACTIVE) that emit `Event`, `EventSeries` or `Course` markup for
SEO without anyone at the organisation knowing it exists. Reading that markup
gets structured dates and prices with no per-site parsing at all.

Try this adapter first on any new source. Only write bespoke parsing when
`campradar sources probe <url>` reports no usable JSON-LD.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterator
from datetime import date, datetime
from typing import Any

from bs4 import BeautifulSoup

from ..fetch import Fetcher
from ..models import CampSession, RegistrationStatus
from .base import Adapter

__all__ = ["JsonLdAdapter", "extract_jsonld_objects", "is_event", "parse_age_text"]

log = logging.getLogger(__name__)

# schema.org types worth treating as a camp session.
_EVENT_TYPES = frozenset({"Event", "EventSeries", "Course", "CourseInstance", "SocialEvent"})

# schema.org availability values mapped onto our own vocabulary.
_AVAILABILITY = {
    "instock": RegistrationStatus.OPEN,
    "limitedavailability": RegistrationStatus.OPEN,
    "presale": RegistrationStatus.NOT_YET_OPEN,
    "preorder": RegistrationStatus.NOT_YET_OPEN,
    "soldout": RegistrationStatus.FULL,
    "outofstock": RegistrationStatus.FULL,
    "discontinued": RegistrationStatus.CLOSED,
}

# "Ages 6-12", "ages 6 to 12", "6–12 years", "rising grades 3-5"
_AGE_RANGE = re.compile(
    r"(?:ages?\s*)?(\d{1,2})\s*(?:-|–|—|to|through)\s*(\d{1,2})\s*(?:years?|yrs?|yo)?",
    re.IGNORECASE,
)
_GRADE_RANGE = re.compile(
    r"(?:rising\s+)?grades?\s*(\d{1,2})\s*(?:-|–|—|to|through)\s*(\d{1,2})", re.IGNORECASE
)
# US convention: a child in grade N is typically N+5 years old.
_GRADE_TO_AGE_OFFSET = 5


def extract_jsonld_objects(html: str) -> list[dict[str, Any]]:
    """Pull every JSON-LD object out of a page, flattening @graph wrappers.

    Malformed blocks are skipped silently — plenty of sites ship one broken
    script tag alongside three good ones, and refusing the whole page over it
    would lose real data.
    """
    soup = BeautifulSoup(html, "html.parser")
    objects: list[dict[str, Any]] = []

    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        if not tag.string:
            continue
        try:
            payload = json.loads(tag.string)
        except json.JSONDecodeError:
            log.debug("skipping malformed JSON-LD block")
            continue

        # A block may be a single object, a list, or a @graph container.
        candidates = payload if isinstance(payload, list) else [payload]
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            if "@graph" in candidate and isinstance(candidate["@graph"], list):
                objects.extend(x for x in candidate["@graph"] if isinstance(x, dict))
            else:
                objects.append(candidate)

    return objects


def is_event(obj: dict[str, Any]) -> bool:
    """Whether a JSON-LD object looks like something we can book a child into."""
    raw_type = obj.get("@type", "")
    types = raw_type if isinstance(raw_type, list) else [raw_type]
    return any(t in _EVENT_TYPES for t in types)


def _parse_date(value: Any) -> date | None:
    """Read a schema.org date or datetime, tolerating a trailing Z."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        # Some sites emit a bare date with junk appended.
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None


def parse_age_text(text: str | None) -> tuple[int | None, int | None]:
    """Best-effort age range from free text.

    Grades are converted to ages, since eligibility filtering needs one unit.
    Returns (None, None) when nothing is recognisable, which the model treats
    as permissive rather than excluding the session.

    >>> parse_age_text("Ages 6-12, all skill levels")
    (6, 12)
    >>> parse_age_text("For rising grades 3-5")
    (8, 10)
    >>> parse_age_text("A great week outdoors")
    (None, None)
    """
    if not text:
        return None, None

    # Grades are checked FIRST and this order is load-bearing. The age pattern
    # treats its "ages" prefix as optional, so it happily matches the "3-5" in
    # "rising grades 3-5" and would report a robotics camp for eight-year-olds
    # as being for three-year-olds.
    if match := _GRADE_RANGE.search(text):
        low = int(match.group(1)) + _GRADE_TO_AGE_OFFSET
        high = int(match.group(2)) + _GRADE_TO_AGE_OFFSET
        if 0 <= low <= high <= 21:
            return low, high

    if match := _AGE_RANGE.search(text):
        low, high = int(match.group(1)), int(match.group(2))
        if 0 <= low <= high <= 21:
            return low, high

    return None, None


def _parse_offer(obj: dict[str, Any]) -> tuple[float | None, RegistrationStatus]:
    """Read price and availability from an `offers` block."""
    offers = obj.get("offers")
    if isinstance(offers, list):
        offers = offers[0] if offers else None
    if not isinstance(offers, dict):
        return None, RegistrationStatus.UNKNOWN

    price: float | None = None
    raw_price = offers.get("price", offers.get("lowPrice"))
    if raw_price is not None:
        try:
            price = float(str(raw_price).replace("$", "").replace(",", "").strip())
        except ValueError:
            price = None

    status = RegistrationStatus.UNKNOWN
    if raw_availability := offers.get("availability"):
        # Values arrive as "https://schema.org/InStock" or bare "InStock".
        token = str(raw_availability).rsplit("/", 1)[-1].lower()
        status = _AVAILABILITY.get(token, RegistrationStatus.UNKNOWN)

    return price, status


class JsonLdAdapter(Adapter):
    """Reads sessions from schema.org markup on one or more listing pages.

    Config keys:
        id:            source identifier
        provider_slug: which provider these sessions belong to
        urls:          list of page URLs to read
    """

    name = "jsonld"

    def parse(self, fetcher: Fetcher) -> Iterator[CampSession]:
        urls: list[str] = self.config.get("urls", [])
        if not urls:
            raise ValueError(f"{self.source_id}: no 'urls' configured")

        for url in urls:
            result = fetcher.get(url)
            for obj in extract_jsonld_objects(result.text):
                if not is_event(obj):
                    continue
                session = self._to_session(obj, fallback_url=url)
                if session is not None:
                    yield session

    def _to_session(self, obj: dict[str, Any], *, fallback_url: str) -> CampSession | None:
        """Convert one JSON-LD event. Returns None if it lacks a usable date."""
        start = _parse_date(obj.get("startDate"))
        if start is None:
            # Without a start date the record cannot be placed on a break
            # calendar and cannot form a stable key, so it is not worth keeping.
            return None
        end = _parse_date(obj.get("endDate")) or start

        title = str(obj.get("name") or "").strip()
        if not title:
            return None

        description = obj.get("description")
        description = str(description).strip() if description else None

        # Age hints appear in wildly different places; check the most specific
        # field first and fall back to scanning prose.
        min_age, max_age = parse_age_text(obj.get("typicalAgeRange"))
        if min_age is None:
            min_age, max_age = parse_age_text(f"{title} {description or ''}")

        price, status = _parse_offer(obj)

        return CampSession(
            provider_slug=self.provider_slug,
            title=title,
            start_date=start,
            end_date=end,
            min_age=min_age,
            max_age=max_age,
            price_usd=price,
            registration_status=status,
            url=obj.get("url") or fallback_url,
            description=description,
            source_id=self.source_id,
        )
