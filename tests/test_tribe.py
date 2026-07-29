"""Tests for the Events Calendar adapter.

Callanwolde's camps page fetched cleanly and produced zero sessions for weeks
because it is prose with no markup. The fix was not a better parser — it was
noticing `X-Tec-Api-Root` in the response headers and using the API the site was
already exposing. These tests pin that path.

Offline throughout, via `httpx.MockTransport`. The request schema was confirmed
against a live discovery call; the response shape here follows The Events
Calendar's documented format, so the tests double as a written record of what
this adapter believes about that format.
"""

from __future__ import annotations

from datetime import date

import httpx
import pytest

from campradar.adapters.base import AdapterError
from campradar.adapters.tribe import (
    TribeEventsAdapter,
    build_events_url,
    strip_html,
)
from campradar.fetch import Fetcher
from campradar.models import RegistrationStatus


def event(**overrides):
    """One event, shaped like The Events Calendar's documented output."""
    base = {
        "id": 4021,
        "status": "publish",
        "title": "Spring Break Creative Camp",
        "description": "<p>A week of painting and clay. Ages 6-11.</p>",
        "excerpt": "<p>A week of art.</p>",
        "slug": "spring-break-creative-camp",
        "url": "https://callanwolde.org/event/spring-break-creative-camp/",
        "all_day": False,
        "start_date": "2027-04-05 09:00:00",
        "end_date": "2027-04-09 15:00:00",
        "timezone": "America/New_York",
        "cost": "$395",
        "cost_details": {"currency_symbol": "$", "values": [395]},
        "website": "",
        "categories": [{"id": 5, "name": "Camps", "slug": "camps"}],
        "tags": [],
    }
    base.update(overrides)
    return base


def payload(events, total_pages=1):
    return {"events": events, "total": len(events), "total_pages": total_pages}


def run_adapter(pages, config=None, tmp_path=None, statuses=None):
    """Drive the adapter against canned pages via MockTransport."""
    calls: list[httpx.URL] = []
    sequence = list(pages)
    codes = list(statuses or [])

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url)
        if codes:
            code = codes.pop(0)
            if code != 200:
                return httpx.Response(code, json={"message": "nope"})
        body = sequence.pop(0) if sequence else payload([])
        return httpx.Response(200, json=body)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    source = {
        "id": "callanwolde-tribe",
        "provider_slug": "callanwolde",
        "base_url": "https://callanwolde.org",
    }
    source.update(config or {})
    adapter = TribeEventsAdapter(source)
    with Fetcher(tmp_path / "raw", delay_seconds=0.0, client=client) as fetcher:
        sessions = adapter.run(fetcher)
    return sessions, calls


class TestBuildEventsUrl:
    def test_uses_the_documented_path(self):
        url = build_events_url("https://callanwolde.org", {})
        assert url.startswith("https://callanwolde.org/wp-json/tribe/events/v1/events")

    def test_trailing_slash_does_not_double_up(self):
        """A doubled slash 404s on some WordPress installs."""
        assert "//wp-json" not in build_events_url("https://e.org/", {"a": 1})

    def test_parameters_are_sorted_for_a_stable_cache_key(self):
        assert build_events_url("https://e.org", {"b": 2, "a": 1}) == build_events_url(
            "https://e.org", {"a": 1, "b": 2}
        )

    def test_empty_values_are_dropped(self):
        url = build_events_url("https://e.org", {"search": None, "page": 1})
        assert "search=" not in url


