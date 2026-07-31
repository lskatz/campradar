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
git clone https://github.com/lskatz/campradar
cd campradar
pip install -e ".[dev]"
pytest -q                                  # 77 tests, ~1s

# The repo ships with sample data, so the dashboard renders immediately:
python3 -m http.server -d site 8000        # → http://localhost:8000
```

No install needed if you'd rather not — `python -m` works straight from the
source tree, which is handy inside a pixi or conda shell:

```bash
PYTHONPATH=src python -m campradar refresh --verbose
```

To pull real data, enable sources in `config/sources.yaml` and run:

```bash
campradar refresh --verbose
```

## Workflow: refresh locally, then push

Data collection happens on your machine. CI only publishes what you pushed, so
what's live is always something you reviewed — and Actions needs no network
access to providers, no schedule, and no write access to the repo.

```bash
make probe      # which configured sources actually expose usable data
make update     # test, refresh, show what changed, commit, push
```

`make update` runs the tests *before* fetching, so a broken parser can't
overwrite good data. **It never runs git** — it reports what changed and prints
the commands to run:

```
==> Files changed
   M site/assets/data/sessions.json
  ?? data/state.json

==> Ready to publish
    git add data site
    git commit -m "refresh 2026-07-29: 3 new"
    git push
```

Staging, committing and pushing stay entirely yours.

`data/state.json` is committed — it's what carries `first_seen` between runs.
`data/refresh.log` is not; it's regenerated every time.

### Checking sources

`make probe` (or `campradar probe`) walks every source in `sources.yaml`,
including disabled ones, and reports which are worth turning on:

```
[off] dunwoody-nature-center-camps
        12 usable event(s)  https://dunwoodynature.org/camps/
           - Pond Explorers
           - Wilderness Skills
[off] callanwolde-camps
       no usable JSON-LD  https://callanwolde.org/camps/
           needs a bespoke adapter — docs/adding-a-source.md

1 disabled source(s) look usable: dunwoody-nature-center-camps
```

Pass a single URL to probe just that page: `campradar probe https://...`.

### If you'd rather CI did the scraping

There is deliberately no scheduled scraping workflow in this repo. `deploy.yml`
is the only workflow: it runs the tests, checks the site has data, and publishes
`site/`. It has no schedule, no provider network access, and `contents: read` —
it cannot write to the repository even by accident.

That is the property worth keeping. Because collection happens on your machine,
every published change passed through a diff you looked at, and a provider
changing their HTML breaks a command you ran on purpose rather than a cron job
you find out about three weeks later.

## Reading the data directly

The published file is public and CORS-open:

```bash
URL=https://lskatz.github.io/campradar/assets/data/sessions.json

# how many camps, and how the last run went
curl -s $URL | python3 -c "import json,sys; d=json.load(sys.stdin); print(len(d['sessions']),'sessions'); print(d['run'])"

# just what's new
curl -s $URL | python3 -c "import json,sys; [print(s['start_date'], s['title']) for s in json.load(sys.stdin)['sessions'] if s['is_new']]"
```

The same snippets appear on the site itself, with the URL filled in.

## Commands

| Command | What it does |
|---|---|
| `make update` | Test + refresh, then print the git commands to run |
| `make probe` | Check every configured source for usable data |
| `make serve` | Preview the dashboard locally |
| `make doctor` | Diagnose which copy of the code is running |
| `campradar refresh` | Fetch all enabled sources, update state, write site data |
| `campradar probe [url]` | Probe one page, or every configured source |
| `campradar active-doctor` | Find out whether your ACTIVE key can read the API at all |
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
scripts/update.sh     # the local refresh-and-publish loop
data/state.json       # committed; carries first_seen between runs
```

## If `campradar` and `make` disagree

The `make` targets run the code in this checkout via `PYTHONPATH=src`, on
purpose. A non-editable `pip install .` copies the code into site-packages,
where it shadows the source tree and makes your edits appear to do nothing —
`campradar probe` failing with "the following arguments are required: url"
while `make probe` works is the classic symptom.

```bash
make doctor                        # shows which copy each path resolves to
pip install -e . --force-reinstall # fix a stale install
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
