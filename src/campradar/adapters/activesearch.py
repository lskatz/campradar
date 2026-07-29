"""Reads sessions from the ACTIVE Network Activity Search API v2.

Why an API adapter at all
-------------------------
Two of the configured providers publish through Active Network products, and
both are unscrapeable: DeKalb County's ActiveNet page returns an empty SPA
shell, and Callanwolde's own site is prose with the real listings on a separate
registration platform. Active publishes a documented, keyed, rate-limited
search API covering "Youth Camps" and "Parks & Recreation" assets, which is the
sanctioned way in and does not depend on anyone's HTML staying put.

    Docs: https://developer.active.com/docs/v2_Activity_API_Search
    Endpoint: https://api.amp.active.com/v2/search
    Auth: `api_key` query parameter (there is no header form)
    Limits: 2 requests/second, 500,000/day

An important caveat, unresolved at time of writing
--------------------------------------------------
It is *not* established that ActiveNet municipal instances are indexed in this
API. Active Network sells several products, and this search index is fed from
ACTIVE.com assets. A customer on the docs page asked precisely this — key
works, 200 response, zero results for their ActiveNet organisation — and was
never answered. So this adapter may cover Callanwolde and not DeKalb.

`campradar active-discover` exists to answer that empirically rather than by
assumption. Run it before enabling a source here.

Credentials
-----------
The key is read from the environment, never from `config/sources.yaml`, because
that file is committed to a public repository. Requests carry the key in the
query string, so every code path that writes a URL down goes through
`campradar.redact` — see that module's docstring for the accident this avoids.

Confidence
----------
The response mapping below follows the documented sample response. Fields the
docs show as sometimes-empty (notably prices, which the docs say live on child
`assetComponents` rather than the parent) are handled defensively: unknown
shapes are skipped and logged rather than guessed at, and unrecognised
`salesStatus` values are logged once so they can be added deliberately.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from datetime import date
from typing import Any

import httpx

from ..fetch import Fetcher
from ..models import CampSession, RegistrationStatus
from .base import Adapter, AdapterError
from .jsonld import parse_age_text

__all__ = ["ActiveSearchAdapter", "api_base", "build_search_url"]

log = logging.getLogger(__name__)

#: https, unlike the http:// shown in Active's own documentation. The key
#: travels in the query string, so TLS is not optional.
API_BASE = "https://api.amp.active.com/v2/search"

#: Overridable so this code can be run against a local fixture server. Without
#: a seam here, the only way to learn whether the adapter works is to spend a
#: real key against a live quota — which is how `active-discover` came to ship
#: without ever once having been executed.
API_BASE_ENV = "ACTIVE_API_BASE"

#: Active's documented ceiling is 2 requests/second. The Fetcher's default
#: per-host delay is 1.5s, comfortably under, but a source may override it and
#: this is the number to respect if anyone ever does.
MIN_SECONDS_BETWEEN_CALLS = 0.5

#: Stop paginating here no matter what `total_results` claims. A misconfigured
#: query ("every activity in the United States") should waste one page of quota
#: and produce a loud warning, not silently spend the daily allowance.
MAX_PAGES = 20

DEFAULT_PER_PAGE = 50

#: `salesStatus` values seen in Active's documentation and observed in the
#: wild. Anything absent maps to UNKNOWN and is logged, so the vocabulary grows
#: on evidence rather than on guesses.
_SALES_STATUS = {
    "registration-open": RegistrationStatus.OPEN,
    "registration-closed": RegistrationStatus.CLOSED,
    "registration-not-open": RegistrationStatus.NOT_YET_OPEN,
    "registration-pending": RegistrationStatus.NOT_YET_OPEN,
    "sold-out": RegistrationStatus.FULL,
    "event-cancelled": RegistrationStatus.CLOSED,
    "event-complete": RegistrationStatus.CLOSED,
}

_unknown_statuses_seen: set[str] = set()


def api_base(config: dict[str, Any] | None = None) -> str:
    """Endpoint to use: explicit config, then $ACTIVE_API_BASE, then the real one."""
    if config and config.get("api_base"):
        return str(config["api_base"])
    return os.environ.get(API_BASE_ENV, "").strip() or API_BASE


def build_search_url(
    params: dict[str, Any], api_key: str, base: str | None = None
) -> str:
    """Assemble a search URL.

    Separated from the adapter so it can be tested, and reused by
    `active-discover`, without a Fetcher or an environment variable.

        >>> url = build_search_url({"near": "Decatur,GA,US", "kids": "true"}, "K")
        >>> url.startswith("https://api.amp.active.com/v2/search?")
        True
        >>> "api_key=K" in url
        True

    Values are passed through `httpx`-compatible encoding by the caller; here we
    only need ordering to be deterministic so that cache keys are stable across
    runs. Without sorting, a dict reordering would silently invalidate every
    cached response.

        >>> a = build_search_url({"b": "2", "a": "1"}, "K")
        >>> a == build_search_url({"a": "1", "b": "2"}, "K")
        True
    """
    from urllib.parse import urlencode

    ordered = {k: params[k] for k in sorted(params) if params[k] not in (None, "")}
    # api_key goes last so the readable part of the URL stays readable, and so
    # a redacted log line still shows the whole query.
    return f"{base or api_base()}?{urlencode(ordered)}&api_key={api_key}"


def _first(items: Any, *keys: str) -> str | None:
    """First non-empty value at `keys` across a list of dicts."""
    if not isinstance(items, list):
        return None
    for item in items:
        if not isinstance(item, dict):
            continue
        node: Any = item
        for key in keys:
            node = node.get(key) if isinstance(node, dict) else None
        if isinstance(node, str) and node.strip():
            return node.strip()
    return None


def _parse_api_date(value: Any) -> date | None:
    """Read Active's `2027-04-05T09:00:00` timestamps as dates.

        >>> _parse_api_date("2027-04-05T09:00:00")
        datetime.date(2027, 4, 5)
        >>> _parse_api_date("2027-04-05")
        datetime.date(2027, 4, 5)
        >>> _parse_api_date("") is None
        True
        >>> _parse_api_date(None) is None
        True

    Times are discarded deliberately. They are in the asset's local timezone,
    which Active reports separately and inconsistently, and `CampSession` keys
    on dates. Half-parsing a timezone is worse than not trying.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError:
        return None


