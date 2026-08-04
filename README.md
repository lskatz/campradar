# Camp Radar

Keeps track of which camps exist for DeKalb County school breaks — and, more
usefully, which ones are **new since last run**.

Built for a specific problem: metro Atlanta camps open registration between
September and February and sell out fast, and the hard part is not choosing
between camps, it is finding out a camp exists before it is full.

This is the command-line core, and nothing else. Three moving parts:

| File | What it is |
|---|---|
| `config/camps.yaml` | where to look for camps |
| `config/dates.yaml` | which days you need covered |
| `data/camps.json` | the local file, updated in place |

## Install

```sh
pip install -e ".[dev]"
pytest                    # 106 tests, under a second
```

## Use

```sh
campradar update          # fetch every enabled source, update data/camps.json
campradar list            # print the local file as TSV
```

`update` writes its summary to stderr and `list` writes to stdout, so this
stays clean:

```sh
campradar update && campradar list > camps.tsv
```

A run looks like:

```
  example-camps: 4 session(s)
4 new, 0 newly open, 0 disappeared (4 total) -> data/camps.json
  + 2026-10-05 Fall Break Discovery Camp
  + 2026-12-21 Winter Wonders Half-Week
```

## The TSV

One header row, tab-separated, empty string for anything missing. Column order
is the tool's public contract; new columns get appended at the end so that
anything parsing by position keeps working.

```
key  first_seen  is_new  start_date  end_date  breaks  needed_days
title  provider  source_id  min_age  max_age  daily_start  daily_end
price_usd  status  url
```

All dates are ISO-8601. `first_seen` is a full UTC timestamp.

`breaks` and `needed_days` are the two ways to ask about coverage, and they
deliberately disagree. `breaks` holds the slugs from `dates.yaml` that a
session overlaps. `needed_days` holds the actual days off it covers — so a
Saturday-to-Wednesday camp overlapping a Monday-to-Friday break shows five
session days but only three needed days.

Filtering is `awk` for now, scoped to a column rather than a bare `grep`,
since a naive line match on a date would also hit `start_date` and
`first_seen`:

```sh
# everything covering Fall Break
campradar list | awk -F'\t' 'NR==1 || $6 ~ /fall-break/'

# everything covering one exact day
campradar list | awk -F'\t' 'NR==1 || $7 ~ /2027-02-16/'

# anything at all covering a day you need
campradar list | awk -F'\t' 'NR==1 || $7 != ""'

# what showed up since last run
campradar list | awk -F'\t' 'NR==1 || $3 == 1'
```

A future `--break` / `--date` filter would do exactly this in Python. Same
semantics, no schema change.

## Adding a camp

Name the provider, then point a source at the page that lists their sessions:

```yaml
providers:
  - slug: dunwoody-nature-center
    name: Dunwoody Nature Center

sources:
  - id: dunwoody-camps
    provider_slug: dunwoody-nature-center
    adapter: jsonld
    enabled: true
    urls:
      - https://dunwoodynature.org/education/camp-programs/
```

There are two parsers. `jsonld` reads schema.org markup, which a surprising
number of sites publish for SEO without knowing it — try it first. Whether a
given site will work:

```sh
curl -s https://their-site.example/camps | grep -c 'application/ld+json'
```

If that is zero, no amount of configuration will help and the site needs its
own parser. `sources.enabled: false` retires a source without losing the
knowledge of it.

`recdesk` is the second parser, for RecDesk Community portals:

```yaml
  - id: tucker-rec
    provider_slug: tucker-rec
    adapter: recdesk
    enabled: true
    base_url: https://tucker.recdesk.com
    categories: ["9", "20"]      # 9 = Day Camp, 20 = Teen Camp
```

RecDesk cannot be scraped by fetching a page: the programme table arrives over
XHR and the active filter is keyed to `ASP.NET_SessionId`, so a stateless GET
lands in a session whose default filter resolves to nothing. `recdesk.py` posts
to `/Community/Program/FilterPrograms` the way the portal's own JavaScript
does, after priming the session with a GET. The `categories` are RecDesk
`ProgramType` ids — find them in the portal's own category links.

Adding a third parser is a new module exposing
`read_source(source, fetcher) -> list[CampSession]`, plus one line in
`READERS` in `cli.py`. That signature is the whole contract.

## Editing `dates.yaml`

`slug` is the stable handle that appears in the `breaks` column and **must not
change** once written. `name` is display text and can be retyped freely. `end`
is inclusive and means the last day students are out — the day before a
semester restarts, not the restart itself.

## Testing

106 tests, no network, no real clock. Two of them are positive controls and the
rest are boundary cases.

**The parse control.** `tests/fixtures/example_camps.html` is a saved listing
page carrying the four kinds of junk real sites ship: a listing with no start
date, a non-event type, a syntactically broken script block, and a `@graph`
wrapper. `example_camps.expected.json` says exactly what it must produce,
including the content-hash `key` of every session. Those keys are frozen on
purpose — if one changes, the fingerprinting logic changed, and the next run
would silently re-report the entire catalogue as new.

**The RecDesk control.** `tests/fixtures/recdesk_day_camp.html` is a saved
FilterPrograms fragment with frozen expectations for all five rows, including a
one-day camp with no stated ages. Separate tests pin the POST body shape, the
session priming, and the paging stop condition, because those are the parts
that fail silently rather than loudly.

**The merge control.** A fixed pair of runs on a pinned clock, asserting the
diff is exactly `1 new, 1 newly open, 1 disappeared` and that `first_seen` on
the unchanged record did not move.

Output is TAP:

```sh
pytest --tap-stream
prove --exec 'pytest --tap-stream' tests/     # if you would rather use prove
```

```
1..106
ok 1 tests/test_cli.py::test_update_then_list
ok 2 tests/test_cli.py::test_list_is_sorted_by_start_date
```

A source URL can be a local path, so the shipped config works on a fresh clone
with no network and no server:

```sh
campradar update && campradar list
```

That reads `tests/fixtures/example_camps.html` off disk. Saved pages are also
how you develop a parser without hammering someone's server, and how you re-run
a fix against the exact bytes that broke it:

```sh
curl -s https://their-site.example/camps > /tmp/page.html
# point a source at /tmp/page.html, then iterate
```

## Design notes

**Recall over precision.** A camp wrongly shown costs two seconds of
scrolling; one wrongly hidden can cost a week of childcare. Unstated age
ranges are permissive, and ambiguity resolves toward including the session.

**The diff is the product.** A full catalogue looks the same every week and
nobody reads it. `first_seen` is a first-class field, not a log line, and
`camps.json` is committed because it is what carries that history between
runs.

**"No camps" and "the markup moved" are different.** If a RecDesk fragment
holds programme links but no readable dates, that raises rather than returning
an empty list. A silent zero is how a broken source hides for a season.

**Fail loudly per source, quietly per row.** A malformed listing is skipped; a
dead site is reported. The run fails outright only when *every* source fails —
and in that case state is not written at all, because overwriting good state
with an empty scrape would mark every camp as disappeared and then re-report
them all as new.

**Nothing about a child is in this repository.** No kid profiles, no ages, no
preferences. Filtering by age is something you do to the TSV.

## Caveats

Scraped dates, prices and availability are often stale or wrong. **Confirm
with the provider before planning around anything here.** Break dates in
`dates.yaml` are transcribed by hand and should be checked against the
official district calendar and your own school's.

## License

MIT.