class TestStripHtml:
    def test_tags_go_and_text_stays(self):
        assert strip_html("<p>Ages 6-11.</p>") == "Ages 6-11."

    def test_block_boundaries_become_spaces(self):
        """Without a separator, "<p>ages</p><p>6-11</p>" would join into "ages6-11"."""
        assert strip_html("<p>For ages</p><p>8-13</p>") == "For ages 8-13"

    def test_age_parsing_survives_html(self):
        """The point of stripping: ages hide inside markup."""
        from campradar.adapters.jsonld import parse_age_text

        text = strip_html("<div><span>For ages</span> <strong>7-12</strong></div>")
        assert parse_age_text(text) == (7, 12)

    @pytest.mark.parametrize("value", ["", "   ", None, 42, "<p></p>"])
    def test_nothing_useful_becomes_none(self, value):
        assert strip_html(value) is None

    def test_whitespace_is_collapsed(self):
        assert strip_html("<p>a\n\n   b</p>") == "a b"


class TestConfigValidation:
    def test_missing_base_url_is_a_source_failure(self, tmp_path):
        adapter = TribeEventsAdapter({"id": "s"})
        with (
            Fetcher(tmp_path / "raw", delay_seconds=0.0) as fetcher,
            pytest.raises(AdapterError, match="base_url"),
        ):
            adapter.run(fetcher)


class TestFieldMapping:
    def test_the_documented_shape_maps_cleanly(self, tmp_path):
        sessions, _ = run_adapter([payload([event()])], tmp_path=tmp_path)
        assert len(sessions) == 1
        s = sessions[0]
        assert s.title == "Spring Break Creative Camp"
        assert s.start_date == date(2027, 4, 5)
        assert s.end_date == date(2027, 4, 9)
        assert (s.min_age, s.max_age) == (6, 11)
        assert s.price_usd == 395.0
        assert s.provider_slug == "callanwolde"

    def test_registration_status_is_unknown_not_open(self, tmp_path):
        """The plugin exposes no registration state; guessing "open" would lie."""
        sessions, _ = run_adapter([payload([event()])], tmp_path=tmp_path)
        assert sessions[0].registration_status is RegistrationStatus.UNKNOWN

    def test_an_event_without_a_start_date_is_skipped(self, tmp_path):
        sessions, _ = run_adapter([payload([event(start_date="")])], tmp_path=tmp_path)
        assert sessions == []

    def test_a_missing_end_date_falls_back_to_the_start(self, tmp_path):
        sessions, _ = run_adapter([payload([event(end_date=None)])], tmp_path=tmp_path)
        assert sessions[0].end_date == date(2027, 4, 5)

    def test_a_reversed_range_is_repaired(self, tmp_path):
        sessions, _ = run_adapter(
            [payload([event(start_date="2027-04-09 09:00:00", end_date="2027-04-05 15:00:00")])],
            tmp_path=tmp_path,
        )
        assert sessions[0].start_date == sessions[0].end_date == date(2027, 4, 9)

    def test_an_event_without_a_url_is_skipped(self, tmp_path):
        sessions, _ = run_adapter([payload([event(url="", website="")])], tmp_path=tmp_path)
        assert sessions == []

    def test_website_is_used_when_url_is_absent(self, tmp_path):
        sessions, _ = run_adapter(
            [payload([event(url="", website="https://callanwolde.org/reg")])],
            tmp_path=tmp_path,
        )
        assert str(sessions[0].url) == "https://callanwolde.org/reg"

    def test_ages_come_from_the_description(self, tmp_path):
        sessions, _ = run_adapter(
            [payload([event(title="Camp", description="<p>For ages 9-14.</p>")])],
            tmp_path=tmp_path,
        )
        assert (sessions[0].min_age, sessions[0].max_age) == (9, 14)

    def test_ages_absent_stay_none(self, tmp_path):
        sessions, _ = run_adapter(
            [payload([event(title="Camp", description="<p>Fun outside.</p>", excerpt="")])],
            tmp_path=tmp_path,
        )
        assert sessions[0].min_age is None


