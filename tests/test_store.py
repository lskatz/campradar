"""Merge, needed dates, and persistence.

The merge control below is the second positive control: a fixed pair of runs
with a pinned clock, asserting the exact diff and that history survived.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from campradar.models import RegistrationStatus
from campradar.store import (
    NeededRange,
    coverage,
    load_needed_ranges,
    load_state,
    merge,
    save_state,
)
from conftest import RUN_ONE, RUN_TWO, make_session

# --------------------------------------------------------------------------
# the merge control
# --------------------------------------------------------------------------


@pytest.fixture
def two_runs():
    """Run one sees three camps; run two sees a changed world.

    Kept:        unchanged, still unknown
    Newly open:  opens, which was not_yet_open and is now open
    Disappeared: vanished, absent from run two
    New:         arrival, seen for the first time
    """
    run_one = [
        make_session("Unchanged"),
        make_session("Opens", status=RegistrationStatus.NOT_YET_OPEN),
        make_session("Vanished"),
    ]
    run_two = [
        make_session("Unchanged"),
        make_session("Opens", status=RegistrationStatus.OPEN),
        make_session("Arrival"),
    ]
    first, _ = merge({}, run_one, now=RUN_ONE)
    second, report = merge(first, run_two, now=RUN_TWO)
    return first, second, report


def test_merge_control_reports_exactly_one_of_each(two_runs):
    _, _, report = two_runs
    assert report.summary() == "1 new, 1 newly open, 1 disappeared"


def test_merge_control_preserves_first_seen(two_runs):
    """The entire point of the local file. A camp seen twice is not new."""
    _, second, _ = two_runs
    unchanged = next(r for r in second.values() if r.session.title == "Unchanged")
    assert unchanged.first_seen == RUN_ONE
    assert unchanged.last_seen == RUN_TWO


def test_merge_control_retains_the_disappeared(two_runs):
    """Deleting it would make it reappear as new if the page comes back."""
    _, second, _ = two_runs
    vanished = next(r for r in second.values() if r.session.title == "Vanished")
    assert vanished.last_seen == RUN_ONE
    assert len(second) == 4


def test_merge_control_new_record_starts_now(two_runs):
    _, _, report = two_runs
    arrival = report.new[0]
    assert arrival.first_seen == RUN_TWO == arrival.last_seen


def test_merge_collapses_cross_source_duplicates():
    """Same camp, two sources. First listed wins."""
    a = make_session("Shared Camp", price_usd=100)
    b = make_session("Shared Camp", price_usd=999)
    state, report = merge({}, [a, b], now=RUN_ONE)
    assert len(state) == 1
    assert report.new[0].session.price_usd == 100


def test_merge_of_nothing_new_is_quiet():
    sessions = [make_session("Steady")]
    first, _ = merge({}, sessions, now=RUN_ONE)
    _, report = merge(first, sessions, now=RUN_TWO)
    assert report.is_empty
    assert report.summary() == "no changes"


# --------------------------------------------------------------------------
# needed dates
# --------------------------------------------------------------------------


def test_needed_range_is_inclusive_of_both_ends():
    week = NeededRange("fall-break", "Fall Break", date(2026, 10, 5), date(2026, 10, 9))
    assert len(week.days()) == 5
    assert week.days()[0] == date(2026, 10, 5)
    assert week.days()[-1] == date(2026, 10, 9)


def test_needed_range_rejects_backwards_dates():
    with pytest.raises(ValueError, match="precedes"):
        NeededRange("bad", "Bad", date(2026, 10, 9), date(2026, 10, 5))


def test_coverage_reports_every_range_a_session_touches(ranges):
    # A session long enough to span two breaks lands in both.
    long_run = make_session("Marathon", start=date(2026, 12, 20), end=date(2027, 2, 20))
    slugs, _ = coverage(long_run, ranges)
    assert slugs == ["february-break", "winter-break"]


def test_coverage_is_empty_when_nothing_overlaps(ranges):
    slugs, days = coverage(make_session("Ordinary Tuesday", start=date(2026, 11, 10)), ranges)
    assert slugs == []
    assert days == []


def test_load_needed_ranges_reads_the_shipped_config():
    config = Path(__file__).resolve().parent.parent / "config" / "dates.yaml"
    ranges = load_needed_ranges(config)
    slugs = [r.slug for r in ranges]
    assert "fall-break" in slugs
    assert "february-break" in slugs
    assert len(slugs) == len(set(slugs))


def test_load_needed_ranges_rejects_duplicate_slugs(tmp_path):
    path = tmp_path / "dates.yaml"
    path.write_text(
        "breaks:\n"
        "  - {slug: dup, name: One, start: 2026-10-05, end: 2026-10-06}\n"
        "  - {slug: dup, name: Two, start: 2026-11-05, end: 2026-11-06}\n"
    )
    with pytest.raises(ValueError, match="duplicate slug"):
        load_needed_ranges(path)


# --------------------------------------------------------------------------
# persistence
# --------------------------------------------------------------------------


def test_state_round_trips(tmp_path):
    state, _ = merge({}, [make_session("Round Trip", price_usd=42.5)], now=RUN_ONE)
    path = tmp_path / "camps.json"
    save_state(path, state, now=RUN_ONE)

    reloaded, generated_at = load_state(path)
    assert generated_at == RUN_ONE
    assert list(reloaded) == list(state)
    assert reloaded[next(iter(state))].session.price_usd == 42.5


def test_missing_state_file_is_a_first_run_not_an_error(tmp_path):
    records, generated_at = load_state(tmp_path / "absent.json")
    assert records == {}
    assert generated_at is None


def test_save_is_byte_stable_for_the_same_state(tmp_path):
    """A committed file whose diff is dictionary reordering is unreadable."""
    state, _ = merge({}, [make_session(f"Camp {i}") for i in range(5)], now=RUN_ONE)
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    save_state(a, state, now=RUN_ONE)
    save_state(b, dict(reversed(list(state.items()))), now=RUN_ONE)
    assert a.read_text() == b.read_text()
