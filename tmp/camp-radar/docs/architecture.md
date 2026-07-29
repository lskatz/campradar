# Architecture

## The problem this solves

Not "which camp should I pick" — **"what camps exist that I don't know about."**

That framing drives most of the design. Discovery is a recall problem: a camp
wrongly shown costs a parent two seconds of scrolling, while one never shown
can cost a week of childcare. So the pipeline over-collects and filters late,
and every ambiguous case resolves toward including the session.

The second consequence is that a static catalogue is nearly useless. It's too
long to read and looks identical week to week. The useful output is the
**diff** — what appeared since last time — which is why `delta.py` exists and
why `first_seen` is a first-class field rather than a log line.

## Flow

```
config/sources.yaml
        │
        ▼
   adapters/*  ──uses──▶  fetch.py  (cached, throttled HTTP)
        │                     │
        │                     └──▶ data/raw/   (snapshots, gitignored)
        ▼
  list[CampSession]           models.py defines the contract
        │
        ▼
     delta.py  ◀── data/state.json  (prior run, committed)
        │
        ├──▶ data/state.json                 (new state, committed)
        └──▶ site/assets/data/sessions.json  (what the browser reads)
                     │
                     ▼
               static dashboard  +  client-side .ics export
```

## Layer responsibilities

**`models.py`** — the contract. Every adapter emits `CampSession` regardless of
how ugly its source is, which stops one bad site from leaking its mess
downstream. The important subtlety is `CampSession.key`: identity is a hash of
`(provider, title fingerprint, start date)`, deliberately excluding price,
status and description. Those are the fields most likely to change on a session
that is otherwise the same one, and including them would make every price tweak
look like a new camp and flood the "new this week" panel with noise.

**`fetch.py`** — polite, cached HTTP. Conditional requests via ETag and
Last-Modified mean a weekly run over a hundred pages transfers almost nothing.
The cache doubles as raw snapshots, so a parser bug can be fixed and re-run
without touching the network — the same discipline as keeping raw reads rather
than only the assembly.

**`adapters/`** — one module per *kind* of site. The leverage here is real:
most providers run on a handful of platforms, and a great many emit schema.org
JSON-LD for SEO without anyone at the organisation knowing. `jsonld.py` reads
that markup and covers dozens of providers with no per-site code. Always run
`campradar probe <url>` before writing anything bespoke.

**`delta.py`** — pure functions, no I/O, injected clock. Given prior state and
fresh scrapes, returns new state plus a report. Being pure is what makes the
interesting cases (a camp opening for registration, a source going quiet)
testable without a network or a fixture server.

**`pipeline.py`** — sequencing and failure policy.

**`site/`** — a static page reading one JSON file. No framework, no build step
for the JavaScript.

## Failure policy

Two levels, deliberately different:

- **Row level: soft.** One listing with an unparseable date is skipped and
  logged. `Adapter.run` catches per-row so a partially-broken page still
  contributes what it could.
- **Source level: loud.** A site that 404s or gets redesigned raises
  `AdapterError`, is recorded in the run report, and shows up in the Actions
  log — but does not stop other sources.

A run fails outright only when *every* source failed, which indicates something
systemic (bad deploy, no network) rather than one provider changing their
layout. In that case state is **not** written: overwriting good state with an
empty scrape would mark every session as disappeared, then re-report them all
as new on recovery. A false alert storm is worse than a stale dashboard.

Sessions that vanish are retained rather than deleted, for the same reason — a
delisted camp usually means "sold out", and deleting it would make it reappear
as new if the provider restores the page.

## Decisions worth knowing about

**Break dates are typed by hand.** The district publishes its calendar as a
flat image with no PDF, no alt text and no feed. OCR would be fragile in
exchange for saving five minutes of typing a year. Automating it is the wrong
place to spend effort — put that effort into camp ingestion instead.

**Geocoding is not part of ingestion.** Providers move rarely; sessions change
weekly. Resolving coordinates once per provider and caching in `sources.yaml`
avoids hammering a geocoder on every run.

**No database, and no repo writes from CI.** The obvious way to persist
`first_seen` between runs is to commit `state.json` back from the workflow, but
that means giving a scheduled job write access to the repository — a standing
risk for something whose whole job is parsing untrusted HTML from the open web.

Instead, `first_seen` is published *inside* `sessions.json`, and each run
hydrates prior state from the live site (`--previous-url`). The deployment is
the state store. CI runs with `contents: read`, and the design is self-healing:
whatever is currently live is the source of truth, so a lost cache or a
re-created repo converges on the next run rather than needing a reset.

`data/state.json` still exists for local runs, where committing nothing is the
default anyway.

**No server.** Static hosting is free, has nothing to maintain, and — most
importantly — gives visitor data nowhere to go. See `privacy.md`.

**Two ICS implementations.** `icsgen.py` for the CLI and a mirror in `app.js`
for the browser. Duplication is deliberate: adding a build step for the
JavaScript to share one implementation would cost more than keeping forty lines
in sync. Both follow the same documented rules — exclusive `DTEND`, escaped
text, CRLF endings.
