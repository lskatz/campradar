"""Shared fixtures. Nothing here touches the network or the real clock."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from campradar.models import CampSession, RegistrationStatus
from campradar.store import NeededRange

FIXTURES = Path(__file__).parent / "fixtures"

#: A pinned clock. Every test that needs "now" uses this, so no assertion in
#: the suite can depend on when it was run.
RUN_ONE = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
RUN_TWO = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)


@pytest.fixture
def control_html() -> str:
    return (FIXTURES / "example_camps.html").read_text(encoding="utf-8")


@pytest.fixture
def control_expected() -> dict:
    return json.loads((FIXTURES / "example_camps.expected.json").read_text(encoding="utf-8"))


@pytest.fixture
def ranges() -> list[NeededRange]:
    """The needed ranges the control fixture is written against."""
    return [
        NeededRange("fall-break", "Fall Break", date(2026, 10, 5), date(2026, 10, 9)),
        NeededRange("winter-break", "Winter Break", date(2026, 12, 21), date(2027, 1, 4)),
        NeededRange("february-break", "February Break", date(2027, 2, 15), date(2027, 2, 19)),
    ]


def make_session(
    title: str = "Test Camp",
    *,
    provider: str = "test-provider",
    start: date = date(2026, 10, 5),
    end: date | None = None,
    status: RegistrationStatus = RegistrationStatus.UNKNOWN,
    **kwargs,
) -> CampSession:
    """Terse session builder, so tests state only what they care about."""
    return CampSession(
        provider_slug=provider,
        title=title,
        start_date=start,
        end_date=end or start,
        registration_status=status,
        source_id="test-source",
        **kwargs,
    )