def _parse_int(value: Any) -> int | None:
    """Active reports ages as strings, and as `""` when unset."""
    if value in (None, ""):
        return None
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if 0 <= number <= 21 else None


def _extract_price(asset: dict[str, Any]) -> float | None:
    """Best-effort price.

    Active's docs are explicit that the parent asset's `assetPrices` is often
    empty and the real figures sit on child `assetComponents`. Both are checked,
    and the *lowest* non-zero amount wins, since a camp week typically prices as
    member/non-member pairs and the lower bound is the honest headline.

    Returns None rather than 0.0 when nothing is found: the UI renders None as
    "not stated" and 0.0 as free, and those are very different claims.
    """
    candidates: list[float] = []
    pools = [asset.get("assetPrices")]
    for component in asset.get("assetComponents") or []:
        if isinstance(component, dict):
            pools.append(component.get("assetPrices"))

    for pool in pools:
        if not isinstance(pool, list):
            continue
        for entry in pool:
            if not isinstance(entry, dict):
                continue
            for key in ("amount", "price", "priceAmt", "unitPrice"):
                raw = entry.get(key)
                if raw in (None, ""):
                    continue
                try:
                    value = float(str(raw).replace("$", "").replace(",", "").strip())
                except ValueError:
                    continue
                if value > 0:
                    candidates.append(value)
                break

    return min(candidates) if candidates else None


def _extract_status(asset: dict[str, Any]) -> RegistrationStatus:
    """Map `salesStatus`, logging any value we have not accounted for."""
    raw = asset.get("salesStatus")
    if not isinstance(raw, str) or not raw.strip():
        return RegistrationStatus.UNKNOWN
    token = raw.strip().lower()
    if token in _SALES_STATUS:
        return _SALES_STATUS[token]
    if token not in _unknown_statuses_seen:
        _unknown_statuses_seen.add(token)
        log.warning(
            "active: unmapped salesStatus %r — treating as unknown; add it to _SALES_STATUS",
            token,
        )
    return RegistrationStatus.UNKNOWN


