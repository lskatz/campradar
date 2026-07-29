# The Events Calendar source

Callanwolde's `/classes/camps/` page is prose. There is no schema.org markup and
never was, which is why `callanwolde-camps` fetched cleanly and produced zero
sessions run after run.

The fix wasn't a better parser. It was reading the response headers on that same
page:

```
X-Tec-Api-Root: https://callanwolde.org/wp-json/tribe/events/v1/
X-Tec-Api-Version: v1
```

The site runs **The Events Calendar**, and its REST API is public,
unauthenticated, documented, and filterable by date. No key, no quota, no
gateway, and the data comes from the provider rather than a middleman. Strictly
better than either scraping or going through ACTIVE.

## Is a provider eligible?

Either check response headers for `X-Tec-Api-Root`, or:

```bash
curl -s https://provider.example/wp-json/ | python3 -c \
  "import json,sys; print('tribe/events/v1' in json.load(sys.stdin)['namespaces'])"
```

Zoo Atlanta runs WordPress on the same host but does **not** have the plugin, so
it needs a different approach. Dunwoody is unchecked.

The endpoint self-documents — `GET /wp-json/tribe/events/v1/` returns the full
parameter schema. That is how this adapter's parameters were confirmed rather
than guessed.

## Before adding a source, measure

```bash
campradar tribe-discover https://callanwolde.org --search camp
```

```
-- everything in the window --
  3 event(s)

-- categories --
     37  concerts  (Concerts)
     12  exhibitions  (Exhibitions)
      2  camps  (Camps)

-- soonest events --
  2027-04-05  Spring Break Creative Camp     [camps]
  2026-12-28  Winter Break Clay Studio       [camps]
  2026-09-22  Jazz on the Lawn               [concerts]

-- matching search='camp' --
  1 event(s)
```

That last number is the point. Two camps exist; `search=camp` found one, because
"Winter Break Clay Studio" never says the word. **Never ship `search` as the
filter** — it is WordPress full-text, so it misses camps that don't say "camp"
and catches concerts that do.

An empty result is three different problems with three different fixes: the date
window, the search term, or the provider not putting camps in its calendar at
all. This command tells you which.

## Configuration

```yaml
- id: callanwolde-tribe
  provider_slug: callanwolde
  adapter: tribe
  enabled: true
  base_url: https://callanwolde.org
  params:
    search: camp
    start_date: 2026-08-01
    end_date: 2027-08-31
  # category_slugs: [camps]
```

| Key | Default | Notes |
|---|---|---|
| `base_url` | — | **required**; site root, no path |
| `params` | `{}` | passed through verbatim |
| `per_page` | 50 | results per request |
| `max_pages` | 20 | safety ceiling |
| `category_slugs` | — | keep only these categories (applied locally) |

Confirmed parameters: `page`, `per_page`, `start_date`, `end_date`,
`starts_before`, `starts_after`, `ends_before`, `ends_after`, `search`,
`categories`, `tags`, `venue`, `organizer`, `featured`, `status`, `ticketed`.

**On filtering.** `search` is the plugin's own full-text filter — cheap, but it
matches descriptions, so a concert whose blurb mentions "camp" gets through.
`category_slugs` is exact but happens after fetching. Prefer it once you know
the real slugs:

```bash
curl -s https://callanwolde.org/wp-json/tribe/events/v1/categories | python3 -m json.tool
```

Callanwolde's calendar carries concerts and galas alongside camps, so this
matters — a test run without the filter pulled in "Jazz on the Lawn".

## Field mapping

| `CampSession` | Event field |
|---|---|
| `title` | `title`, HTML stripped |
| `start_date` / `end_date` | `start_date` / `end_date`, date part |
| `min_age` / `max_age` | parsed from title + description |
| `price_usd` | lowest non-zero of `cost_details.values`, else parsed from `cost` |
| `url` | `url`, else `website` |
| `description` | `description`, else `excerpt`, HTML stripped |
| `registration_status` | always `UNKNOWN` |

Notes on the awkward parts:

- **`registration_status` is always UNKNOWN.** The plugin exposes no
  registration state. Inferring "open" because an event exists would be exactly
  the false confidence this project is meant to avoid.
- **HTML stripping uses a space separator.** Without it,
  `<p>ages</p><p>6-11</p>` collapses to `ages6-11` and the age parser misses it.
- **Prices prefer `cost_details.values`** over the `cost` display string, which
  can read `$395`, `310 - 340`, `Free`, or empty. Lowest non-zero wins, since
  camps price as member/non-member pairs.
- **No price gives `None`, not `0.0`.** The UI renders `None` as "not stated"
  and `0.0` as free. A genuine "Free" also lands on `None` today; that changes
  when a provider is seen publishing a structured zero.
- **A 404 past page 1 ends pagination quietly** — that's how the plugin signals
  the end of the archive. A 404 on page 1 fails loudly, because that means the
  `base_url` is wrong and it must not look like an empty calendar.

## Status

Request schema confirmed against a live discovery call. Response field names
follow the plugin's documented format and are read defensively. Exercised
end-to-end against a mock server: pagination, category filtering, both price
shapes, and age parsing out of HTML. 44 tests in `tests/test_tribe.py`.
