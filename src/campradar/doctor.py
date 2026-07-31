"""Triage for the ACTIVE Activity Search API.

Why this exists
---------------
`docs/active-api.md` records a 403 with `ERR_403_DEVELOPER_INACTIVE` and treats
it as settled: activate the account and re-enable the source. That turns out to
be optimistic. ACTIVE's own forum has unresolved reports of this exact code
going back nine years, including from accounts ACTIVE staff confirmed as valid,
and the most recent one is weeks old. So "the account is active" and "the key is
correct" do not between them predict success, and the adapter's error message —
which sends you to developer.active.com — may send you somewhere that cannot
help.

What this module does about it is narrow: it separates the failure into classes
that have *different next actions*, and it does so by comparing a call we
control against ACTIVE's own documented sample call. If the documented sample
fails with the same key, the problem is upstream of anything in this repo and no
amount of parameter tuning will fix it. That single distinction is the one the
existing error handling cannot make.

Everything here is a pure function over (status, headers, body). The network
lives in `cli.cmd_active_doctor`; this part is unit-testable offline, which
matters because the whole point is to be trustworthy about a failure we cannot
reproduce locally.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum

__all__ = ["Verdict", "Diagnosis", "classify", "MASHERY_CODE_HEADER", "MASHERY_DETAIL_HEADER"]

MASHERY_CODE_HEADER = "X-Mashery-Error-Code"
MASHERY_DETAIL_HEADER = "X-Error-Detail-Header"


class Verdict(StrEnum):
    """Outcome classes, chosen so that each implies a different next step."""

    OK = "ok"
    OK_BUT_EMPTY = "ok-but-empty"
    DEVELOPER_INACTIVE = "developer-inactive"
    KEY_REJECTED = "key-rejected"
    RATE_LIMITED = "rate-limited"
    NOT_JSON = "not-json"
    SERVER_ERROR = "server-error"
    UNKNOWN = "unknown"


#: What to do about each verdict. Kept beside the enum so a new verdict cannot
#: be added without someone deciding what the user should do about it.
NEXT_STEP = {
    Verdict.OK: "Working. Fill in params.org_id and enable the source.",
    Verdict.OK_BUT_EMPTY: (
        "The API answered but holds nothing matching. Widen --radius, drop "
        "--start-date, or try --no-kids. If the documented sample call below "
        "also came back empty, the index itself is not returning data."
    ),
    Verdict.DEVELOPER_INACTIVE: (
        "ACTIVE's gateway is refusing at the developer-account level, not the "
        "key level. This is not fixable in this repo. Note that ACTIVE's forum "
        "has unresolved reports of this on accounts ACTIVE confirmed as valid, "
        "so 'my account is active' does not rule it out — open a ticket and "
        "quote the Mashery code."
    ),
    Verdict.KEY_REJECTED: (
        "The key itself was refused. Check you exported the Activity Search "
        "key and not another product's key or the API secret."
    ),
    Verdict.RATE_LIMITED: "Slow down: keep at least 0.5s between calls.",
    Verdict.NOT_JSON: (
        "A non-JSON body usually means a gateway error page rather than the "
        "API. The body excerpt below is the evidence — Mashery returns XML for "
        "some refusals, which is itself diagnostic."
    ),
    Verdict.SERVER_ERROR: "ACTIVE-side fault. Retry later; nothing to change here.",
    Verdict.UNKNOWN: "Unrecognised response. The excerpt below is the evidence.",
}


@dataclass(frozen=True)
class Diagnosis:
    """One classified response."""

    verdict: Verdict
    status: int
    total_results: int | None = None
    mashery_code: str = ""
    detail: str = ""
    excerpt: str = ""

    @property
    def next_step(self) -> str:
        return NEXT_STEP[self.verdict]

    @property
    def ok(self) -> bool:
        return self.verdict in (Verdict.OK, Verdict.OK_BUT_EMPTY)


def _header(headers: dict[str, str], name: str) -> str:
    """Case-insensitive header lookup.

    httpx normalises case; a fixture dict written by hand may not, and this
    module is meant to be driven from tests as well as from a live response.
    """
    lowered = {k.lower(): v for k, v in headers.items()}
    return str(lowered.get(name.lower(), "") or "").strip()


def classify(status: int, headers: dict[str, str], body: str) -> Diagnosis:
    """Classify one ACTIVE response.

    The Mashery headers are read before the status code is trusted, because
    the same 403 means two different things:

        >>> d = classify(403, {"X-Mashery-Error-Code": "ERR_403_DEVELOPER_INACTIVE"}, "")
        >>> d.verdict
        <Verdict.DEVELOPER_INACTIVE: 'developer-inactive'>

        >>> classify(403, {"X-Mashery-Error-Code": "ERR_403_NOT_AUTHORIZED"}, "").verdict
        <Verdict.KEY_REJECTED: 'key-rejected'>

    Mashery also signals inactivity in an XML body with no useful header at
    all, which is what several of the unresolved forum reports show:

        >>> body = "<h1>Developer Inactive</h1>"
        >>> classify(403, {}, body).verdict
        <Verdict.DEVELOPER_INACTIVE: 'developer-inactive'>

    A 200 is split on whether anything came back, because "no camps here" and
    "this provider is not in the index" need different responses from the user:

        >>> classify(200, {}, '{"total_results": 12}').verdict
        <Verdict.OK: 'ok'>
        >>> classify(200, {}, '{"total_results": 0}').verdict
        <Verdict.OK_BUT_EMPTY: 'ok-but-empty'>

    And a 200 carrying HTML is not a success:

        >>> classify(200, {}, "<html>maintenance</html>").verdict
        <Verdict.NOT_JSON: 'not-json'>
    """
    code = _header(headers, MASHERY_CODE_HEADER)
    detail = _header(headers, MASHERY_DETAIL_HEADER)
    excerpt = body[:400].strip()

    haystack = f"{code} {detail} {body[:2000]}".lower()
    inactive = "developer_inactive" in haystack or "developer inactive" in haystack

    def made(verdict: Verdict, total: int | None = None) -> Diagnosis:
        return Diagnosis(
            verdict=verdict,
            status=status,
            total_results=total,
            mashery_code=code,
            detail=detail,
            excerpt=excerpt,
        )

    if inactive:
        return made(Verdict.DEVELOPER_INACTIVE)
    if status in (401, 403):
        return made(Verdict.KEY_REJECTED)
    if status == 429:
        return made(Verdict.RATE_LIMITED)
    if status >= 500:
        return made(Verdict.SERVER_ERROR)

    if status == 200:
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, TypeError):
            return made(Verdict.NOT_JSON)
        if not isinstance(payload, dict):
            return made(Verdict.NOT_JSON)
        total = payload.get("total_results")
        if not isinstance(total, int):
            # Some Mashery refusals are 200-with-an-error-body. Absent the
            # field the response is not a search result, whatever the status.
            results = payload.get("results")
            total = len(results) if isinstance(results, list) else None
        if total is None:
            return made(Verdict.NOT_JSON)
        return made(Verdict.OK if total > 0 else Verdict.OK_BUT_EMPTY, total)

    return made(Verdict.UNKNOWN)


def interpret(sample: Diagnosis, ours: Diagnosis) -> str:
    """Compare ACTIVE's own documented call against ours, and say what it means.

    This is the whole reason the doctor makes two calls. On its own, a failure
    of the campradar-shaped query is ambiguous between "our params are wrong"
    and "this key cannot read this API". Running ACTIVE's published sample —
    a query ACTIVE itself asserts should work — resolves it.
    """
    if not sample.ok and not ours.ok:
        return (
            "ACTIVE's own documented sample call fails too, with the same key.\n"
            "  => The problem is upstream of campradar. No change to sources.yaml,\n"
            "     params, or the adapter will fix this. Take it to ACTIVE support."
        )
    if sample.ok and not ours.ok:
        return (
            "ACTIVE's documented sample works, but the campradar-shaped query does not.\n"
            "  => The key and account are fine; the query is the problem. This one is\n"
            "     fixable here — the params are the place to look."
        )
    if sample.ok and ours.verdict is Verdict.OK_BUT_EMPTY:
        return (
            "Both calls were accepted; ours simply matched nothing.\n"
            "  => Credentials are good and the adapter path works end to end. Widen the\n"
            "     search before concluding a provider is absent from the index."
        )
    if not sample.ok and ours.ok:
        return (
            "Ours works and the sample does not, which is unexpected — the sample\n"
            "  query is nine years old and may simply have aged out. Trust ours."
        )
    return "Both calls succeeded. The ACTIVE path is working."
