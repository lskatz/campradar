"""Tests for the ACTIVE adapter and the credential redaction that guards it.

The redaction tests come first and are the more important of the two. The
adapter can be wrong and produce no camps; redaction being wrong publishes an
API key to a public repository, and git history cannot be retracted.

Everything here is offline. The adapter is driven through a `httpx.MockTransport`
so the request/response contract is exercised for real — pagination, dedupe,
JSON decoding, field mapping — without a key or a network.
"""

from __future__ import annotations

import json
import logging
from datetime import date

import httpx
import pytest

from campradar.adapters.activesearch import (
    ActiveSearchAdapter,
    build_search_url,
)
from campradar.adapters.base import AdapterError
from campradar.fetch import Fetcher
from campradar.models import RegistrationStatus
from campradar.redact import PLACEHOLDER, RedactingFilter, redact

SECRET = "abc123SUPERSECRETkey"


class TestRedact:
    """`redact` is the last line between a key and a committed log file."""

    def test_api_key_value_is_removed_and_name_is_kept(self):
        out = redact(f"https://api.amp.active.com/v2/search?near=Decatur&api_key={SECRET}")
        assert SECRET not in out
        assert "api_key=" in out
        assert PLACEHOLDER in out

    def test_other_parameters_survive(self):
        """A log line with no readable parameters is useless for debugging."""
        out = redact(f"/v2/search?near=Decatur,GA,US&radius=25&api_key={SECRET}")
        assert "near=Decatur,GA,US" in out
        assert "radius=25" in out

    @pytest.mark.parametrize(
        "text",
        [
            f"?api_key={SECRET}",
            f"?API_KEY={SECRET}",
            f"?apikey={SECRET}",
            f"?token={SECRET}",
            f"?api_secret={SECRET}",
            f"?access_token={SECRET}",
            f"?secret={SECRET}",
            f"?signature={SECRET}",
            f"?password={SECRET}",
            f"%3Fapi_key%3D{SECRET}",
            f"Authorization: Bearer {SECRET}",
        ],
    )
    def test_every_credential_shape_we_expect(self, text):
        assert SECRET not in redact(text)

    def test_multiple_secrets_in_one_string(self):
        out = redact(f"?api_key={SECRET}&token={SECRET}&near=Decatur")
        assert SECRET not in out
        assert "near=Decatur" in out

    def test_value_ends_at_the_quote_not_past_it(self):
        """httpx error messages wrap URLs in quotes; the tail must survive."""
        out = redact(f"Client error '403 Forbidden' for url 'http://x/y?api_key={SECRET}'")
        assert SECRET not in out
        assert out.endswith("'")
        assert "403 Forbidden" in out

    def test_accepts_an_exception_directly(self):
        """Callers should not have to remember to stringify first."""
        exc = httpx.HTTPError(f"boom ?api_key={SECRET}")
        assert SECRET not in redact(exc)

    def test_text_without_secrets_is_untouched(self):
        assert redact("https://callanwolde.org/camps/") == "https://callanwolde.org/camps/"


class TestRedactingFilter:
    """The record that leaks the key comes from httpx, not from our code."""

    @staticmethod
    def make_record(msg, *args):
        return logging.LogRecord(
            name="httpx", level=logging.INFO, pathname=__file__, lineno=1,
            msg=msg, args=args, exc_info=None,
        )

    def test_redacts_a_composed_message(self):
        record = self.make_record('HTTP Request: GET %s "200 OK"', f"http://x/?api_key={SECRET}")
        RedactingFilter().filter(record)
        assert SECRET not in record.getMessage()

    def test_returns_true_so_the_record_still_gets_logged(self):
        """Redaction must not silently swallow log output."""
        record = self.make_record("nothing sensitive")
        assert RedactingFilter().filter(record) is True


# --------------------------------------------------------------------------
# Adapter
# --------------------------------------------------------------------------


