# Camp Radar

Keeps track of what camps exist for DeKalb County school breaks — and, more
usefully, which ones are **new since last week**.

Built for a specific problem: metro Atlanta camps open registration between
September and February and sell out fast, and the hard part isn't choosing
between camps, it's finding out a camp exists before it's full.

## What it does

- Scrapes camp listings from provider sites, aggregators and registration
  platforms into one normalised schema.
- Tracks `first_seen` per session, so the dashboard can show **what appeared
  since last run** rather than an undifferentiated wall of camps.
- Maps sessions against the school break calendar and shows which break days
  still have nothing covering them.
- Publishes a static dashboard with an `.ics` export.

## Privacy

**No information about a child ever enters this repository or the published
site.** Kid profiles live in your browser's `localStorage` (web) or in a
gitignored `config/kids.local.yaml` (CLI). The dashboard is static, has no
backend, and makes exactly one network request — for the camp data itself.

This is why the repo can safely be public. Making it private would cost money
(Pages from private repos needs GitHub Pro), still wouldn't make the *site*
private, and wouldn't protect anything the current design doesn't already
protect. Full reasoning in [docs/privacy.md](docs/privacy.md).

## Quick start

```bash
git clone https://github.com/lskatz/camp-radar
cd camp-radar
pip install -e ".[dev]"
pytest -q                                  # 45 tests, ~0.3s

# The repo ships with sample data, so the dashboard renders immediately:
python3 -m http.server -d site 8000        # → http://localhost:8000
```

To pull real data, enable sources in `config/sources.yaml` and run:

```bash
campradar refresh --verbose
```

## Commands

| Command | What it does |
|---|---|
| `campradar refresh` | Fetch all enabled sources, update state, write site data |
| `campradar probe <url>` | Check whether a page exposes usable JSON-LD |
| `campradar export -o camps.ics` | Write an `.ics` from current state |

## Layout

```
config/
  sources.yaml        # providers and sources — usually the only file you edit
  breaks.yaml         # school break dates, typed by hand once a year
  kids.example.yaml   # template; copy to kids.local.yaml (gitignored)
src/campradar/
  models.py           # the CampSession contract every adapter must satisfy
  fetch.py            # cached, throttled HTTP
  delta.py            # first_seen tracking — the part that solves discovery
  pipeline.py         # sequencing and failure policy
  icsgen.py           # calendar output
  adapters/
    base.py           # adapter contract
    jsonld.py         # generic schema.org reader — covers many sites at once
site/                 # static dashboard, no build step
data/state.json       # committed; its git history is the archive
```

## Adding camps

Most sources need no code — a lot of sites expose schema.org markup they don't
know they have. Run `campradar probe <url>`; if it reports anything usable, you
just add a few lines to `sources.yaml`. See
[docs/adding-a-source.md](docs/adding-a-source.md).

## Design notes

Three decisions explain most of the code, and each is argued out in
[docs/architecture.md](docs/architecture.md):

**Recall over precision.** A camp wrongly shown costs two seconds of scrolling;
one wrongly hidden can cost a week of childcare. Unstated age ranges are
treated as permissive, and ambiguity resolves toward including the session.

**The diff is the product.** A full catalogue looks the same every week and
nobody reads it. `first_seen` is a first-class field, not a log line.

**Fail loudly per source, quietly per row.** A malformed listing is skipped; a
dead site is reported. The run only fails outright when everything fails —
and in that case state isn't written, because overwriting good state with an
empty scrape would mark every camp as disappeared and then re-report them all
as new.

## Caveats

Scraped dates, prices and availability are often stale or wrong. **Confirm with
the provider before planning around anything here.** Break dates in
`breaks.yaml` are transcribed by hand and should be checked against the
official calendar and your own school's.

## License

MIT.
