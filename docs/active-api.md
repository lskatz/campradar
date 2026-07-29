# The ACTIVE Network source

Two providers publish through Active Network products, and neither can be
scraped: DeKalb County's ActiveNet page is an empty SPA shell, and Callanwolde's
own site is prose with the real listings on a third-party platform. The
`activesearch` adapter reads Active's documented [Activity Search API
v2](https://developer.active.com/docs/v2_Activity_API_Search) instead.

## Your key never goes in a file

```bash
export ACTIVE_API_KEY=...        # in your shell profile, not in this repo
```

`config/sources.yaml` is committed to a public repository. The adapter reads
`$ACTIVE_API_KEY` and refuses to run without it, with an error that says so.

**`ACTIVE_API_SECRET` is not used.** Activity Search API v2 authenticates with
the `api_key` query parameter alone; there is no signed-request flow. Keep the
secret somewhere safe in case another Active API needs it, but nothing here
reads it.

### Why redaction exists

The key travels in the **query string** — Active offers no header form. That
means every request URL is a credential. Before this adapter existed,
`data/refresh.log` was tracked in git, `scripts/update.sh` teed run output into
it, and httpx logs full request URLs at INFO. Wiring up the API without fixing
that would have committed a live key to a public repo on the first refresh.

Two defences, because a pushed credential cannot be un-pushed:

1. `campradar.redact` scrubs credential-shaped substrings from log records,
   stdout, exception messages, and the fetch cache's metadata.
2. `data/refresh.log` is now gitignored and untracked.

If you ever see a real key in output, that is a bug worth fixing immediately.

## Step 1: find out what's actually there

Coverage is an open question, not a known quantity. Active sells several
products, and this search index is fed from ACTIVE.com assets. Whether ActiveNet
municipal instances are indexed is **unconfirmed** — a customer asked exactly
this on the docs page (key works, HTTP 200, zero results for their ActiveNet
organisation) and was never answered.

So measure before building:

```bash
campradar active-discover --near "Decatur,GA,US" --radius 25
```

That spends four cheap calls (`per_page=0`, facets only) and prints how many
kids' activities exist nearby, then breaks them down by organisation, source
system, and category. The **source system** breakdown is the interesting one: if
no ActiveNet-flavoured system appears, DeKalb County is not in this index and no
amount of parameter tuning will find it.

If you get zero results, widen before concluding anything:

```bash
campradar active-discover --radius 60 --no-kids     # drop the filters
```

## Step 2: pin a provider by GUID

```bash
campradar active-discover --org-query "Callanwolde"
```

This prints `organizationGuid` values. Put one in `params.org_id`:

```yaml
- id: callanwolde-active
  provider_slug: callanwolde
  adapter: activesearch
  enabled: true
  params:
    org_id: 0e1c...             # from active-discover
    kids: "true"
    exclude_children: "true"
    start_date: 2026-08-01..2027-08-31
    sort: date_asc
```

Prefer `org_id` over `query`. Active re-titles assets, so a keyword search
drifts; the organisation GUID is the provider itself.

## Configuration reference

| Key | Default | Notes |
|---|---|---|
| `api_key_env` | `ACTIVE_API_KEY` | env var holding the key |
| `params` | — | **required**; passed through to the API verbatim |
| `per_page` | 50 | results per request |
| `max_pages` | 20 | safety ceiling |

`params` is passed straight through, so any parameter Active documents works
without touching Python. Useful ones: `near`, `radius`, `lat_lon`, `org_id`,
`query`, `kids`, `category`, `start_date`, `end_date`, `sort`,
`exclude_children`, `registerable_only`.

Ranges use `start..end`, and either side may be omitted:
`start_date=2026-09-01..`, `start_date=..2027-01-01`.

An empty `params` is refused rather than treated as "everything" — an unbounded
query would spend the daily quota on activities in other states.

## Rate limits

Active documents 2 requests/second and 500,000/day. The `Fetcher`'s default
per-host delay is 1.5s, comfortably inside that, and `active-discover` uses
0.6s. If you raise either, keep the interval above 0.5s.

## Field mapping

| `CampSession` | ACTIVE asset field |
|---|---|
| `title` | `assetName` |
| `start_date` / `end_date` | `activityStartDate` / `activityEndDate` (date part) |
| `min_age` / `max_age` | `regReqMinAge` / `regReqMaxAge`, else parsed from prose |
| `price_usd` | lowest non-zero of `assetPrices` or `assetComponents[].assetPrices` |
| `registration_status` | `salesStatus` |
| `registration_opens` | `salesStartDate` |
| `url` | `assetSeoUrls[].urlAdr`, else `urlAdr`, else `registrationUrlAdr` |

Notes on the awkward parts:

- **Times are discarded.** They arrive in the asset's local timezone, which
  Active reports separately and inconsistently, and `CampSession` keys on dates.
  Half-parsing a timezone is worse than not trying.
- **Prices are often absent from the parent asset.** Active's docs say the real
  figures sit on child `assetComponents`; both are checked and the lowest
  non-zero wins, since camps commonly price as member/non-member pairs.
- **No price means `None`, not `0.0`.** The UI renders `None` as "not stated"
  and `0.0` as free, and those are very different claims.
- **Unrecognised `salesStatus` values log a warning** and map to `UNKNOWN`. The
  vocabulary in `_SALES_STATUS` grows on evidence, not guesses — if you see that
  warning, send the value and it gets added.

## Testing without spending a key

The endpoint is overridable, so the whole path can be exercised offline:

```bash
campradar active-discover --api-base http://127.0.0.1:8177/v2/search
```

or per-source in `sources.yaml` with `api_base:`, or globally with
`$ACTIVE_API_BASE`. This seam exists because the first version of
`active-discover` shipped without ever having been run — the only way to try it
was against a live quota with a real key, so nobody did.

## When a source returns nothing

`refresh` now separates three outcomes, because they need different fixes:

```
Sources: 1 produced camps, 2 returned nothing, 1 failed
  failed:  zoo-atlanta-camps
  nothing: dekalb-county, tucker-rec
```

- **produced camps** — working.
- **returned nothing** — fetched and parsed fine, but there was nothing there.
  For an ACTIVE source this usually means the `params` match no assets, or the
  provider genuinely is not in the index. Run `active-discover` to tell which.
- **failed** — an error. A rejected key now says so explicitly rather than
  surfacing as a generic source failure.

`sources_empty` is in `sessions.json` and on the dashboard too.

## Status

The adapter is tested against fixtures built from Active's documented sample
response, driven through `httpx.MockTransport` — pagination, deduplication,
JSON errors, and field mapping all covered offline in `tests/test_active.py`.

It has **not** been run against the live API. The response shape beyond the
documented sample is unverified, which is why unknown values are logged rather
than assumed. Expect at least one field to need adjusting on first contact.
