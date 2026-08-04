"""The positive control.

One saved page, one frozen list of what it must produce. If this fails,
nothing else in the suite is worth reading.
"""

from __future__ import annotations

from datetime import date

import pytest

from campradar.jsonld import parse_page
from campradar.store import NeededRange, coverage


@pytest.fixture
def parsed(control_html, control_expected):
    return parse_page(
        control_html,
        source_id=control_expected["source_id"],
        provider_slug=control_expected["provider_slug"],
        fallback_url=control_expected["fallback_url"],
    )


def test_control_yields_exactly_the_expected_sessions(parsed, control_expected):
    """Count and order both matter: order decides which duplicate wins."""
    assert [s.title for s in parsed] == [e["title"] for e in control_expected["sessions"]]


def test_control_skips_the_four_kinds_of_junk(parsed):
    """No start date, wrong type, broken JSON, and a non-event inside @graph."""
    titles = {s.title for s in parsed}
    assert "Registration Opens Soon" not in titles  # no startDate
    assert "Example Camps" not in titles  # Organization, not an Event
    assert "Broken Block" not in titles  # malformed JSON
    assert "Camps & Programs" not in titles  # WebPage inside @graph


@pytest.mark.parametrize("index", range(4))
def test_control_fields(parsed, control_expected, index):
    got = parsed[index]
    want = control_expected["sessions"][index]

    assert got.title == want["title"]
    assert got.start_date.isoformat() == want["start_date"]
    assert got.end_date.isoformat() == want["end_date"]
    assert got.min_age == want["min_age"]
    assert got.max_age == want["max_age"]
    assert got.price_usd == want["price_usd"]
    assert got.registration_status.value == want["registration_status"]
    assert str(got.url) == want["url"]


@pytest.mark.parametrize("index", range(4))
def test_control_keys_are_frozen(parsed, control_expected, index):
    """A changed key silently re-reports the whole catalogue as new.

    This is the most expensive failure mode in the project, so it gets its own
    assertion rather than riding along with the other fields.
    """
    assert parsed[index].key == control_expected["sessions"][index]["key"]


@pytest.mark.parametrize("index", range(4))
def test_control_coverage(parsed, control_expected, ranges, index):
    slugs, days = coverage(parsed[index], ranges)
    want = control_expected["sessions"][index]

    assert slugs == want["breaks"]
    assert [d.isoformat() for d in days] == want["needed_days"]


def test_coverage_counts_needed_days_not_session_days(parsed):
    """The distinction the two columns exist for.

    Presidents Week Art Studio runs Saturday to Wednesday — five days — but
    only three of those are days off.
    """
    art = next(s for s in parsed if s.title == "Presidents Week Art Studio")
    assert art.duration_days == 5

    february = NeededRange("february-break", "February Break", date(2027, 2, 15), date(2027, 2, 19))
    _, days = coverage(art, [february])
    assert len(days) == 3
