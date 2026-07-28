# Adding a source

Most sources need **no code at all**. Work through this in order and stop as
soon as something works.

## 1. Check robots.txt

```bash
curl -s https://provider.example/robots.txt
```

If the camp listing path is disallowed, stop. These are mostly small nonprofits
and county departments, and the tool is only sustainable if it stays welcome.

## 2. Probe for JSON-LD

```bash
campradar probe https://provider.example/camps
```

A surprising number of sites emit schema.org `Event` or `Course` markup for SEO
without anyone at the organisation knowing. Squarespace, Wix, WordPress event
plugins, Sawyer and ACTIVE all do it by default. Output looks like:

```
Found 12 JSON-LD object(s):
  [      usable] Event: Pond Explorers
  [      usable] Event: Wilderness Skills
  [ no startDate] Organization: Dunwoody Nature Center
```

Any line marked `usable` means you're done coding.

## 3. Add it to sources.yaml

Add the provider once:

```yaml
providers:
  - slug: dunwoody-nature-center
    name: Dunwoody Nature Center
    homepage: https://dunwoodynature.org/
    locality: Dunwoody
```

Then the source:

```yaml
sources:
  - id: dunwoody-nature-center-camps
    provider_slug: dunwoody-nature-center
    adapter: jsonld
    enabled: true
    urls:
      - https://dunwoodynature.org/camps/
```

**Ordering matters.** When the same session is reachable through two sources,
the one listed first wins. Put provider sites above aggregators — providers are
authoritative about their own dates and prices.

Verify:

```bash
campradar refresh --verbose
```

## 4. Only if probe came back empty: write an adapter

```python
# src/campradar/adapters/example_provider.py
"""Adapter for Example Provider's camp listing.

Their listing table has no JSON-LD, so we parse the DOM. Written against the
layout as of 2026-07; if it stops returning sessions, the layout changed.
"""

from collections.abc import Iterator

from bs4 import BeautifulSoup

from ..fetch import Fetcher
from ..models import CampSession
from .base import Adapter


class ExampleProviderAdapter(Adapter):
    name = "example-provider"

    def parse(self, fetcher: Fetcher) -> Iterator[CampSession]:
        for url in self.config["urls"]:
            soup = BeautifulSoup(fetcher.get(url).text, "html.parser")
            for row in soup.select(".camp-listing"):
                # Raising here is fine and expected. Adapter.run catches
                # per-row errors, logs them, and moves on — one malformed
                # listing must not cost you the other forty.
                yield CampSession(
                    provider_slug=self.provider_slug,
                    title=row.select_one(".title").get_text(strip=True),
                    start_date=...,
                    end_date=...,
                    source_id=self.source_id,
                )
```

Register it:

```python
# src/campradar/adapters/__init__.py
REGISTRY = {
    "jsonld": JsonLdAdapter,
    "example-provider": ExampleProviderAdapter,
}
```

### Rules for adapters

- **Yield, don't return a list.** A partially-parsed page should still
  contribute what it managed to read.
- **Let bad rows raise.** `ValueError`, `KeyError` and `TypeError` are caught
  per-row by `Adapter.run`. Don't write your own try/except around each field.
- **Never set `first_seen`.** That belongs to the delta layer. Adapters
  describe the world as it is now; they don't remember.
- **Prefer `None` to a guess.** An unstated age range is permissive and shows
  the camp to everyone. A fabricated one hides it from the family it suited.
- **Convert grades to ages.** Eligibility filtering needs one unit;
  `parse_age_text` handles the common phrasings.

### Add a fixture test

Save a trimmed copy of the real HTML under `tests/fixtures/` and assert on a
couple of parsed sessions. When the site is redesigned — and it will be — the
test tells you what changed. See `tests/test_adapters.py` for the pattern,
including a `FakeFetcher` that keeps tests off the network.

## Sources worth adding

Aggregators first — they exist precisely to solve discovery and one adapter
covers a long tail of providers:

- Atlanta Parent's annual camp guide
- Macaroni Kid (Decatur / Druid Hills editions)
- ActivityHero, Sawyer

Registration platforms next, since one adapter covers every provider on them:

- CampBrain, CampMinder, Jackrabbit, Ultracamp, ACTIVE Network

Then individual providers, starting with whatever is closest to home.