def asset(**overrides):
    """A minimal ACTIVE asset, shaped like the documented sample response."""
    base = {
        "assetGuid": "guid-1",
        "assetName": "Spring Break Art Camp",
        "activityStartDate": "2027-04-05T09:00:00",
        "activityEndDate": "2027-04-09T15:00:00",
        "regReqMinAge": "6",
        "regReqMaxAge": "11",
        "salesStatus": "registration-open",
        "salesStartDate": "2027-01-15T06:00:00",
        "urlAdr": "http://www.active.com/atlanta-ga/spring-break-art-camp",
        "assetDescriptions": [{"description": "A week of painting."}],
        "assetPrices": [],
        "organization": {"organizationName": "Callanwolde", "organizationGuid": "org-1"},
    }
    base.update(overrides)
    return base


def payload(results, total=None):
    return {
        "total_results": total if total is not None else len(results),
        "items_per_page": 50,
        "start_index": 0,
        "results": results,
    }


def run_adapter(pages, config=None, monkeypatch=None, tmp_path=None):
    """Drive the adapter against canned API pages via MockTransport."""
    calls: list[httpx.URL] = []
    sequence = list(pages)

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url)
        body = sequence.pop(0) if sequence else payload([])
        return httpx.Response(200, json=body)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    source = {
        "id": "active-test",
        "provider_slug": "callanwolde",
        "params": {"org_id": "org-1", "kids": "true"},
    }
    source.update(config or {})
    adapter = ActiveSearchAdapter(source)
    with Fetcher(tmp_path / "raw", delay_seconds=0.0, client=client) as fetcher:
        sessions = adapter.run(fetcher)
    return sessions, calls


class TestBuildSearchUrl:
    def test_key_is_included_and_params_are_sorted(self):
        url = build_search_url({"radius": 25, "near": "Decatur,GA,US"}, "K")
        assert url.startswith("https://api.amp.active.com/v2/search?")
        assert url.index("near=") < url.index("radius=")
        assert url.endswith("&api_key=K")

    def test_https_not_http(self):
        """ACTIVE's own docs show http://, but the key rides in the query string."""
        assert build_search_url({"a": "1"}, "K").startswith("https://")

    def test_ordering_is_stable_so_the_cache_key_is_stable(self):
        a = build_search_url({"b": "2", "a": "1"}, "K")
        b = build_search_url({"a": "1", "b": "2"}, "K")
        assert a == b

    def test_empty_values_are_dropped(self):
        url = build_search_url({"near": "Decatur", "query": None, "city": ""}, "K")
        assert "query=" not in url
        assert "city=" not in url


class TestCredentialHandling:
    def test_missing_key_is_a_source_level_failure(self, monkeypatch, tmp_path):
        monkeypatch.delenv("ACTIVE_API_KEY", raising=False)
        adapter = ActiveSearchAdapter({"id": "s", "params": {"org_id": "x"}})
        with (
            Fetcher(tmp_path / "raw", delay_seconds=0.0) as fetcher,
            pytest.raises(AdapterError, match="ACTIVE_API_KEY"),
        ):
            adapter.run(fetcher)

    def test_the_error_tells_you_not_to_put_it_in_config(self, monkeypatch, tmp_path):
        monkeypatch.delenv("ACTIVE_API_KEY", raising=False)
        adapter = ActiveSearchAdapter({"id": "s", "params": {"org_id": "x"}})
        with (
            Fetcher(tmp_path / "raw", delay_seconds=0.0) as fetcher,
            pytest.raises(AdapterError, match="committed"),
        ):
            adapter.run(fetcher)

    def test_empty_params_is_refused(self, monkeypatch, tmp_path):
        """An unbounded query would burn the daily quota on irrelevant assets."""
        monkeypatch.setenv("ACTIVE_API_KEY", SECRET)
        adapter = ActiveSearchAdapter({"id": "s", "params": {}})
        with (
            Fetcher(tmp_path / "raw", delay_seconds=0.0) as fetcher,
            pytest.raises(AdapterError, match="params"),
        ):
            adapter.run(fetcher)

    def test_cache_metadata_never_contains_the_key(self, monkeypatch, tmp_path):
        monkeypatch.setenv("ACTIVE_API_KEY", SECRET)
        run_adapter([payload([asset()])], monkeypatch=monkeypatch, tmp_path=tmp_path)
        written = "".join(
            path.read_text() for path in (tmp_path / "raw").glob("*.meta.json")
        )
        assert written, "expected cache metadata to have been written"
        assert SECRET not in written


