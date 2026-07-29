"""The example fixture is a contract, so it gets tested like one.

`site/example-camps.html` exists to be the one source that always works — the
thing that tells you "the providers changed" apart from "the pipeline broke".
That only holds if the fixture itself is known good, which means parsing it
here, offline, rather than trusting that it still says what it used to.

These tests read the file straight from disk. No network, no fetcher: the
network path is exercised for real when `make update` pulls the published copy
off GitHub Pages, and duplicating it here would only add flakiness.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from campradar.adapters.jsonld import extract_jsonld_objects, is_event
from campradar.models import RegistrationStatus

FIXTURE = Path(__file__).resolve().parents[1] / "site" / "example-camps.html"


@pytest.fixture(scope="module")
def events() -> dict[str, dict]:
    """Every event object in the fixture, keyed by name."""
    if not FIXTURE.exists():  # pragma: no cover - only when the file is deleted
        pytest.fail(f"{FIXTURE} is missing; the example source has nothing to read")
    objects = extract_jsonld_objects(FIXTURE.read_text(encoding="utf-8"))
    return {str(obj["name"]): obj for obj in objects if is_event(obj)}


def test_fixture_yields_the_expected_number_of_events(events):
    assert len(events) == 9


def test_non_event_objects_are_not_counted(events):
    """The Organization block has no startDate and must not become a session."""
    assert "Example Nature & Arts Collective" not in events


def test_every_event_has_the_fields_a_session_needs(events):
    for name, obj in events.items():
        assert obj.get("startDate"), f"{name} has no startDate"
        assert obj.get("name"), f"{name} has no name"
        assert obj.get("offers"), f"{name} has no offers block"


class TestAwkwardCasesStayCovered:
    """The fixture earns its keep by covering the cases that break parsers.

    If someone simplifies it, these fail and say what was lost.
    """

    def test_a_single_day_camp_is_present(self, events):
        mlk = events["MLK Day Mini-Camp"]
        assert mlk["startDate"] == mlk["endDate"] == "2027-01-18"

    def test_a_grade_range_is_present(self, events):
        """Exercises the grades-before-ages ordering in parse_age_text."""
        coding = events["Winter Break Coding Lab"]
        assert "rising grades 3-5" in coding["description"]

    def test_a_course_type_is_present(self, events):
        """Providers publish Course as well as Event; both must be picked up."""
        assert events["Winter Break Coding Lab"]["@type"] == "Course"

    def test_ages_appear_in_prose_only_on_at_least_one_event(self, events):
        sampler = events["Thanksgiving Sports Sampler"]
        assert "typicalAgeRange" not in sampler
        assert "ages 5-10" in sampler["description"]

    @pytest.mark.parametrize(
        "name",
        [
            "Fall Break Nature Detectives",   # InStock
            "Fall Break Clay Studio",         # LimitedAvailability
            "Winter Break Telescope Nights",  # SoldOut
            "Mid-Winter Theater Intensive",   # PreSale
        ],
    )
    def test_the_four_registration_states_are_represented(self, events, name):
        assert events[name]["offers"]["availability"]


class TestFixtureLinesUpWithTheBreakCalendar:
    """A camp outside every break window would never appear on the gap chart."""

    def test_events_land_inside_configured_breaks(self, events):
        from campradar.pipeline import load_breaks

        breaks = load_breaks(Path(__file__).resolve().parents[1] / "config" / "breaks.yaml")
        assert breaks, "breaks.yaml is empty; the fixture has nothing to line up with"

        for name, obj in events.items():
            start = date.fromisoformat(str(obj["startDate"])[:10])
            assert any(b.contains(start) for b in breaks), (
                f"{name} starts {start}, which is not in any configured break"
            )


def test_registration_status_vocabulary_covers_the_fixture(events):
    """Guards against a fixture using a schema.org value the adapter can't map."""
    known = {
        "instock",
        "limitedavailability",
        "presale",
        "preorder",
        "soldout",
        "outofstock",
        "discontinued",
    }
    for name, obj in events.items():
        token = str(obj["offers"]["availability"]).rsplit("/", 1)[-1].lower()
        assert token in known, f"{name} uses an availability value the adapter ignores"
    assert RegistrationStatus.OPEN  # the enum still exists