class ActiveSearchAdapter(Adapter):
    """Pulls camp sessions from Active's Activity Search API.

    Config keys:
        id, provider_slug   as for every adapter
        api_key_env         env var holding the key (default ACTIVE_API_KEY)
        params              dict passed through to the API as query parameters
        per_page            results per request (default 50)
        max_pages           safety ceiling (default 20)

    `params` is passed through rather than wrapped, so any parameter Active
    documents works without a code change here. A source is therefore usually a
    config edit, which is the same property the `jsonld` adapter has.

    Example source:

        - id: callanwolde-active
          provider_slug: callanwolde
          adapter: activesearch
          params:
            org_id: <organizationGuid from active-discover>
            kids: "true"
            start_date: 2026-08-01..2027-08-31
            exclude_children: "true"
    """

    name = "activesearch"

    def _api_key(self) -> str:
        env_name = self.config.get("api_key_env", "ACTIVE_API_KEY")
        key = os.environ.get(env_name, "").strip()
        if not key:
            # AdapterError, not a bare raise: a missing credential makes the
            # whole source unusable, which is exactly the source-level failure
            # the base class is designed to report and carry on from.
            raise AdapterError(
                f"{self.source_id}: ${env_name} is not set. "
                f"Export it in your shell; do not put it in config/sources.yaml, "
                f"which is committed."
            )
        return key

    def parse(self, fetcher: Fetcher) -> Iterator[CampSession]:
        api_key = self._api_key()
        params: dict[str, Any] = dict(self.config.get("params") or {})
        if not params:
            raise AdapterError(
                f"{self.source_id}: no 'params' configured; "
                f"refusing to query the whole of ACTIVE"
            )

        base = api_base(self.config)
        per_page = int(self.config.get("per_page", DEFAULT_PER_PAGE))
        max_pages = int(self.config.get("max_pages", MAX_PAGES))

        seen_guids: set[str] = set()
        page = 1
        while page <= max_pages:
            query = {**params, "per_page": per_page, "current_page": page}
            url = build_search_url(query, api_key, base)
            payload = self._fetch_json(fetcher, url)

            results = payload.get("results")
            if not isinstance(results, list) or not results:
                break

            for asset in results:
                if not isinstance(asset, dict):
                    continue
                # Active returns parent assets alongside their components, and
                # the same asset can appear on a page boundary during
                # pagination. Dedupe on the stable GUID rather than on title,
                # which repeats legitimately across weeks.
                guid = str(asset.get("assetGuid") or "")
                if guid and guid in seen_guids:
                    continue
                if guid:
                    seen_guids.add(guid)
                session = self._to_session(asset)
                if session is not None:
                    yield session

            total = payload.get("total_results")
            if isinstance(total, int) and page * per_page >= total:
                break
            page += 1
        else:
            log.warning(
                "%s: stopped at the %d-page ceiling; narrow 'params' if you expected more",
                self.source_id,
                max_pages,
            )

    def _fetch_json(self, fetcher: Fetcher, url: str) -> dict[str, Any]:
        """Fetch and decode one page, failing at source level on bad JSON."""
        import json

        try:
            result = fetcher.get(url)
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            if code in (401, 403):
                # ACTIVE sits behind Mashery, which explains itself in headers.
                # Reading them is the difference between "your key is wrong"
                # (edit config) and "your account is not activated" (log in to
                # the developer portal) — two completely different next steps
                # that a generic 403 collapses into one dead end.
                mashery = exc.response.headers.get("X-Mashery-Error-Code", "")
                detail = exc.response.headers.get("X-Error-Detail-Header", "")
                if "DEVELOPER_INACTIVE" in mashery.upper() or "inactive" in detail.lower():
                    raise AdapterError(
                        f"{self.source_id}: the key was accepted but the ACTIVE "
                        f"developer account is not active ({mashery or detail}). "
                        f"This is not a config problem — activate the account at "
                        f"developer.active.com (check for an unverified email or an "
                        f"application still awaiting approval), then re-enable this "
                        f"source."
                    ) from exc
                raise AdapterError(
                    f"{self.source_id}: ACTIVE rejected the request (HTTP {code}"
                    f"{', ' + mashery if mashery else ''}). "
                    f"Check $ACTIVE_API_KEY is the Search API key."
                ) from exc
            if code == 429:
                raise AdapterError(
                    f"{self.source_id}: ACTIVE rate limit hit (HTTP 429). "
                    f"Raise the Fetcher delay above 0.5s or lower per_page."
                ) from exc
            raise AdapterError(f"{self.source_id}: ACTIVE returned HTTP {code}") from exc

        try:
            payload = json.loads(result.text)
        except json.JSONDecodeError as exc:
            # No URL in the message: it carries the key, and while redaction
            # would catch it, not putting it there is better than relying on
            # the net below.
            raise AdapterError(f"{self.source_id}: API returned non-JSON ({exc})") from exc
        if not isinstance(payload, dict):
            raise AdapterError(
                f"{self.source_id}: API returned {type(payload).__name__}, expected an object"
            )
        return payload

    def _to_session(self, asset: dict[str, Any]) -> CampSession | None:
        """Convert one asset. Returns None when it can't be a session."""
        start = _parse_api_date(asset.get("activityStartDate"))
        if start is None:
            # No date means it cannot be placed on a break calendar. Same rule
            # as the jsonld adapter.
            return None
        end = _parse_api_date(asset.get("activityEndDate")) or start
        if end < start:
            end = start

        title = str(asset.get("assetName") or "").strip()
        if not title:
            return None

        description = _first(asset.get("assetDescriptions"), "description")

        min_age = _parse_int(asset.get("regReqMinAge"))
        max_age = _parse_int(asset.get("regReqMaxAge"))
        if min_age is None and max_age is None:
            min_age, max_age = parse_age_text(f"{title} {description or ''}")
        if min_age is not None and max_age is not None and max_age < min_age:
            min_age, max_age = max_age, min_age

        url = (
            _first(asset.get("assetSeoUrls"), "urlAdr")
            or (asset.get("urlAdr") if isinstance(asset.get("urlAdr"), str) else None)
            or (
                asset.get("registrationUrlAdr")
                if isinstance(asset.get("registrationUrlAdr"), str)
                else None
            )
        )
        if not url:
            return None

        return CampSession(
            provider_slug=self.provider_slug,
            title=title,
            start_date=start,
            end_date=end,
            min_age=min_age,
            max_age=max_age,
            price_usd=_extract_price(asset),
            registration_status=_extract_status(asset),
            registration_opens=_parse_api_date(asset.get("salesStartDate")),
            url=url,
            description=description,
            source_id=self.source_id,
        )
