"""Change tracking: the part that actually solves the discovery problem.

A catalogue of every camp in DeKalb County is not useful — it is too long to
read and it looks the same every week. What is useful is the *diff*: which
sessions appeared since last time, and which ones just opened for registration.

This module is deliberately storage-agnostic and pure. `merge()` takes the old
state and the freshly scraped state and returns the new state plus a summary of
what changed. No I/O, no clock reads except the one passed in — which is what
makes it straightforward to test.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .models import CampSession, RegistrationStatus, SessionRecord

__all__ = ["DeltaReport", "merge", "load_state", "save_state"]


# Statuses that mean "you can act on this right now". A session crossing into
# one of these is worth interrupting someone's week for; other transitions are
# not.
_ACTIONABLE = frozenset({RegistrationStatus.OPEN, RegistrationStatus.WAITLIST})


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
        """One-line summary, used in Actions logs and the digest subject."""
        if self.is_empty:
            return "No changes."
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
    """Fold freshly scraped sessions into prior state.

    Args:
        previous: State from the last run, keyed by `CampSession.key`.
        scraped: Everything this run found, across all adapters. May contain
            duplicates — the same session reached via two sources — which are
            collapsed by key, first writer winning.
        now: Injected clock. Tests pass a fixed value; production passes None.

    Returns:
        The new state and a report of what changed.

    Note that records absent from `scraped` are *retained* with their old
    `last_seen`. See `SessionRecord.is_stale` for why we don't delete.
    """
    now = now or datetime.now(timezone.utc)
    report = DeltaReport()
    merged: dict[str, SessionRecord] = {}

    seen_this_run: set[str] = set()
    for session in scraped:
        key = session.key
        if key in seen_this_run:
            # Cross-source duplicate. Keep the first, which — because sources
            # are processed in the order given in sources.yaml — means the
            # more authoritative source wins if it is listed first.
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
            first_seen=prior.first_seen,  # preserved: this is the point of the module
            last_seen=now,
        )
        merged[key] = record

        became_actionable = (
            prior.session.registration_status not in _ACTIONABLE
            and session.registration_status in _ACTIONABLE
        )
        if became_actionable:
            report.newly_open.append(record)

    # Carry forward anything this run didn't see, untouched.
    for key, prior in previous.items():
        if key not in seen_this_run:
            merged[key] = prior
            report.disappeared.append(prior)

    return merged, report


# --------------------------------------------------------------------------
# persistence
# --------------------------------------------------------------------------


def load_state(path: Path) -> dict[str, SessionRecord]:
    """Read prior state. A missing file is a first run, not an error."""
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {item["key"]: SessionRecord.model_validate(item) for item in raw["sessions"]}


def save_state(path: Path, state: dict[str, SessionRecord]) -> None:
    """Write state as sorted, indented JSON.

    Sorting and indenting are not cosmetic: this file is committed by CI, and a
    stable serialisation means the git diff of a run shows only genuine
    changes rather than dictionary reordering.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sessions": [
            json.loads(state[key].model_dump_json()) for key in sorted(state)
        ],
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8")
