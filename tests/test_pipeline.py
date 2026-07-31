"""Smoke tests for pipeline output, focused on run status.

These exist because of a real gap. The pipeline originally skipped writing the
site data file whenever every source failed — protecting good state, but also
meaning the dashboard could never report the failure. It would keep serving
last week's camps and look perfectly healthy while collecting nothing, which is
indistinguishable from working right up until a registration date is missed.

The rule these lock in:

* state.json is only overwritten when at least one source succeeded, and
* sessions.json is written *every* run, so the run block always reflects reality.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from campradar.models import CampSession
from campradar.pipeline import RunResult, _write_site_data, load_breaks

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)

BREAKS_YAML = """
breaks:
  - name: Spring Break
    start: 2027-04-05
    end: 2027-04-09
"""


@pytest.fixture
def breaks(tmp_path: Path):
    path = tmp_path / "breaks.yaml"
    path.write_text(BREAKS_YAML)
    return load_breaks(path)


def a_session() -> CampSession:
    return CampSession(
        provider_slug="test-provider",
        title="Pond Explorers",
        start_date=date(2027, 4, 5),
        end_date=date(2027, 4, 9),
        source_id="good-source",
    )


def write(tmp_path: Path, state, breaks, result: RunResult) -> dict:
    """Write site data and read it back."""
    from campradar.delta import merge

    out = tmp_path / "site"
    merged, delta = merge({}, state, now=NOW)
    _write_site_data(out, merged, breaks, {}, delta, NOW, result)
    return json.loads((out / "sessions.json").read_text())


class TestRunBlock:
    def test_unconfigured_install_reports_no_sources(self, tmp_path, breaks):
        """A fresh install must be distinguishable from a broken one."""
        payload = write(tmp_path, [], breaks, RunResult())
        assert payload["run"]["sources_ok"] == []
        assert payload["run"]["sources_failed"] == []
        assert payload["sessions"] == []

    def test_successful_run_names_its_sources(self, tmp_path, breaks):
        result = RunResult(sessions_found=1, succeeded_sources=["good-source"])
        payload = write(tmp_path, [a_session()], breaks, result)
        assert payload["run"]["sources_ok"] == ["good-source"]
        assert len(payload["sessions"]) == 1

    def test_failures_are_named_not_just_counted(self, tmp_path, breaks):
        """The dashboard shows which source broke, so it can be fixed."""
        result = RunResult(failed_sources=["broken-source"], succeeded_sources=["good-source"])
        payload = write(tmp_path, [a_session()], breaks, result)
        assert payload["run"]["sources_failed"] == ["broken-source"]

    def test_total_failure_still_publishes_the_failure(self, tmp_path, breaks):
        """The regression this module exists for.

        Every source failed, so sessions carry forward untouched — but the run
        block must say so rather than letting the page look healthy.
        """
        result = RunResult(failed_sources=["good-source"])
        payload = write(tmp_path, [a_session()], breaks, result)

        assert payload["run"]["sources_ok"] == []
        assert payload["run"]["sources_failed"] == ["good-source"]
        assert len(payload["sessions"]) == 1, "prior sessions must not be discarded"


class TestRunResult:
    def test_total_failure_requires_at_least_one_source(self):
        """Zero configured sources is 'not set up', not 'everything broke'."""
        assert RunResult().is_total_failure is False

    def test_all_sources_failing_is_a_total_failure(self):
        assert RunResult(failed_sources=["a", "b"]).is_total_failure is True

    def test_partial_failure_is_not_total(self):
        result = RunResult(succeeded_sources=["a"], failed_sources=["b"])
        assert result.is_total_failure is False


def test_breaks_reach_the_dashboard(tmp_path, breaks):
    payload = write(tmp_path, [], breaks, RunResult())
    assert payload["breaks"] == [
        {"name": "Spring Break", "start": "2027-04-05", "end": "2027-04-09"}
    ]


class TestPublishedRoundTrip:
    """State must survive a round trip through the published sessions.json.

    This is what lets CI run without commit access: the deployed site is the
    state store. If `first_seen` doesn't survive, every weekly run re-reports
    every camp as new and the dashboard becomes noise.
    """

    def payload(self, tmp_path, breaks) -> dict:
        result = RunResult(sessions_found=1, succeeded_sources=["good-source"])
        return write(tmp_path, [a_session()], breaks, result)

    def test_published_payload_carries_both_timestamps(self, tmp_path, breaks):
        session = self.payload(tmp_path, breaks)["sessions"][0]
        assert "first_seen" in session
        assert "last_seen" in session, "needed to rebuild state without data loss"

    def test_hydrates_back_into_equivalent_state(self, tmp_path, breaks):
        from campradar.delta import state_from_published

        state = state_from_published(self.payload(tmp_path, breaks))
        assert len(state) == 1
        record = next(iter(state.values()))
        assert record.session.title == "Pond Explorers"
        assert record.first_seen == NOW

    def test_first_seen_survives_a_second_cycle(self, tmp_path, breaks):
        """Publish, hydrate, merge again — the original date must persist."""
        from campradar.delta import merge, state_from_published

        published = self.payload(tmp_path, breaks)
        hydrated = state_from_published(published)

        later = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)
        merged, report = merge(hydrated, [a_session()], now=later)

        assert report.new == [], "a known camp must not be re-reported as new"
        assert next(iter(merged.values())).first_seen == NOW

    def test_missing_last_seen_falls_back(self, tmp_path, breaks):
        """Files published before last_seen existed must still hydrate."""
        from campradar.delta import state_from_published

        payload = self.payload(tmp_path, breaks)
        del payload["sessions"][0]["last_seen"]
        assert len(state_from_published(payload)) == 1

    def test_malformed_entries_are_skipped_not_fatal(self, tmp_path, breaks):
        """A published file is outside our control once deployed."""
        from campradar.delta import state_from_published

        payload = self.payload(tmp_path, breaks)
        payload["sessions"].append({"key": "junk", "title": "no dates here"})
        assert len(state_from_published(payload)) == 1

    def test_empty_payload_is_a_first_run(self):
        from campradar.delta import state_from_published

        assert state_from_published({"sessions": []}) == {}
        assert state_from_published({}) == {}


class TestEmptySourcesAreDistinctFromWorkingOnes:
    """"Ran without erroring" and "found something" are different facts.

    Conflating them is how this project spent weeks reporting "3 sources ok"
    over an empty dashboard: every source fetched, every source parsed, and
    every source produced nothing. The run block now says so.
    """

    def test_a_source_returning_nothing_is_recorded_as_empty(self):
        result = RunResult(
            succeeded_sources=["has-camps", "quiet-one"],
            empty_sources=["quiet-one"],
        )
        assert result.productive_sources == ["has-camps"]

    def test_productive_excludes_every_empty_source(self):
        result = RunResult(
            succeeded_sources=["a", "b", "c"],
            empty_sources=["a", "c"],
        )
        assert result.productive_sources == ["b"]

    def test_all_empty_is_not_a_total_failure(self):
        """Nothing errored, so CI should not go red — but the report must show it."""
        result = RunResult(succeeded_sources=["a"], empty_sources=["a"])
        assert result.is_total_failure is False
        assert result.productive_sources == []

    def test_no_empty_sources_means_everything_produced(self):
        result = RunResult(succeeded_sources=["a", "b"])
        assert result.productive_sources == ["a", "b"]


class TestBreakCalendarMatchesTheDistrict:
    """Guards config/breaks.yaml against the 2026-2027 DeKalb calendar.

    These dates were wrong once in a way no code could catch: Fall Break was
    listed for late September when the district has it in early October. The
    file parsed, the site rendered, and the answer was two weeks off. The only
    defence against that class of error is asserting the boundaries against the
    published calendar, so the checks below encode school days -- the dates a
    break must NOT contain -- rather than restating the breaks themselves.
    """

    @staticmethod
    def _breaks():
        from pathlib import Path

        from campradar.pipeline import load_breaks

        return load_breaks(Path("config/breaks.yaml"))

    @pytest.mark.parametrize(
        "day,why",
        [
            (date(2026, 8, 3), "First Day of School"),
            (date(2026, 12, 18), "Last Day of Semester"),
            (date(2027, 1, 5), "First Day of 2nd Semester"),
            (date(2027, 5, 27), "Last Day of School"),
            (date(2026, 10, 13), "back in class after Fall Break"),
            (date(2026, 9, 21), "an ordinary Monday, the old wrong Fall Break"),
        ],
    )
    def test_school_days_are_not_inside_any_break(self, day, why):
        inside = [b.name for b in self._breaks() if b.contains(day)]
        assert not inside, f"{day} is a school day ({why}) but sits inside {inside}"

    @pytest.mark.parametrize(
        "day,expected",
        [
            (date(2026, 10, 5), "Fall Break"),
            (date(2026, 10, 9), "Fall Break"),
            (date(2026, 11, 27), "Thanksgiving Break"),
            (date(2027, 1, 4), "Winter Break"),
            (date(2027, 4, 9), "Spring Break"),
        ],
    )
    def test_break_edges_are_covered(self, day, expected):
        """The first and last days out are the ones an off-by-one loses."""
        assert expected in [b.name for b in self._breaks() if b.contains(day)]

    def test_every_window_is_ordered(self):
        for brk in self._breaks():
            assert brk.start <= brk.end, f"{brk.name} ends before it starts"
