"""Data model for Camp Radar.

Everything that reads a website must emit `CampSession` objects. That single
contract is what keeps the mess of one-off provider pages from leaking into
the rest of the program.

Two design notes carry most of the weight:

* `CampSession.key` is a *stable* identity derived from content, not from any
  ID the source site happens to expose. Source IDs churn between seasons and
  are frequently absent; a content hash lets us recognise the same session
  across runs and across sources.
* `first_seen` / `last_seen` are never set here. They belong to `store`, which
  is the only thing that knows about history. A parser describes the world as
  it is right now; it does not remember.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import date, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

__all__ = [
    "CampSession",
    "Provider",
    "RegistrationStatus",
    "SessionRecord",
    "slugify",
]


_NON_ALNUM = re.compile(r"[^a-z0-9]+")

# Filler words providers add or drop between seasons ("Camp Kingfisher Summer
# Camp 2027" vs "Kingfisher Camp"). Stripping them before hashing makes the
# identity survive marketing rewrites.
_NOISE_WORDS = frozenset(
    {
        "camp",
        "camps",
        "summer",
        "winter",
        "spring",
        "fall",
        "break",
        "week",
        "session",
        "the",
        "a",
        "an",
    }
)


def slugify(value: str) -> str:
    """Lowercase ASCII slug, used for identity and URL fragments."""
    normalized = unicodedata.normalize("NFKD", value)
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii")
    return _NON_ALNUM.sub("-", ascii_only.lower()).strip("-")


def title_fingerprint(title: str) -> str:
    """Reduce a session title to its distinguishing words.

    Order is preserved rather than sorted: "Robotics for Girls" and "Girls for
    Robotics" are plausibly different programmes, and collapsing them would
    silently merge two real sessions.
    """
    tokens = [t for t in _NON_ALNUM.split(slugify(title)) if t and t not in _NOISE_WORDS]
    # A title made entirely of noise words ("Summer Camp") still needs to
    # produce something stable, so fall back to the raw slug.
    return "-".join(tokens) or slugify(title)


class RegistrationStatus(StrEnum):
    """How reachable a session is right now.

    `UNKNOWN` is the honest default and by far the most common value. Most
    provider pages simply do not say, and guessing "open" would manufacture
    exactly the false confidence this tool exists to prevent.
    """

    UNKNOWN = "unknown"
    NOT_YET_OPEN = "not_yet_open"
    OPEN = "open"
    WAITLIST = "waitlist"
    FULL = "full"
    CLOSED = "closed"


class Provider(BaseModel):
    """An organisation that runs camps. Roughly one per entry in camps.yaml."""

    model_config = ConfigDict(frozen=True)

    slug: str
    name: str
    homepage: HttpUrl | None = None
    locality: str | None = None


class CampSession(BaseModel):
    """One bookable block of camp: a provider, a date range, an age range.

    This is the unit a parent makes a decision about, which is why it — rather
    than the camp or the provider — is the primary record.
    """

    model_config = ConfigDict(frozen=True)

    provider_slug: str
    title: str
    start_date: date
    end_date: date

    # Ages are the most common eligibility filter and the thing provider pages
    # state most vaguely ("rising 3rd-5th graders"). None means "not stated",
    # which we surface rather than hide.
    min_age: int | None = Field(default=None, ge=0, le=21)
    max_age: int | None = Field(default=None, ge=0, le=21)

    # Times drive the aftercare problem: a camp ending at 15:00 is not a
    # workday of coverage. Plain strings ("09:00") because providers are wildly
    # inconsistent and we would rather round-trip the original.
    daily_start: str | None = None
    daily_end: str | None = None

    price_usd: float | None = Field(default=None, ge=0)
    registration_status: RegistrationStatus = RegistrationStatus.UNKNOWN

    url: HttpUrl | None = None
    description: str | None = None
    source_id: str = Field(description="Which source produced this record.")

    @model_validator(mode="after")
    def _check_ranges(self) -> CampSession:
        if self.end_date < self.start_date:
            raise ValueError(f"end_date {self.end_date} precedes start_date {self.start_date}")
        if self.min_age is not None and self.max_age is not None and self.max_age < self.min_age:
            raise ValueError(f"max_age {self.max_age} below min_age {self.min_age}")
        return self

    @property
    def key(self) -> str:
        """Content-derived identity, stable across runs and across sources.

        Deliberately excludes price, status and description: those are the
        fields most likely to change on a session that is otherwise the same
        one. Including them would make every price tweak look like a brand-new
        camp and flood the new-this-week column with noise.
        """
        material = (
            f"{self.provider_slug}|{title_fingerprint(self.title)}|{self.start_date.isoformat()}"
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]

    @property
    def duration_days(self) -> int:
        """Inclusive length in days. A one-day camp returns 1, not 0."""
        return (self.end_date - self.start_date).days + 1

    def covers(self, day: date) -> bool:
        """Whether this session provides childcare on `day`."""
        return self.start_date <= day <= self.end_date


class SessionRecord(BaseModel):
    """A `CampSession` plus the history the store maintains for it.

    Keeping this separate from `CampSession` stops a parser from fabricating a
    `first_seen`, and makes the persisted format self-describing.
    """

    model_config = ConfigDict(frozen=True)

    key: str
    session: CampSession
    first_seen: datetime = Field(description="UTC timestamp of the run that first saw this key.")
    last_seen: datetime = Field(description="UTC timestamp of the most recent run that saw it.")