class TestPriceExtraction:
    def test_structured_values_win(self, tmp_path):
        sessions, _ = run_adapter(
            [payload([event(cost="from $500", cost_details={"values": [395, 425]})])],
            tmp_path=tmp_path,
        )
        assert sessions[0].price_usd == 395.0

    def test_lowest_of_a_member_nonmember_pair(self, tmp_path):
        sessions, _ = run_adapter(
            [payload([event(cost_details={"values": [425, 395]})])], tmp_path=tmp_path
        )
        assert sessions[0].price_usd == 395.0

    def test_falls_back_to_parsing_the_display_string(self, tmp_path):
        sessions, _ = run_adapter(
            [payload([event(cost="$310 - $340", cost_details={})])], tmp_path=tmp_path
        )
        assert sessions[0].price_usd == 310.0

    @pytest.mark.parametrize("cost", ["", "Free", "Varies", "call for pricing"])
    def test_unpriceable_strings_give_none_not_zero(self, cost, tmp_path):
        """None renders as "not stated"; 0.0 would claim the camp is free."""
        sessions, _ = run_adapter(
            [payload([event(cost=cost, cost_details={})])], tmp_path=tmp_path
        )
        assert sessions[0].price_usd is None

    def test_a_structured_zero_is_not_treated_as_a_price(self, tmp_path):
        sessions, _ = run_adapter(
            [payload([event(cost="", cost_details={"values": [0]})])], tmp_path=tmp_path
        )
        assert sessions[0].price_usd is None


class TestCategoryFiltering:
    def test_wanted_slug_is_kept(self, tmp_path):
        sessions, _ = run_adapter(
            [payload([event()])],
            config={"category_slugs": ["camps"]},
            tmp_path=tmp_path,
        )
        assert len(sessions) == 1

    def test_other_categories_are_dropped(self, tmp_path):
        """Callanwolde's calendar carries concerts and galas as well as camps."""
        sessions, _ = run_adapter(
            [
                payload(
                    [
                        event(id=1, categories=[{"name": "Camps", "slug": "camps"}]),
                        event(id=2, categories=[{"name": "Concerts", "slug": "concerts"}]),
                    ]
                )
            ],
            config={"category_slugs": ["camps"]},
            tmp_path=tmp_path,
        )
        assert len(sessions) == 1

    def test_matching_is_case_insensitive_and_accepts_names(self, tmp_path):
        sessions, _ = run_adapter(
            [payload([event()])],
            config={"category_slugs": ["CAMPS"]},
            tmp_path=tmp_path,
        )
        assert len(sessions) == 1

    def test_no_filter_keeps_everything(self, tmp_path):
        sessions, _ = run_adapter(
            [payload([event(id=1), event(id=2, categories=[])])], tmp_path=tmp_path
        )
        assert len(sessions) == 2


class TestPagination:
    def test_walks_to_total_pages(self, tmp_path):
        pages = [
            payload([event(id=1)], total_pages=2),
            payload([event(id=2)], total_pages=2),
        ]
        sessions, calls = run_adapter(pages, config={"per_page": 1}, tmp_path=tmp_path)
        assert len(sessions) == 2
        assert len(calls) == 2

    def test_a_404_past_page_one_ends_pagination_quietly(self, tmp_path):
        """The plugin 404s past the end of the archive. That is not an error."""
        sessions, _ = run_adapter(
            [payload([event()], total_pages=99)],
            config={"per_page": 1},
            tmp_path=tmp_path,
            statuses=[200, 404],
        )
        assert len(sessions) == 1

    def test_a_404_on_page_one_is_a_real_failure(self, tmp_path):
        """A wrong base_url must fail loudly, not look like an empty calendar."""
        adapter = TribeEventsAdapter(
            {"id": "s", "base_url": "https://example.org", "provider_slug": "p"}
        )

        def handler(request):
            return httpx.Response(404, json={"message": "no route"})

        client = httpx.Client(transport=httpx.MockTransport(handler))
        with (
            Fetcher(tmp_path / "raw", delay_seconds=0.0, client=client) as fetcher,
            pytest.raises(AdapterError, match="tribe/events/v1"),
        ):
            adapter.run(fetcher)

    def test_duplicate_ids_across_pages_are_dropped(self, tmp_path):
        same = payload([event(id=7)], total_pages=3)
        sessions, _ = run_adapter(
            [same, same, payload([])], config={"per_page": 1}, tmp_path=tmp_path
        )
        assert len(sessions) == 1

    def test_page_ceiling_is_enforced(self, tmp_path, caplog):
        import logging

        pages = [payload([event(id=i)], total_pages=999) for i in range(10)]
        with caplog.at_level(logging.WARNING):
            _, calls = run_adapter(
                pages, config={"per_page": 1, "max_pages": 3}, tmp_path=tmp_path
            )
        assert len(calls) == 3
        assert any("ceiling" in r.message for r in caplog.records)


