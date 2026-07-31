# Sources to add back

Every source that has been tried and does not yet produce sessions, with what
was actually observed and what would have to change. `config/sources.yaml`
carries only sources that work; this file carries the knowledge of the rest.

The point of writing the evidence down is that "returned nothing" is not one
condition. A page with no markup, a page behind a JavaScript shell, and a
gateway refusing a valid key all look identical in a refresh summary and need
completely different work. Six months from now the only way to tell them apart
without re-deriving everything is to have written down which one it was.

**Before re-enabling anything here, re-run the probe.** These notes describe
sites as they were on 2026-07-31. Providers redesign; a note is a starting
point, not a current fact.

---

## The three ACTIVE Network products are not the same system

This caused real confusion and is worth stating plainly, because all three say
"active" and none of them reach the others:

| System | Host | Who uses it here |
|---|---|---|
| ACTIVE.com Activity Search API v2 | `api.amp.active.com` | what `$ACTIVE_API_KEY` is for |
| ActiveNet | `anc.apm.activecommunities.com` | DeKalb County Recreation |
| Camp & Class Manager | `campscui.active.com` | Callanwolde |

A working Search API key says nothing about whether the other two are
reachable. They are separate products that happen to share a brand.

---

## Blocked upstream — not fixable here

### `callanwolde-active`, `dekalb-county-active` — ACTIVE Search API

**Observed.** HTTP 403 over both https and http, with:

```
X-Mashery-Error-Code: ERR_403_DEVELOPER_INACTIVE
X-Error-Detail-Header: Account Inactive
```

**Confirmed not a config problem.** `campradar active-doctor` runs ACTIVE's own
published sample query beside ours; the documented sample fails identically
with the same key. The developer account is approved and active, and the key
transmits fine. ACTIVE's support forum carries unresolved reports of this exact
code going back nine years, including from accounts ACTIVE staff confirmed as
valid.

**Next step.** A support ticket quoting the Mashery code — nothing in this repo.
Re-run `campradar active-doctor` before spending any more time on it; if the
documented sample starts passing, the account issue cleared.

**Worth noting even if it clears:** it was never established that ActiveNet
municipal instances are indexed in this API at all, so DeKalb may still return
zero. Callanwolde does not need it either way — see below.

---

## Tried and answered — the data is not there

### `callanwolde-tribe` — The Events Calendar REST API

**Observed.** The API is live and healthy (`X-Tec-Api-Root` is advertised;
`tribe-discover` returned 41 upcoming events across 24 categories). But there
is no camp category. The categories are `art-classes`, `pottery-ceramics`,
`dance-events`, `drawing-painting`, `jewelry-making-metalsmithing`,
`wellness-arts` and similar, and the events are adult classes.

**Conclusion.** The calendar is Callanwolde's class catalogue, not its camps.
Do not enable this source. `childrens-dance` (15 events) is the only
child-facing category and holds term-time dance classes, not break camps.

---

## Needs a bespoke adapter

### `callanwolde-camps` → actually `campscui.active.com`

**Observed.** `callanwolde.org/classes/camps/` fetches 200 with no JSON-LD, but
the HTML is rich: each camp has a title, date range, registration code, age
band, and a `SOLD OUT` marker. Every registration link goes to
`https://campscui.active.com/orgs/CallanwoldeFineArtsCenter` with `season` and
`session` IDs.

**Decide before building.** Everything on that page is summer camp — the page
is titled "Spend the Summer at Callanwolde" and all sessions run June–July.
Camp Radar tracks *school break* weeks. Confirm Callanwolde runs fall,
Thanksgiving, winter or spring camps at all before writing an adapter; if they
are summer-only they do not belong in this tool, and that is a finding rather
than a gap.

### `tucker-rec` — RecDesk

**Observed.** `tucker.recdesk.com/Community/Program?category=3` returns 200 with
a 163 KB server-rendered body and no JSON-LD.

**Why it is a good next target.** No SPA, no gateway, no key — the listings are
in the HTML that arrives. RecDesk is also a common platform, so an adapter here
likely generalises to other municipalities.

### `dunwoody-nature-center-camps`

**Observed.** `dunwoodynature.org/education/camp-programs/` returns 200, prose
only, no markup. `/camps/` is a 404. Summer lives at
`/education/summer-camp/`.

**Note.** WordPress with `wp-json` advertised but no Events Calendar headers.
Check whether camp programmes are a custom post type exposed via
`/wp-json/wp/v2/` before writing an HTML parser.

### `zoo-atlanta-camps`

**Observed.** All three break-camp pages (`/program/fall-camp/`,
`/program/winter-camp/`, `/program/spring-break-camp/`) return 200 with prose
and no JSON-LD. Note the path is `/program/`, singular — `/programs/camps/`
is a 404.

**Note.** WordPress on WP Engine, `wp-json` advertised, no Events Calendar.
Same question as Dunwoody: check the REST API before parsing HTML.

---

## Never enabled

`fernbank-science-center`, `decatur-active-living` and
`atlanta-botanical-garden` have provider entries but no source. Run
`campradar probe <url>` against each before adding one.
