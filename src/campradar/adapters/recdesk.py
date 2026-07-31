"""Reads programmes from a RecDesk Community portal.

Why the page itself cannot be scraped
-------------------------------------
`tucker.recdesk.com/Community/Program?category=9` serves 163 KB of chrome with
an empty programme table — the rows say "Loading..." and "No results found"
because they arrive later over XHR. Worse, the sidebar counts are session
state: two GETs seconds apart reported 8 and then 76 Day Camp programmes,
because RecDesk keys the active filter to `ASP.NET_SessionId`. A stateless GET
therefore lands in a fresh session whose default filter resolves to nothing,
which is exactly why the `jsonld` adapter reported this source as "fetched
cleanly but produced 0 sessions" rather than failing.

The real endpoint
-----------------
    POST /Community/Program/FilterPrograms
    Content-Type: application/json
    X-Requested-With: XMLHttpRequest

    {"ProgramName":"", "ProgramType":"9", "Age":"", "Facility":"0",
     "Days":"0", "Pagination":{"CurrentPageIndex":1,"LoadMore":true}, ...}

It answers with an **HTML fragment**, not JSON — the browser sends
`Accept: text/html, */*` and injects the result. So this adapter posts a filter
and parses markup, but the markup it parses is only the rows, with none of the
navigation that makes full-page scraping fragile.

Parsing strategy, and why it is text-shaped
-------------------------------------------
The fragment's CSS classes are RecDesk's private business and will change
without notice. What is far more stable is the visible field vocabulary —
`Dates`, `Days`, `Ages`, `Grades`, `Openings`, `Remaining` — because those are
the column headings users read. So this parser locates each programme by its
anchor (a fragment contains only programme links, no nav) and then reads
labelled values out of the surrounding text by pattern.

Failure is loud on purpose
--------------------------
If the fragment contains programme anchors but none of them yield a date, that
is a layout change and raises `AdapterError`. It must *not* return an empty
list: "the markup moved" and "there are no camps this month" are the two
conditions this project has repeatedly confused, and a silent zero is how a
broken source hides for a season.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from typing import Any

from bs4 import BeautifulSoup

from ..fetch import Fetcher
from ..models import CampSession, RegistrationStatus
from .base import Adapter, AdapterError

__all__ = ["RecDeskAdapter", "ProgramRow", "parse_fragment", "parse_date_range", "parse_age_range"]

log = logging.getLogger(__name__)

FILTER_PATH = "/Community/Program/FilterPrograms"

#: The browser sends these; RecDesk returns the full page without the XHR
#: marker, which would parse as zero programmes and look like an empty result.
XHR_HEADERS = {
    "X-Requested-With": "XMLHttpRequest",
    "Accept": "text/html, */*; q=0.01",
}

MAX_PAGES = 25

_DATE_RANGE = re.compile(r"(\d{1,2}/\d{1,2}/\d{4})\s*[-–]\s*(\d{1,2}/\d{1,2}/\d{4})")
_SINGLE_DATE = re.compile(r"(\d{1,2}/\d{1,2}/\d{4})")
#: "5y - 12y", "7y - 12y 0m", "5y 6m - 12y"
_AGE_RANGE = re.compile(r"(\d{1,2})\s*y(?:\s*\d{1,2}\s*m)?\s*[-–]\s*(\d{1,2})\s*y", re.I)


@dataclass(frozen=True)
class ProgramRow:
    """One programme as read off the fragment, before it becomes a session."""

    title: str
    url: str | None
    start: date | None
    end: date | None
    min_age: int | None
    max_age: int | None
    status: RegistrationStatus
    category: str | None = None


def parse_date_range(text: str) -> tuple[date | None, date | None]:
    """Read RecDesk's `M/D/YYYY - M/D/YYYY` range.

        >>> parse_date_range("Dates 7/27/2026 - 7/31/2026")
        (datetime.date(2026, 7, 27), datetime.date(2026, 7, 31))

    A single date means a one-day programme, which is a legitimate camp day:

        >>> parse_date_range("11/26/2026")
        (datetime.date(2026, 11, 26), datetime.date(2026, 11, 26))

        >>> parse_date_range("no dates here")
        (None, None)
    """

    def read(raw: str) -> date | None:
        month, day, year = (int(part) for part in raw.split("/"))
        try:
            return date(year, month, day)
        except ValueError:
            return None

    if match := _DATE_RANGE.search(text):
        return read(match.group(1)), read(match.group(2))
    if match := _SINGLE_DATE.search(text):
        one = read(match.group(1))
        return one, one
    return None, None


def parse_age_range(text: str) -> tuple[int | None, int | None]:
    """Read RecDesk's `5y - 12y` age band, ignoring the months component.

        >>> parse_age_range("Ages 5y - 12y")
        (5, 12)
        >>> parse_age_range("Ages 7y - 12y 0m")
        (7, 12)
        >>> parse_age_range("Ages -")
        (None, None)

    Months are dropped rather than rounded. `CampSession.suits_age` compares
    whole years, and a child who is 6y11m is six for every purpose this tool
    has; inventing precision the model cannot use would only make the bound
    look more authoritative than it is.
    """
    if match := _AGE_RANGE.search(text):
        low, high = int(match.group(1)), int(match.group(2))
        if low > high:
            low, high = high, low
        return low, high
    return None, None


def parse_status(text: str) -> RegistrationStatus:
    """Map the badges and the `Remaining` column onto a status.

        >>> parse_status("Remaining FULL")
        <RegistrationStatus.FULL: 'full'>
        >>> parse_status("Registration ended on 4/30/2026")
        <RegistrationStatus.CLOSED: 'closed'>
        >>> parse_status("Registration opens on 1/5/2027")
        <RegistrationStatus.NOT_YET_OPEN: 'not_yet_open'>
        >>> parse_status("Openings 30 Remaining 12")
        <RegistrationStatus.OPEN: 'open'>

    Ordering matters: a full camp whose registration has also closed is FULL,
    because that is the fact a parent acts on. Unknown shapes stay UNKNOWN
    rather than being optimistically called open — this project would rather
    show a camp with no status than imply availability it has not seen.

        >>> parse_status("something else entirely")
        <RegistrationStatus.UNKNOWN: 'unknown'>
    """
    lowered = text.lower()
    if "full" in lowered or "sold out" in lowered:
        return RegistrationStatus.FULL
    if "waitlist" in lowered or "wait list" in lowered:
        return RegistrationStatus.WAITLIST
    if "registration ended" in lowered or "registration closed" in lowered:
        return RegistrationStatus.CLOSED
    if "registration opens" in lowered or "registration begins" in lowered:
        return RegistrationStatus.NOT_YET_OPEN
    if re.search(r"remaining\s*:?\s*\d+", lowered):
        return RegistrationStatus.OPEN
    return RegistrationStatus.UNKNOWN


def _record_text(anchor: Any) -> str:
    """Text of the smallest ancestor that actually contains the labelled fields.

    Walking up from the title rather than assuming a container class is what
    keeps this working when RecDesk restyles: the relationship "the dates are
    near the title" is a fact about the page's meaning, whereas
    `div.program-row` is a fact about this month's stylesheet.
    """
    node = anchor
    for _ in range(6):
        node = getattr(node, "parent", None)
        if node is None:
            break
        text = node.get_text(" ", strip=True)
        if "Dates" in text or _DATE_RANGE.search(text):
            return text
    return anchor.get_text(" ", strip=True)


def parse_fragment(html: str, base_url: str = "") -> list[ProgramRow]:
    """Pull programme rows out of one FilterPrograms response.

    Returns an empty list when the fragment genuinely holds no programmes; the
    caller distinguishes that from a layout change by checking whether anchors
    were present at all.
    """
    soup = BeautifulSoup(html, "html.parser")
    rows: list[ProgramRow] = []

    for anchor in soup.find_all("a", href=True):
        title = anchor.get_text(" ", strip=True)
        if not title:
            continue
        href = str(anchor["href"])
        # Category filter links and cart controls also appear as anchors; a
        # programme link points at a specific programme, not back at a filter.
        if "?category=" in href or "type=" in href or href.startswith("#"):
            continue
        if "/Program" not in href and "/Activity" not in href:
            continue

        text = _record_text(anchor)
        start, end = parse_date_range(text)
        min_age, max_age = parse_age_range(text)
        url = href if href.startswith("http") else f"{base_url.rstrip('/')}{href}"

        rows.append(
            ProgramRow(
                title=title,
                url=url,
                start=start,
                end=end,
                min_age=min_age,
                max_age=max_age,
                status=parse_status(text),
            )
        )
    return rows


def count_anchors(html: str) -> int:
    """How many programme-shaped anchors the fragment holds, parsed or not.

    Used to tell "no camps" from "markup moved" — the distinction that decides
    whether a source reports empty or fails.
    """
    return len(parse_fragment(html))


class RecDeskAdapter(Adapter):
    """Reads a RecDesk Community portal's programme list.

    Config keys:
        base_url        e.g. https://tucker.recdesk.com
        categories      list of RecDesk ProgramType ids, as strings
        params          optional extra keys merged into the POST body
        max_pages       safety ceiling (default 25)

    Example source:

        - id: tucker-rec
          provider_slug: tucker-rec
          adapter: recdesk
          base_url: https://tucker.recdesk.com
          categories: ["9", "20"]
    """

    name = "recdesk"

    def _body(self, category: str, page: int) -> dict[str, Any]:
        """The POST body, shaped exactly as the portal's own JavaScript sends it.

        Every key is present even when empty because RecDesk's model binder
        expects the whole object; omitting a field has been known to make
        ASP.NET fall back to session state, which is the behaviour this adapter
        exists to route around.
        """
        body = {
            "ProgramName": "",
            "Code": "",
            "ProgramNameXS": "",
            "DateRangeSelection": "",
            "DateRangeFrom": "",
            "DateRangeTo": "",
            "ProgramType": str(category),
            "Age": "",
            "Facility": "0",
            "Days": "0",
            "Pagination": {"CurrentPageIndex": page, "LoadMore": True},
        }
        body.update(self.config.get("params") or {})
        return body

    def parse(self, fetcher: Fetcher) -> Iterator[CampSession]:
        base_url = str(self.config.get("base_url") or "").rstrip("/")
        if not base_url:
            raise AdapterError(f"{self.source_id}: 'base_url' is required")

        categories = self.config.get("categories") or []
        if not categories:
            raise AdapterError(
                f"{self.source_id}: no 'categories' configured. Refusing to guess — "
                f"run `campradar recdesk-discover {base_url}` to list them."
            )

        url = f"{base_url}{FILTER_PATH}"
        max_pages = int(self.config.get("max_pages", MAX_PAGES))
        seen: set[tuple[str, date | None]] = set()
        anchors_seen = 0
        dated = 0

        for category in categories:
            for page in range(1, max_pages + 1):
                result = fetcher.post_json(
                    url, self._body(str(category), page), headers=XHR_HEADERS
                )
                rows = parse_fragment(result.text, base_url)
                if not rows:
                    break
                anchors_seen += len(rows)

                new_on_page = 0
                for row in rows:
                    key = (row.title, row.start)
                    if key in seen:
                        continue
                    seen.add(key)
                    new_on_page += 1
                    if row.start is None:
                        # Undated rows cannot be placed on a break calendar.
                        # Same rule as every other adapter here.
                        continue
                    dated += 1
                    if row.url is None:
                        continue
                    yield CampSession(
                        provider_slug=self.provider_slug,
                        title=row.title,
                        start_date=row.start,
                        end_date=row.end or row.start,
                        min_age=row.min_age,
                        max_age=row.max_age,
                        registration_status=row.status,
                        url=row.url,
                        source_id=self.source_id,
                    )

                # RecDesk keeps answering past the last page; a page whose rows
                # we have all seen is the real end.
                if new_on_page == 0:
                    break
            else:
                log.warning(
                    "%s: hit the %d-page ceiling on category %s",
                    self.source_id,
                    max_pages,
                    category,
                )

        if anchors_seen and not dated:
            raise AdapterError(
                f"{self.source_id}: found {anchors_seen} programme link(s) but could not "
                f"read a date from any of them. RecDesk's markup has probably changed — "
                f"re-record tests/fixtures/recdesk_*.html with "
                f"`campradar recdesk-discover {base_url} --save`."
            )