class TestFieldMapping:
    @pytest.fixture(autouse=True)
    def _key(self, monkeypatch):
        monkeypatch.setenv("ACTIVE_API_KEY", SECRET)

    def test_the_documented_shape_maps_cleanly(self, monkeypatch, tmp_path):
        sessions, _ = run_adapter([payload([asset()])], tmp_path=tmp_path)
        assert len(sessions) == 1
        s = sessions[0]
        assert s.title == "Spring Break Art Camp"
        assert s.start_date == date(2027, 4, 5)
        assert s.end_date == date(2027, 4, 9)
        assert (s.min_age, s.max_age) == (6, 11)
        assert s.registration_status is RegistrationStatus.OPEN
        assert s.registration_opens == date(2027, 1, 15)
        assert s.provider_slug == "callanwolde"

    def test_an_asset_without_a_start_date_is_skipped(self, tmp_path):
        """It cannot be placed on a break calendar, same rule as jsonld."""
        sessions, _ = run_adapter(
            [payload([asset(activityStartDate="")])], tmp_path=tmp_path
        )
        assert sessions == []

    def test_a_missing_end_date_falls_back_to_the_start(self, tmp_path):
        sessions, _ = run_adapter(
            [payload([asset(activityEndDate=None)])], tmp_path=tmp_path
        )
        assert sessions[0].end_date == date(2027, 4, 5)

    def test_empty_string_ages_become_none_not_zero(self, tmp_path):
        """ACTIVE sends "" for unset ages; 0 would read as "newborns welcome"."""
        sessions, _ = run_adapter(
            [payload([asset(regReqMinAge="", regReqMaxAge="", assetName="Camp")])],
            tmp_path=tmp_path,
        )
        assert sessions[0].min_age is None

    def test_ages_fall_back_to_prose_when_the_fields_are_empty(self, tmp_path):
        sessions, _ = run_adapter(
            [
                payload(
                    [
                        asset(
                            regReqMinAge="",
                            regReqMaxAge="",
                            assetDescriptions=[{"description": "For ages 8-12."}],
                        )
                    ]
                )
            ],
            tmp_path=tmp_path,
        )
        assert (sessions[0].min_age, sessions[0].max_age) == (8, 12)

    def test_price_is_read_from_child_components(self, tmp_path):
        """The docs are explicit that parent assetPrices is often empty."""
        sessions, _ = run_adapter(
            [
                payload(
                    [
                        asset(
                            assetPrices=[],
                            assetComponents=[
                                {"assetPrices": [{"amount": "395.00"}]},
                                {"assetPrices": [{"amount": "310.00"}]},
                            ],
                        )
                    ]
                )
            ],
            tmp_path=tmp_path,
        )
        assert sessions[0].price_usd == 310.0

    def test_no_price_is_none_rather_than_zero(self, tmp_path):
        """None renders as "not stated"; 0.0 would claim the camp is free."""
        sessions, _ = run_adapter([payload([asset(assetPrices=[])])], tmp_path=tmp_path)
        assert sessions[0].price_usd is None

    def test_unmapped_sales_status_becomes_unknown(self, tmp_path, caplog):
        with caplog.at_level(logging.WARNING):
            sessions, _ = run_adapter(
                [payload([asset(salesStatus="something-new")])], tmp_path=tmp_path
            )
        assert sessions[0].registration_status is RegistrationStatus.UNKNOWN

    def test_seo_url_is_preferred_over_the_raw_one(self, tmp_path):
        sessions, _ = run_adapter(
            [
                payload(
                    [asset(assetSeoUrls=[{"urlAdr": "https://www.active.com/nice-slug"}])]
                )
            ],
            tmp_path=tmp_path,
        )
        assert str(sessions[0].url) == "https://www.active.com/nice-slug"


