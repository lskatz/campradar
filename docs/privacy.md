# Privacy

This project involves children, so the privacy design is a constraint rather
than a feature. The whole approach comes down to one decision:

> **No information about a child ever enters the repository or the built site.**

Everything else follows from that.

## Why "just make the repo private" is the wrong answer

It's the obvious first instinct, and it doesn't work:

1. **A private repo does not produce a private site.** GitHub Pages serves
   publicly regardless of repository visibility. Site-level access control
   exists only on GitHub Enterprise Cloud. Anything that reaches `site/` is
   world-readable the moment it deploys.
2. **Pages from a private repo requires a paid plan.** On the free tier,
   Pages only publishes from public repositories.
3. **Private repos rot into public ones.** Repos get shared with collaborators,
   forked, made public years later when the owner is tidying up. A design that
   depends on the repo staying private depends on a decision nobody will
   remember making.

So repository visibility is the wrong control. The right control is never
putting the data in scope.

## Where child data actually lives

| Data | Location | Leaves the device? |
|---|---|---|
| Camp listings, prices, dates | `site/assets/data/sessions.json`, committed | Yes — all public information |
| School break dates | `config/breaks.yaml`, committed | Yes — public information |
| Kid names and ages (web) | Browser `localStorage` | **No** |
| Kid names and ages (CLI) | `config/kids.local.yaml`, gitignored | **No** |

The dashboard is a static page with no backend. There is no endpoint to POST
to, no database, no analytics, and no third-party scripts other than a Google
Fonts stylesheet. When you type a child's name into the filter box, that string
is written to `localStorage` and read back by the same page. It has nowhere
else to go.

This is also what makes sharing safe: your friends visit the same public URL
and enter their own kids, and their data stays on their own devices. You never
hold anyone else's family information, which means you never have to secure it.

## Invariants

These are the rules that keep the guarantee true. Breaking any one of them
breaks the model.

1. **`config/kids.local.yaml` stays gitignored.** It's the first entry in
   `.gitignore`, above everything else, so it's visible in any audit.
2. **The build never reads `kids.local.yaml`.** `pipeline.py` does not import
   it and does not know it exists. Only CLI commands you run locally do.
3. **No module under `src/campradar/` has a field for a child's name or age.**
   Grep for it — `models.py` has no such field, by construction.
4. **`app.js` makes exactly one network request**, to `sessions.json`. Any pull
   request adding a second `fetch()` needs a very good explanation.
5. **No analytics, ever.** Not Plausible, not GA, not a self-hosted counter.
   Traffic to a page about children is itself sensitive.

## If you want the site genuinely private

If you'd rather not publish at all, drop the `deploy` job from the workflow and
run the dashboard locally:

```bash
campradar refresh
python3 -m http.server -d site 8000
```

You lose easy sharing with friends, and you gain nothing privacy-wise over the
default design — because under the default design there is nothing about your
family on the public site to begin with.

## What is still public

The repository does disclose some things about you, and you should decide
whether you mind:

- **Which camps you're tracking.** `sources.yaml` is a reasonable proxy for
  your interests and neighbourhood.
- **Roughly where you live.** A source list heavy on Decatur and Druid Hills
  providers narrows things down.
- **Your children's approximate ages**, inferable from the age ranges of the
  camps you bothered to configure.

None of that is identifying on its own, but it's not nothing. If it bothers
you, keep `sources.yaml` broad — covering all of DeKalb rather than just your
corner of it — which also happens to make the tool more useful to friends.

## Respecting the sites we read

A note on the other people involved. Providers are mostly small nonprofits and
county departments:

- `fetch.py` sends an identifying User-Agent with a contact URL.
- Requests to a host are spaced by at least 1.5 seconds.
- Conditional requests mean unchanged pages transfer nothing.
- Check `robots.txt` before enabling a source.
- Republish only what a parent needs to make a decision — dates, ages, price,
  and a link. Don't mirror full descriptions or images; send people to the
  provider.
