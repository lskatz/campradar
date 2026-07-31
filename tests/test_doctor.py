"""Tests for ACTIVE response triage.

These matter more than most tests here, because the thing being tested is a
claim about a failure nobody has been able to reproduce on demand. The doctor's
whole value is that its verdict is trustworthy; if it says "the problem is
upstream", that has to be right, because the alternative is spending an evening
tuning `params` that were never the issue.

So the cases below are the actual observed shapes: the header form from
`docs/active-api.md`, and the headerless XML/HTML form that ACTIVE's forum
reports show, which the adapter's existing header-only check would miss.
"""

from __future__ import annotations

import json

import pytest

from campradar.doctor import Diagnosis, Verdict, classify, interpret


class TestDeveloperInactive:
    """The failure this repo has actually hit."""

    def test_the_documented_header_form(self):
        d = classify(
            403,
            {
                "X-Mashery-Error-Code": "ERR_403_DEVELOPER_INACTIVE",
                "X-Error-Detail-Header": "Account Inactive",
            },
            "",
        )
        assert d.verdict is Verdict.DEVELOPER_INACTIVE

    def test_headers_are_matched_case_insensitively(self):
        d = classify(403, {"x-mashery-error-code": "ERR_403_DEVELOPER_INACTIVE"}, "")
        assert d.verdict is Verdict.DEVELOPER_INACTIVE

    @pytest.mark.parametrize(
        "body",
        [
            "<h1>Developer Inactive</h1>",
            "<error><message>Developer Inactive</message></error>",
            "Developer Inactive",
        ],
    )
    def test_the_body_only_form_the_forum_reports(self, body):
        """No Mashery header at all — the case the adapter currently misses.

        Several forum reports show the refusal arriving as a body with no
        useful header. Classifying on headers alone would call this a plain
        key rejection and send the user to check a key that is fine.
        """
        assert classify(403, {}, body).verdict is Verdict.DEVELOPER_INACTIVE

    def test_it_does_not_collapse_into_key_rejected(self):
        """The two 403s must stay distinguishable — they have different fixes."""
        inactive = classify(403, {"X-Mashery-Error-Code": "ERR_403_DEVELOPER_INACTIVE"}, "")
        rejected = classify(403, {"X-Mashery-Error-Code": "ERR_403_NOT_AUTHORIZED"}, "")
        assert inactive.verdict is not rejected.verdict
        assert inactive.next_step != rejected.next_step


class TestSuccessIsSplit:
    def test_results_present(self):
        d = classify(200, {}, json.dumps({"total_results": 12, "results": []}))
        assert d.verdict is Verdict.OK
        assert d.total_results == 12

    def test_zero_results_is_not_the_same_as_working(self):
        """A 200 with nothing in it is the ambiguous case worth naming.

        It is what you get both when a provider is absent from the index and
        when `params` are too narrow, and conflating either with a hard failure
        is what sends people to re-check credentials that were never wrong.
        """
        d = classify(200, {}, json.dumps({"total_results": 0}))
        assert d.verdict is Verdict.OK_BUT_EMPTY
        assert d.ok, "an empty result set still proves the credentials work"

    def test_a_200_carrying_html_is_not_success(self):
        assert classify(200, {}, "<html>maintenance</html>").verdict is Verdict.NOT_JSON

    def test_a_200_without_the_expected_fields_is_not_success(self):
        """Mashery returns 200-with-an-error-body in some configurations."""
        d = classify(200, {}, json.dumps({"error": "something"}))
        assert d.verdict is Verdict.NOT_JSON

    def test_results_list_stands_in_for_a_missing_total(self):
        d = classify(200, {}, json.dumps({"results": [{"assetName": "Camp"}]}))
        assert d.verdict is Verdict.OK
        assert d.total_results == 1


class TestOtherStatuses:
    @pytest.mark.parametrize(
        "status,expected",
        [
            (401, Verdict.KEY_REJECTED),
            (403, Verdict.KEY_REJECTED),
            (429, Verdict.RATE_LIMITED),
            (500, Verdict.SERVER_ERROR),
            (503, Verdict.SERVER_ERROR),
            (418, Verdict.UNKNOWN),
        ],
    )
    def test_status_mapping(self, status, expected):
        assert classify(status, {}, "").verdict is expected

    def test_every_verdict_has_a_next_step(self):
        """A verdict with no action attached is a dead end for the user."""
        for verdict in Verdict:
            assert Diagnosis(verdict=verdict, status=0).next_step


class TestBodyExcerpt:
    def test_excerpt_is_bounded(self):
        d = classify(500, {}, "x" * 5000)
        assert len(d.excerpt) <= 400

    def test_excerpt_is_kept_for_failures(self):
        assert "maintenance" in classify(200, {}, "<html>maintenance</html>").excerpt


class TestInterpretation:
    """The comparison is the point: it says whose problem this is."""

    OK = Diagnosis(verdict=Verdict.OK, status=200, total_results=5)
    EMPTY = Diagnosis(verdict=Verdict.OK_BUT_EMPTY, status=200, total_results=0)
    DEAD = Diagnosis(verdict=Verdict.DEVELOPER_INACTIVE, status=403)

    def test_both_failing_points_away_from_this_repo(self):
        text = interpret(self.DEAD, self.DEAD)
        assert "upstream of campradar" in text

    def test_sample_working_points_at_our_params(self):
        text = interpret(self.OK, self.DEAD)
        assert "fixable here" in text

    def test_empty_result_is_reported_as_credentials_fine(self):
        text = interpret(self.OK, self.EMPTY)
        assert "Credentials are good" in text

    def test_both_working_says_so(self):
        assert "working" in interpret(self.OK, self.OK).lower()


class TestTheNetworkGuardItself:
    """The guard is only worth having if it actually fires."""

    def test_a_real_connection_is_refused(self):
        import socket

        from conftest import NetworkAccessAttempted

        with pytest.raises(NetworkAccessAttempted):
            socket.create_connection(("example.com", 80), timeout=1)

    def test_mock_transport_is_unaffected(self):
        """The guard must not break the pattern every adapter test relies on."""
        import httpx

        client = httpx.Client(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"ok": True}))
        )
        assert client.get("https://api.amp.active.com/v2/search").json() == {"ok": True}
