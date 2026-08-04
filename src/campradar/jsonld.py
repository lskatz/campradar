"""Read camp sessions from schema.org JSON-LD.

This is the highest-leverage thing to parse and the only parser here. A large
share of camp providers run on platforms (Squarespace, Wix, WordPress with an
events plugin, Sawyer, ACTIVE) that emit `Event` or `Course` markup for SEO
without anyone at the organisation knowing it exists. Reading that markup gets
structured dates and prices with no per-site parsing at all.

Failure policy: loud per source, quiet per row. One listing with an
unparseable date is skipped; a dead site raises.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime
from typing import Any

from bs4 import BeautifulSoup

from .fetch import Fetcher
from .models import CampSession, RegistrationStatus

__all__ = [
    "extract_jsonld_objects",
    "is_event",
    "parse_age_text",
    "parse_page",
    "read_source",
]

log = logging.getLogger(__name__)

# schema.org types worth treating as a camp session.
_EVENT_TYPES = frozenset({"Event", "EventSeries", "Course", "CourseInstance", "SocialEvent"})

# schema.org availability mapped onto our own vocabulary.
_AVAILABILITY = {
    "instock": RegistrationStatus.OPEN,
    "limitedavailability": RegistrationStatus.OPEN,
    "presale": RegistrationStatus.NOT_YET_OPEN,
    "preorder": RegistrationStatus.NOT_YET_OPEN,
    "soldout": RegistrationStatus.FULL,
    "outofstock": RegistrationStatus.FULL,
    "discontinued": RegistrationStatus.CLOSED,
}

# "Ages 6-12", "ages 6 to 12", "6-12 years"
_AGE_RANGE = re.compile(
    r"(?:ages?\s*)?(\d{1,2})\s*(?:-|\u2013|\u2014|to|through)\s*(\d{1,2})\s*(?:years?|yrs?|yo)?",
    re.IGNORECASE,
)
_GRADE_RANGE = re.compile(
    r"(?:rising\s+)?grades?\s*(\d{1,2})\s*(?:-|\u2013|\u2014|to|through)\s*(\d{1,2})",
    re.IGNORECASE,
)
# US convention: a child in grade N is typically N+5 years old.
_GRADE_TO_AGE_OFFSET = 5


def extract_jsonld_objects(html: str) -> list[dict[str, Any]]:
    """Pull every JSON-LD object out of a page, flattening `@graph` wrappers.

    Malformed blocks are skipped silently. Plenty of sites ship one broken
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

        candidates = payload if isinstance(payload, list) else [payload]
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            if isinstance(candidate.get("@graph"), list):
                objects.extend(x for x in candidate["@graph"] if isinstance(x, dict))
            else:
                objects.append(candidate)

    return objects


def is_event(obj: dict[str, Any]) -> bool:
    """Whether an object looks like something you can book a child into."""
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
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None


def parse_age_text(text: str | None) -> tuple[int | None, int | None]:
    """Best-effort age range from free text.

    Grades are converted to ages, since eligibility needs one unit. Returns
    (None, None) when nothing is recognisable, which the model treats as
    permissive rather than as a reason to exclude the session.

    >>> parse_age_text("Ages 6-12, all skill levels")
    (6, 12)
    >>> parse_age_text("For rising grades 3-5")
    (8, 10)
    >>> parse_age_text("A great week outdoors")
    (None, None)
    """
    if not text:
        return None, None

    # Grades are checked FIRST and the order is load-bearing: the age pattern
    # treats its "ages" prefix as optional, so it happily matches the "3-5" in
    # "rising grades 3-5" and would report a camp for eight-year-olds as being
    # for three-year-olds.
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
        # Arrives as "https://schema.org/InStock" or bare "InStock".
        token = str(raw_availability).rsplit("/", 1)[-1].lower()
        status = _AVAILABILITY.get(token, RegistrationStatus.UNKNOWN)

    return price, status


def _to_session(
    obj: dict[str, Any], *, source_id: str, provider_slug: str, fallback_url: str
) -> CampSession | None:
    """Convert one JSON-LD object. Returns None when it is not usable."""
    start = _parse_date(obj.get("startDate"))
    if start is None:
        # Without a start date the record cannot be placed on a calendar and
        # cannot form a stable key, so it is not worth keeping.
        return None
    end = _parse_date(obj.get("endDate")) or start
    if end < start:
        return None

    title = str(obj.get("name") or "").strip()
    if not title:
        return None

    description = obj.get("description")
    description = str(description).strip() if description else None

    # Age hints appear in wildly different places; check the most specific
    # field first, then fall back to scanning prose.
    min_age, max_age = parse_age_text(obj.get("typicalAgeRange"))
    if min_age is None:
        min_age, max_age = parse_age_text(f"{title} {description or ''}")

    price, status = _parse_offer(obj)

    return CampSession(
        provider_slug=provider_slug,
        title=title,
        start_date=start,
        end_date=end,
        min_age=min_age,
        max_age=max_age,
        price_usd=price,
        registration_status=status,
        url=obj.get("url") or fallback_url,
        description=description,
        source_id=source_id,
    )


def parse_page(
    html: str, *, source_id: str, provider_slug: str, fallback_url: str
) -> list[CampSession]:
    """Every usable session on one page. Unusable listings are skipped."""
    sessions: list[CampSession] = []
    for obj in extract_jsonld_objects(html):
        if not is_event(obj):
            continue
        try:
            session = _to_session(
                obj,
                source_id=source_id,
                provider_slug=provider_slug,
                fallback_url=fallback_url,
            )
        except (ValueError, TypeError, KeyError) as exc:
            log.warning("%s: skipped a listing (%s)", source_id, exc)
            continue
        if session is not None:
            sessions.append(session)
    return sessions


def read_source(source: dict[str, Any], fetcher: Fetcher) -> list[CampSession]:
    """Read every URL configured for one source in camps.yaml."""
    source_id = source["id"]
    provider_slug = source.get("provider_slug", source_id)
    urls: list[str] = source.get("urls", [])
    if not urls:
        raise ValueError(f"{source_id}: no 'urls' configured")

    sessions: list[CampSession] = []
    for url in urls:
        result = fetcher.get(url)
        sessions.extend(
            parse_page(
                result.text,
                source_id=source_id,
                provider_slug=provider_slug,
                fallback_url=url,
            )
        )
    return sessions