class TestBadResponses:
    def test_non_json_is_a_source_failure(self, tmp_path):
        def handler(request):
            return httpx.Response(200, text="<html>maintenance</html>")

        client = httpx.Client(transport=httpx.MockTransport(handler))
        adapter = TribeEventsAdapter({"id": "s", "base_url": "https://e.org"})
        with (
            Fetcher(tmp_path / "raw", delay_seconds=0.0, client=client) as fetcher,
            pytest.raises(AdapterError, match="non-JSON"),
        ):
            adapter.run(fetcher)

    def test_a_json_array_is_rejected(self, tmp_path):
        def handler(request):
            return httpx.Response(200, json=[1, 2])

        client = httpx.Client(transport=httpx.MockTransport(handler))
        adapter = TribeEventsAdapter({"id": "s", "base_url": "https://e.org"})
        with (
            Fetcher(tmp_path / "raw", delay_seconds=0.0, client=client) as fetcher,
            pytest.raises(AdapterError, match="expected an object"),
        ):
            adapter.run(fetcher)

    def test_garbage_rows_are_skipped_not_fatal(self, tmp_path):
        sessions, _ = run_adapter(
            [payload(["not a dict", event(), {"title": "no dates"}])], tmp_path=tmp_path
        )
        assert len(sessions) == 1

    def test_an_empty_events_list_is_not_an_error(self, tmp_path):
        sessions, _ = run_adapter([payload([])], tmp_path=tmp_path)
        assert sessions == []


class TestApiPaths:
    """The categories path must not be derived by munging the events path.

    `API_PATH.replace("/events", "/categories")` matches the `/events` inside
    `tribe/events/v1` first and yields `/wp-json/tribe/categories/v1/categories`,
    a route that does not exist. That shipped once and was caught only by
    running the command.
    """

    def test_events_path(self):
        from campradar.adapters.tribe import API_PATH

        assert API_PATH == "/wp-json/tribe/events/v1/events"

    def test_categories_path_keeps_the_events_namespace(self):
        from campradar.adapters.tribe import CATEGORIES_PATH

        assert CATEGORIES_PATH == "/wp-json/tribe/events/v1/categories"

    def test_naive_munging_would_have_been_wrong(self):
        from campradar.adapters.tribe import API_PATH, CATEGORIES_PATH

        assert API_PATH.replace("/events", "/categories") != CATEGORIES_PATH


class TestArrayParams:
    """WordPress array params need `key[]=`; a bare `key=` is silently ignored."""

    def test_a_list_becomes_repeated_bracket_pairs(self):
        url = build_events_url("https://e.org", {"categories": ["camps", "youth"]})
        assert url.count("categories%5B%5D=") == 2

    def test_a_scalar_stays_plain(self):
        assert "search=camp" in build_events_url("https://e.org", {"search": "camp"})

    def test_the_adapter_passes_lists_through_correctly(self, tmp_path):
        _, calls = run_adapter(
            [payload([event()])],
            config={"params": {"categories": ["camps"]}},
            tmp_path=tmp_path,
        )
        assert "categories%5B%5D=camps" in str(calls[0])
