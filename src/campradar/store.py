"""The local file, the needed dates, and the diff between runs.

Three jobs live here, in dependency order:

1. Read `dates.yaml` into `NeededRange` objects.
2. Work out, for one session, which ranges it overlaps and exactly which needed
   days it covers. Pure function of a session and a list of ranges.
3. Merge a fresh scrape into prior state, preserving `first_seen`, and report
   what changed.

None of it touches the network, and the only clock read is the one passed in,
which is what makes it straightforward to test.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import yaml

from .models import CampSession, RegistrationStatus, SessionRecord

__all__ = [
    "DeltaReport",
    "NeededRange",
    "coverage",
    "load_needed_ranges",
    "load_state",
    "merge",
    "save_state",
]


# Statuses meaning "you can act on this right now". A session crossing into one
# of these is worth interrupting someone's week for; other transitions are not.
_ACTIONABLE = frozenset({RegistrationStatus.OPEN, RegistrationStatus.WAITLIST})


# --------------------------------------------------------------------------
# needed dates
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class NeededRange:
    """A stretch of days childcare is needed for — usually a school break.

    `slug` is the stable handle you filter on; `name` is display text. They are
    kept separate on purpose, because names get retyped every year and slugs
    must not move when they do.

    `end` is INCLUSIVE and means the last day off.
    """

    slug: str
    name: str
    start: date
    end: date

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise ValueError(f"{self.slug}: end {self.end} precedes start {self.start}")

    def days(self) -> list[date]:
        """Every day in the range, inclusive of both ends."""
        span = (self.end - self.start).days
        return [self.start + timedelta(days=i) for i in range(span + 1)]


def load_needed_ranges(path: Path) -> list[NeededRange]:
    """Read `dates.yaml`. Order is preserved so output columns are stable."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    ranges = [
        NeededRange(
            slug=entry["slug"],
            name=entry.get("name", entry["slug"]),
            start=entry["start"],
            end=entry["end"],
        )
        for entry in raw.get("breaks", [])
    ]

    seen: set[str] = set()
    for item in ranges:
        if item.slug in seen:
            raise ValueError(f"duplicate slug in {path}: {item.slug}")
        seen.add(item.slug)
    return ranges


def coverage(session: CampSession, ranges: list[NeededRange]) -> tuple[list[str], list[date]]:
    """Which needed ranges a session touches, and which needed days it covers.

    The two are not the same thing and the difference is the point: a Monday-to
    -Friday camp overlapping a three-day break covers three needed days, not
    five. Returning both lets you ask either question.

    Returns (range slugs, needed days), both sorted and deduplicated.
    """
    slugs: list[str] = []
    days: set[date] = set()
    for item in ranges:
        covered = [day for day in item.days() if session.covers(day)]
        if covered:
            slugs.append(item.slug)
            days.update(covered)
    return sorted(slugs), sorted(days)


# --------------------------------------------------------------------------
# merge
# --------------------------------------------------------------------------


@dataclass(slots=True)
class DeltaReport:
    """What changed between two runs."""

    new: list[SessionRecord] = field(default_factory=list)
    newly_open: list[SessionRecord] = field(default_factory=list)
    disappeared: list[SessionRecord] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (self.new or self.newly_open or self.disappeared)

    def summary(self) -> str:
        if self.is_empty:
            return "no changes"
        return (
            f"{len(self.new)} new, "
            f"{len(self.newly_open)} newly open, "
            f"{len(self.disappeared)} disappeared"
        )


def merge(
    previous: dict[str, SessionRecord],
    scraped: list[CampSession],
    *,
    now: datetime | None = None,
) -> tuple[dict[str, SessionRecord], DeltaReport]:
    """Fold freshly fetched sessions into prior state.

    Args:
        previous: State from the last run, keyed by `CampSession.key`.
        scraped: Everything this run found. May contain duplicates — the same
            session reached through two sources — which collapse by key, with
            the first writer winning.
        now: Injected clock. Tests pass a fixed value; the CLI passes the same
            value it later hands to `save_state`.

    Records absent from `scraped` are *retained* with their old `last_seen`
    rather than deleted. A session vanishing is itself information (it usually
    means sold out and delisted), and deleting it would make it reappear as
    brand new if the provider restores the page.
    """
    now = now or datetime.now(UTC)
    report = DeltaReport()
    merged: dict[str, SessionRecord] = {}
    seen_this_run: set[str] = set()

    for session in scraped:
        key = session.key
        if key in seen_this_run:
            # Cross-source duplicate. Keep the first, which — because sources
            # are processed in the order given in camps.yaml — means the more
            # authoritative source wins if it is listed first.
            continue
        seen_this_run.add(key)

        prior = previous.get(key)
        if prior is None:
            record = SessionRecord(key=key, session=session, first_seen=now, last_seen=now)
            merged[key] = record
            report.new.append(record)
            continue

        record = SessionRecord(
            key=key,
            session=session,
            first_seen=prior.first_seen,  # preserved: this is the whole point
            last_seen=now,
        )
        merged[key] = record

        became_actionable = (
            prior.session.registration_status not in _ACTIONABLE
            and session.registration_status in _ACTIONABLE
        )
        if became_actionable:
            report.newly_open.append(record)

    for key, prior in previous.items():
        if key not in seen_this_run:
            merged[key] = prior
            report.disappeared.append(prior)

    return merged, report


# --------------------------------------------------------------------------
# persistence
# --------------------------------------------------------------------------


def load_state(path: Path) -> tuple[dict[str, SessionRecord], datetime | None]:
    """Read the local file. A missing file is a first run, not an error.

    Returns the records plus the timestamp of the run that wrote them, which is
    what `list` compares `first_seen` against to decide what is new.
    """
    if not path.exists():
        return {}, None
    raw = json.loads(path.read_text(encoding="utf-8"))
    records = {item["key"]: SessionRecord.model_validate(item) for item in raw["sessions"]}
    generated_at = raw.get("generated_at")
    return records, datetime.fromisoformat(generated_at) if generated_at else None


def save_state(path: Path, state: dict[str, SessionRecord], *, now: datetime | None = None) -> None:
    """Write state as sorted, indented JSON.

    Sorting and indenting are not cosmetic. This file is committed, and a
    stable serialisation means its git diff shows genuine changes rather than
    dictionary reordering.
    """
    now = now or datetime.now(UTC)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": now.isoformat(),
        "sessions": [json.loads(state[key].model_dump_json()) for key in sorted(state)],
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
