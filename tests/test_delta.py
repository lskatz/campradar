"""Smoke tests for change tracking.

The delta layer is where a bug would be least visible and most costly: a
regression that stops reporting new sessions produces a dashboard that looks
perfectly healthy while quietly failing at its only job.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from campradar.delta import merge, save_state, load_state
from campradar.models import CampSession, RegistrationStatus

RUN_ONE = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
RUN_TWO = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)


def session(title: str = "Junior Naturalists", **overrides) -> CampSession:
    defaults = dict(
        provider_slug="fernbank-science-center",
        title=title,
        start_date=date(2027, 4, 5),
        end_date=date(2027, 4, 9),
        source_id="test",
    )
    return CampSession(**{**defaults, **overrides})


class TestFirstRun:
    def test_everything_is_new_on_an_empty_state(self):
        state, report = merge({}, [session()], now=RUN_ONE)
        assert len(state) == 1
        assert len(report.new) == 1

    def test_first_seen_is_recorded(self):
        state, _ = merge({}, [session()], now=RUN_ONE)
        assert next(iter(state.values())).first_seen == RUN_ONE


class TestSubsequentRuns:
    def test_unchanged_session_is_not_reported_as_new(self):
        state_one, _ = merge({}, [session()], now=RUN_ONE)
        _, report = merge(state_one, [session()], now=RUN_TWO)
        assert report.new == []

    def test_first_seen_survives_a_second_run(self):
        """This is the whole point of the module."""
        state_one, _ = merge({}, [session()], now=RUN_ONE)
        state_two, _ = merge(state_one, [session()], now=RUN_TWO)
        record = next(iter(state_two.values()))
        assert record.first_seen == RUN_ONE
        assert record.last_seen == RUN_TWO

    def test_genuinely_new_session_is_flagged(self):
        state_one, _ = merge({}, [session("Junior Naturalists")], now=RUN_ONE)
        _, report = merge(
            state_one,
            [session("Junior Naturalists"), session("Pond Explorers")],
            now=RUN_TWO,
        )
        assert len(report.new) == 1
        assert report.new[0].session.title == "Pond Explorers"


class TestRegistrationTransitions:
    def test_opening_for_registration_is_reported(self):
        closed = session(registration_status=RegistrationStatus.NOT_YET_OPEN)
        state_one, _ = merge({}, [closed], now=RUN_ONE)

        opened = session(registration_status=RegistrationStatus.OPEN)
        _, report = merge(state_one, [opened], now=RUN_TWO)
        assert len(report.newly_open) == 1

    def test_staying_open_is_not_reported_again(self):
        """Otherwise every weekly run would re-alert on the same camps."""
        open_session = session(registration_status=RegistrationStatus.OPEN)
        state_one, _ = merge({}, [open_session], now=RUN_ONE)
        _, report = merge(state_one, [open_session], now=RUN_TWO)
        assert report.newly_open == []


class TestDisappearance:
    def test_missing_session_is_retained_not_deleted(self):
        state_one, _ = merge({}, [session()], now=RUN_ONE)
        state_two, report = merge(state_one, [], now=RUN_TWO)
        assert len(state_two) == 1, "records must survive a source going quiet"
        assert len(report.disappeared) == 1


class TestDeduplication:
    def test_same_session_from_two_sources_collapses(self):
        from_provider = session(source_id="provider-site")
        from_aggregator = session(source_id="aggregator")
        state, report = merge({}, [from_provider, from_aggregator], now=RUN_ONE)
        assert len(state) == 1
        assert len(report.new) == 1

    def test_first_source_listed_wins(self):
        state, _ = merge(
            {},
            [session(source_id="provider-site"), session(source_id="aggregator")],
            now=RUN_ONE,
        )
        assert next(iter(state.values())).session.source_id == "provider-site"


def test_state_survives_a_save_load_round_trip(tmp_path):
    state, _ = merge({}, [session()], now=RUN_ONE)
    path = tmp_path / "state.json"
    save_state(path, state)
    assert load_state(path) == state


def test_load_state_treats_missing_file_as_first_run(tmp_path):
    assert load_state(tmp_path / "nope.json") == {}