class TestPagination:
    @pytest.fixture(autouse=True)
    def _key(self, monkeypatch):
        monkeypatch.setenv("ACTIVE_API_KEY", SECRET)

    def test_walks_pages_until_total_is_reached(self, tmp_path):
        page1 = payload([asset(assetGuid=f"g{i}") for i in range(2)], total=3)
        page2 = payload([asset(assetGuid="g2")], total=3)
        sessions, calls = run_adapter(
            [page1, page2], config={"per_page": 2}, tmp_path=tmp_path
        )
        assert len(sessions) == 3
        assert len(calls) == 2

    def test_stops_on_an_empty_page(self, tmp_path):
        sessions, calls = run_adapter(
            [payload([asset()], total=999)], config={"per_page": 1}, tmp_path=tmp_path
        )
        assert len(sessions) == 1
        assert len(calls) == 2  # one real page, one empty that ends the loop

    def test_duplicate_guids_across_pages_are_dropped(self, tmp_path):
        same = payload([asset(assetGuid="dup")], total=4)
        sessions, _ = run_adapter(
            [same, same, payload([])], config={"per_page": 1}, tmp_path=tmp_path
        )
        assert len(sessions) == 1

    def test_page_ceiling_is_enforced(self, tmp_path, caplog):
        """A runaway query must cost a bounded amount of quota."""
        pages = [payload([asset(assetGuid=f"g{i}")], total=10_000) for i in range(10)]
        with caplog.at_level(logging.WARNING):
            _, calls = run_adapter(
                pages, config={"per_page": 1, "max_pages": 3}, tmp_path=tmp_path
            )
        assert len(calls) == 3
        assert any("ceiling" in r.message for r in caplog.records)


class TestBadResponses:
    @pytest.fixture(autouse=True)
    def _key(self, monkeypatch):
        monkeypatch.setenv("ACTIVE_API_KEY", SECRET)

    def test_non_json_is_a_source_level_failure(self, tmp_path):
        def handler(request):
            return httpx.Response(200, text="<html>maintenance</html>")

        client = httpx.Client(transport=httpx.MockTransport(handler))
        adapter = ActiveSearchAdapter({"id": "s", "params": {"org_id": "x"}})
        with (
            Fetcher(tmp_path / "raw", delay_seconds=0.0, client=client) as fetcher,
            pytest.raises(AdapterError, match="non-JSON"),
        ):
            adapter.run(fetcher)

    def test_the_failure_message_does_not_carry_the_key(self, tmp_path):
        def handler(request):
            return httpx.Response(200, text="nope")

        client = httpx.Client(transport=httpx.MockTransport(handler))
        adapter = ActiveSearchAdapter({"id": "s", "params": {"org_id": "x"}})
        with (
            Fetcher(tmp_path / "raw", delay_seconds=0.0, client=client) as fetcher,
            pytest.raises(AdapterError) as caught,
        ):
            adapter.run(fetcher)
        assert SECRET not in str(caught.value)

    def test_a_json_array_is_rejected(self, tmp_path):
        def handler(request):
            return httpx.Response(200, json=[1, 2, 3])

        client = httpx.Client(transport=httpx.MockTransport(handler))
        adapter = ActiveSearchAdapter({"id": "s", "params": {"org_id": "x"}})
        with (
            Fetcher(tmp_path / "raw", delay_seconds=0.0, client=client) as fetcher,
            pytest.raises(AdapterError, match="expected an object"),
        ):
            adapter.run(fetcher)

    def test_garbage_rows_are_skipped_not_fatal(self, tmp_path):
        """One bad listing must not lose the good ones on the same page."""
        sessions, _ = run_adapter(
            [payload(["not a dict", asset(), {"assetName": "no date"}])],
            tmp_path=tmp_path,
        )
        assert len(sessions) == 1


def test_the_request_actually_carries_the_key(monkeypatch, tmp_path):
    """Redaction must not have broken the outgoing request."""
    monkeypatch.setenv("ACTIVE_API_KEY", SECRET)
    _, calls = run_adapter([payload([asset()])], tmp_path=tmp_path)
    assert f"api_key={SECRET}" in str(calls[0])
    assert json  # keeps the import honest for readers of this file
