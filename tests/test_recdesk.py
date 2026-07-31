"""Tests for the RecDesk adapter.

All offline, driven through `httpx.MockTransport` against
`tests/fixtures/recdesk_day_camp.html`.

The emphasis here is deliberately lopsided toward the *failure* behaviour. The
happy path is easy and would have been caught by anyone eyeballing the output.
What actually cost this project a season was a source that fetched cleanly and
returned zero, which looked identical to a quiet month. So the tests that matter
most are the ones asserting that a layout change raises rather than returns
nothing.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import httpx
import pytest

from campradar.adapters.base import AdapterError
from campradar.adapters.recdesk import (
    RecDeskAdapter,
    parse_age_range,
    parse_date_range,
    parse_fragment,
    parse_status,
)
from campradar.fetch import Fetcher
from campradar.models import RegistrationStatus

FIXTURE = (Path(__file__).parent / "fixtures" / "recdesk_day_camp.html").read_text()

CONFIG = {
    "id": "tucker-rec",
    "provider_slug": "tucker-rec",
    "base_url": "https://tucker.recdesk.com",
    "categories": ["9"],
}


def run_adapter(pages, tmp_path, config=None):
    """Drive the adapter against canned fragments, one per POST."""
    responses = list(pages)
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            # The session-priming request. Recorded, but it does not consume a
            # canned fragment -- those stand in for FilterPrograms responses.
            calls.append(request)
            return httpx.Response(200, text="<html><body>programme page</body></html>")
        calls.append(request)
        body = responses.pop(0) if responses else ""
        return httpx.Response(200, text=body)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = RecDeskAdapter({**CONFIG, **(config or {})})
    with Fetcher(tmp_path / "raw", delay_seconds=0.0, client=client) as fetcher:
        return adapter.run(fetcher), calls


class TestFieldParsers:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("Dates 7/27/2026 - 7/31/2026", (date(2026, 7, 27), date(2026, 7, 31))),
            ("Dates 11/25/2026", (date(2026, 11, 25), date(2026, 11, 25))),
            ("Dates 9/21/2026 – 9/25/2026", (date(2026, 9, 21), date(2026, 9, 25))),
            ("no dates", (None, None)),
        ],
    )
    def test_dates(self, text, expected):
        assert parse_date_range(text) == expected

    def test_an_impossible_date_is_not_invented(self):
        """13/45/2026 is not a date; returning None beats returning garbage."""
        assert parse_date_range("2/30/2026")[0] is None

    @pytest.mark.parametrize(
        "text,expected",
        [
            ("Ages 5y - 12y", (5, 12)),
            ("Ages 7y - 12y 0m", (7, 12)),
            ("Ages 5y 6m - 12y", (5, 12)),
            ("Ages -", (None, None)),
        ],
    )
    def test_ages(self, text, expected):
        assert parse_age_range(text) == expected

    @pytest.mark.parametrize(
        "text,expected",
        [
            ("Remaining FULL", RegistrationStatus.FULL),
            ("Registration ended on 4/30/2026", RegistrationStatus.CLOSED),
            ("Registration opens on 1/5/2027", RegistrationStatus.NOT_YET_OPEN),
            ("Openings 30 Remaining 12", RegistrationStatus.OPEN),
            ("mystery", RegistrationStatus.UNKNOWN),
        ],
    )
    def test_status(self, text, expected):
        assert parse_status(text) == expected

    def test_full_wins_over_closed(self):
        """Both badges appear together; FULL is the fact a parent acts on."""
        text = "Registration ended on 4/30/2026 Remaining FULL"
        assert parse_status(text) is RegistrationStatus.FULL

    def test_unknown_never_becomes_open(self):
        """Optimism here would show availability nobody observed."""
        assert parse_status("Openings 30") is not RegistrationStatus.OPEN


class TestFragmentParsing:
    def test_it_finds_every_programme(self):
        rows = parse_fragment(FIXTURE, "https://tucker.recdesk.com")
        assert len(rows) == 5

    def test_category_headers_are_not_mistaken_for_programmes(self):
        titles = [r.title for r in parse_fragment(FIXTURE)]
        assert not any(t.startswith("Category") for t in titles)

    def test_fields_map_off_the_observed_values(self):
        rows = {r.title: r for r in parse_fragment(FIXTURE, "https://tucker.recdesk.com")}
        row = rows["100 - Eco Adventure Camp: Rainforest Expedition"]
        assert (row.start, row.end) == (date(2026, 7, 27), date(2026, 7, 31))
        assert (row.min_age, row.max_age) == (7, 12)
        assert row.status is RegistrationStatus.FULL
        assert row.url == "https://tucker.recdesk.com/Community/Program/Detail/48302"

    def test_a_single_day_programme_keeps_a_real_end_date(self):
        rows = {r.title: r for r in parse_fragment(FIXTURE)}
        row = rows["200 - Turkey Day Mini Camp"]
        assert row.start == row.end == date(2026, 11, 25)

    def test_an_unstated_age_stays_none(self):
        """Recall over precision: an unstated bound must not become a filter."""
        rows = {r.title: r for r in parse_fragment(FIXTURE)}
        assert rows["200 - Turkey Day Mini Camp"].min_age is None

    def test_filter_links_are_ignored(self):
        html = '<a href="/Community/Program?category=9">Day Camp</a>'
        assert parse_fragment(html) == []

    def test_an_empty_fragment_is_empty_not_an_error(self):
        assert parse_fragment("<div>No results found</div>") == []


class TestAdapter:
    def test_it_posts_to_filterprograms_as_the_browser_does(self, tmp_path):
        _, calls = run_adapter([FIXTURE, ""], tmp_path)
        request = next(c for c in calls if c.method == "POST")
        assert request.method == "POST"
        assert request.url.path == "/Community/Program/FilterPrograms"
        assert request.headers["X-Requested-With"] == "XMLHttpRequest"

    def test_the_body_carries_every_key_the_binder_expects(self, tmp_path):
        """Omitting fields lets ASP.NET fall back to session state.

        That fallback is the original bug: session-scoped filters are why a
        stateless request returned an empty table in the first place.
        """
        import json

        _, calls = run_adapter([FIXTURE, ""], tmp_path)
        body = json.loads(next(c for c in calls if c.method == "POST").content)
        for key in (
            "ProgramName", "Code", "ProgramNameXS", "DateRangeSelection",
            "DateRangeFrom", "DateRangeTo", "ProgramType", "Age",
            "Facility", "Days", "Pagination",
        ):
            assert key in body, f"missing {key}"
        assert body["ProgramType"] == "9"
        assert body["Pagination"]["CurrentPageIndex"] == 1

    def test_sessions_come_out_mapped(self, tmp_path):
        sessions, _ = run_adapter([FIXTURE, ""], tmp_path)
        by_title = {s.title: s for s in sessions}
        art = by_title["200 - Fall Break Art Studio"]
        assert art.start_date == date(2026, 9, 21)
        assert art.registration_status is RegistrationStatus.OPEN
        assert art.provider_slug == "tucker-rec"

    def test_pagination_advances_then_stops(self, tmp_path):
        import json

        _, calls = run_adapter([FIXTURE, FIXTURE, ""], tmp_path)
        posts = [c for c in calls if c.method == "POST"]
        pages = [json.loads(c.content)["Pagination"]["CurrentPageIndex"] for c in posts]
        assert pages[:2] == [1, 2]

    def test_a_repeated_page_ends_the_loop(self, tmp_path):
        """RecDesk keeps answering past the last page; identical rows mean stop."""
        sessions, calls = run_adapter([FIXTURE, FIXTURE, FIXTURE], tmp_path)
        posts = [c for c in calls if c.method == "POST"]
        assert len(posts) == 2, "should stop once a page adds nothing new"
        assert len(sessions) == 5, "five programmes, each yielded once"

    def test_multiple_categories_are_each_queried(self, tmp_path):
        import json

        _, calls = run_adapter(
            [FIXTURE, "", FIXTURE, ""], tmp_path, config={"categories": ["9", "20"]}
        )
        types = {
            json.loads(c.content)["ProgramType"] for c in calls if c.method == "POST"
        }
        assert types == {"9", "20"}


class TestFailingLoudly:
    """The behaviour that matters most: a broken source must not look empty."""

    def test_markup_drift_raises_rather_than_returning_nothing(self, tmp_path):
        drifted = (
            '<div><a href="/Community/Program/Detail/1">Some Camp</a>'
            "<span>starts sometime next spring</span></div>"
        )
        with pytest.raises(AdapterError) as excinfo:
            run_adapter([drifted], tmp_path)
        assert "markup" in str(excinfo.value).lower()

    def test_the_drift_error_says_how_to_fix_it(self, tmp_path):
        drifted = '<div><a href="/Community/Program/Detail/1">Some Camp</a> no date</div>'
        with pytest.raises(AdapterError) as excinfo:
            run_adapter([drifted], tmp_path)
        assert "recdesk-discover" in str(excinfo.value)

    def test_a_genuinely_empty_category_is_not_an_error(self, tmp_path):
        """No camps this month is a fact, not a fault."""
        sessions, _ = run_adapter(["<div>No results found</div>"], tmp_path)
        assert sessions == []

    def test_missing_categories_refuses_rather_than_guessing(self, tmp_path):
        with pytest.raises(AdapterError) as excinfo:
            run_adapter([FIXTURE], tmp_path, config={"categories": []})
        assert "recdesk-discover" in str(excinfo.value)

    def test_missing_base_url_is_a_source_level_failure(self, tmp_path):
        with pytest.raises(AdapterError):
            run_adapter([FIXTURE], tmp_path, config={"base_url": ""})


class TestSessionPriming:
    """The POST must land in an established session, as it does in a browser.

    Every date-filter value tried against the live site returned the same
    current-week rows, which is the signature of a server applying session
    defaults rather than the submitted filter. The browser never issues this
    POST cold -- it always has the programme page open first -- so neither
    should we.
    """

    def test_the_programme_page_is_fetched_before_filtering(self, tmp_path):
        _, calls = run_adapter([FIXTURE, ""], tmp_path)
        assert calls[0].method == "GET", "priming GET must come first"
        assert "category=9" in str(calls[0].url)

    def test_the_post_carries_a_referer(self, tmp_path):
        _, calls = run_adapter([FIXTURE, ""], tmp_path)
        post = next(c for c in calls if c.method == "POST")
        assert post.headers.get("Referer", "").endswith("category=9")

    def test_priming_can_be_disabled(self, tmp_path):
        """Kept switchable so the hypothesis stays falsifiable."""
        _, calls = run_adapter([FIXTURE, ""], tmp_path, config={"prime_session": False})
        assert all(c.method == "POST" for c in calls)

    def test_a_failed_priming_request_does_not_abort_the_source(self, tmp_path):
        """Best effort: if the page 404s, still try the filter."""
        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                return httpx.Response(500)
            return httpx.Response(200, text=FIXTURE)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        adapter = RecDeskAdapter(CONFIG)
        with Fetcher(tmp_path / "raw", delay_seconds=0.0, client=client) as fetcher:
            assert adapter.run(fetcher), "should still have parsed the fragment"


class TestBodyMatchesTheCapturedBrowserRequest:
    """Pins the POST body against a real request copied from the portal.

    This class exists because guessing cost four wrong diagnoses. The date
    fields were being sent correctly the whole time and doing nothing, because
    `DateRangeSelection` has to be the literal "pick" before RecDesk reads
    them. That token appears nowhere in the rendered page -- it was only
    recoverable by capturing the portal's own request -- so the defence against
    losing it again is to assert it directly.
    """

    #: Keys observed in the captured request, verbatim.
    CAPTURED_KEYS = {
        "ProgramName", "Code", "ProgramNameXS", "DateRangeSelection",
        "DateRangeFrom", "DateRangeTo", "ProgramType", "Age", "Facility",
        "Days", "ResultsPerPage", "Pagination",
    }

    @staticmethod
    def body(config=None):
        import json

        adapter = RecDeskAdapter({**CONFIG, "today": "2026-07-31", **(config or {})})
        return json.loads(json.dumps(adapter._body("9", 1)))

    def test_every_captured_key_is_present(self):
        assert set(self.body()) >= self.CAPTURED_KEYS

    def test_the_date_range_selection_is_the_pick_token(self):
        """The whole bug in one assertion."""
        assert self.body()["DateRangeSelection"] == "pick"

    def test_pagination_carries_a_page_size(self):
        pagination = self.body()["Pagination"]
        assert pagination["PageSize"] == "25"
        assert pagination["CurrentPageIndex"] == 1
        assert pagination["LoadMore"] is True

    def test_results_per_page_is_sent_at_the_top_level_too(self):
        """It appears in both places in the capture; send it in both."""
        assert self.body()["ResultsPerPage"] == "25"

    def test_dates_are_in_recdesks_us_format(self):
        import re

        body = self.body()
        for field in ("DateRangeFrom", "DateRangeTo"):
            assert re.fullmatch(r"\d{2}/\d{2}/\d{4}", body[field]), body[field]

    def test_the_default_window_spans_the_school_year(self):
        body = self.body()
        assert body["DateRangeFrom"] == "07/31/2026"
        assert body["DateRangeTo"] == "08/31/2027"

    def test_explicit_dates_win_over_the_default_window(self):
        body = self.body({"date_from": "08/08/2026", "date_to": "07/02/2027"})
        assert body["DateRangeFrom"] == "08/08/2026"
        assert body["DateRangeTo"] == "07/02/2027"

    def test_params_can_still_override_anything(self):
        assert self.body({"params": {"Age": "8"}})["Age"] == "8"

    def test_page_size_is_configurable_and_lands_in_both_places(self):
        body = self.body({"page_size": 50})
        assert body["ResultsPerPage"] == "50"
        assert body["Pagination"]["PageSize"] == "50"


class TestActionLinksAreNotProgrammes:
    """RecDesk renders a "Register Now" link inside every row.

    Observed live: a sweep of category 0 reported 39 "programmes", ten of which
    were the words "Register Now" paired with whatever dates sat nearest. The
    reconstructed fixture had no such buttons, so the suite passed on markup
    that does not exist -- which is the standing hazard of a fixture nobody
    captured.
    """

    @pytest.mark.parametrize(
        "label", ["Register Now", "register now", "Add to Cart", "More Info", "Waitlist"]
    )
    def test_control_links_are_skipped(self, label):
        html = (
            f'<div><a href="/Community/Program/Detail/1">{label}</a>'
            f"<span>Dates 8/24/2026 - 10/14/2026</span></div>"
        )
        assert parse_fragment(html) == []

    def test_a_real_programme_beside_a_button_still_parses(self):
        html = (
            '<div class="program-row">'
            '<a href="/Community/Program/Detail/7">Fall Break Art Studio</a>'
            '<a href="/Community/Program/Detail/7">Register Now</a>'
            "<span>Dates 10/5/2026 - 10/9/2026</span></div>"
        )
        rows = parse_fragment(html)
        assert [r.title for r in rows] == ["Fall Break Art Studio"]
