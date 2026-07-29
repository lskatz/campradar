"""Command-line entry point.

Uses argparse rather than a CLI framework to keep the dependency list short —
this runs unattended in CI, where every extra package is another thing that can
break a scheduled job nobody is watching.

Commands:
    refresh   Fetch all sources, update state, write site data.
    probe     Survey every configured source — or one URL — for usable JSON-LD.
    active-discover  Ask the ACTIVE API what it actually has near a place.
    tribe-discover   Ask an Events Calendar site what is actually in it.
    export    Write an .ics file from current state.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .adapters.activesearch import api_base, build_search_url
from .adapters.jsonld import extract_jsonld_objects, is_event
from .adapters.tribe import CATEGORIES_PATH as TRIBE_CATEGORIES_PATH
from .adapters.tribe import build_events_url, strip_html
from .delta import load_state
from .fetch import Fetcher
from .icsgen import render_calendar
from .pipeline import load_sources, run_pipeline
from .redact import install_redaction, redact

DEFAULT_CONFIG = Path("config")
DEFAULT_DATA = Path("data")
DEFAULT_SITE_DATA = Path("site/assets/data")


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)-7s %(name)s: %(message)s",
    )
    # httpx logs full request URLs at INFO, and the Active API carries its key
    # in the query string. Install this before any request can be made.
    install_redaction()


def cmd_refresh(args: argparse.Namespace) -> int:
    """Run the pipeline and report what changed."""
    result = run_pipeline(
        config_dir=args.config,
        data_dir=args.data,
        site_data_dir=args.site_data,
        previous_url=args.previous_url,
    )

    productive = result.productive_sources
    print(
        f"Sources: {len(productive)} produced camps, "
        f"{len(result.empty_sources)} returned nothing, "
        f"{len(result.failed_sources)} failed"
    )
    if result.failed_sources:
        print(f"  failed:  {', '.join(sorted(result.failed_sources))}")
    if result.empty_sources:
        # Called out explicitly because it is the failure mode that hides: the
        # fetch worked, the parse worked, and there is simply nothing there.
        print(f"  nothing: {', '.join(sorted(result.empty_sources))}")
    print(f"Sessions found: {result.sessions_found}")
    if result.delta:
        print(f"Changes: {result.delta.summary()}")
        for record in result.delta.new[:20]:
            session = record.session
            print(f"  NEW  {session.start_date}  {session.title}")

    if result.is_total_failure:
        print("Every source failed — not writing state.", file=sys.stderr)
        return 1
    return 0


@dataclass(slots=True)
class ProbeReport:
    """What one probed URL turned out to be."""

    source_id: str
    url: str
    enabled: bool
    reachable: bool = False
    events: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def usable(self) -> bool:
        """True when the generic `jsonld` adapter would get sessions out of this."""
        return bool(self.events)


def _probe_targets(config_dir: Path) -> list[tuple[str, str, bool]]:
    """Every `(source_id, url, enabled)` triple in sources.yaml.

    Disabled sources are included on purpose. Retired and placeholder entries
    are exactly the ones worth re-checking — a site mid-redesign comes back,
    and a placeholder URL nobody has confirmed is the whole reason to probe.
    Filtering them out here would hide the answer the command exists to give.
    """
    source_configs, _providers = load_sources(config_dir / "sources.yaml")
    return [
        (source["id"], url, bool(source.get("enabled", True)))
        for source in source_configs
        for url in source.get("urls", [])
    ]


def _probe_one(fetcher: Fetcher, source_id: str, url: str, enabled: bool) -> ProbeReport:
    """Fetch one URL and record what it exposes.

    Network and HTTP errors are captured rather than raised: a survey should
    cover every source, and one provider being down says nothing about the
    rest. This mirrors the pipeline's per-source failure policy.
    """
    report = ProbeReport(source_id=source_id, url=url, enabled=enabled)
    try:
        result = fetcher.get(url)
    except Exception as exc:  # noqa: BLE001 - one dead site must not end the survey
        report.error = redact(exc).splitlines()[0]
        return report

    report.reachable = True
    report.events = [
        str(obj.get("name", "(untitled)"))[:60]
        for obj in extract_jsonld_objects(result.text)
        if is_event(obj) and obj.get("startDate")
    ]
    return report


def _render(report: ProbeReport, *, show_marker: bool) -> None:
    """Print one report in the format documented in the README."""
    shown = redact(report.url)
    if show_marker:
        marker = "[on]" if report.enabled else "[off]"
        print(f"{marker:<5} {report.source_id}")
        # In a sweep the id is the heading, so each status line carries the URL.
        where = f"  {shown}"
    else:
        # Probing one page: the URL is the heading and would only repeat below.
        print(shown)
        where = ""

    if not report.reachable:
        print(f"        unreachable{where}")
        print(f"          {report.error}")
        return

    if not report.events:
        print(f"        no usable JSON-LD{where}")
        print("          needs a bespoke adapter — docs/adding-a-source.md")
        return

    print(f"        {len(report.events)} usable event(s){where}")
    for name in report.events[:3]:
        print(f"          - {name}")
    if len(report.events) > 3:
        print(f"          … and {len(report.events) - 3} more")


def cmd_probe(args: argparse.Namespace) -> int:
    """Survey sources for JSON-LD the generic adapter can use.

    With no URL this walks every source in sources.yaml, enabled or not, and
    ends by naming the disabled ones worth turning on. With a URL it inspects
    that page alone, which is the form `docs/adding-a-source.md` uses.

    Exit status distinguishes "this source has no JSON-LD" from "the probe
    could not do its job". The former is a finding, not a failure, so it exits
    0 — otherwise `make probe && make update` would be blocked by any provider
    that happens to lack markup. Only an empty config or a total inability to
    reach anything (no network, every host refusing) exits non-zero. The
    single-URL form is stricter: it answers one yes/no question, so an
    unusable page exits 1.
    """
    single = args.url is not None
    if single:
        targets = [(args.url, args.url, True)]
    else:
        targets = _probe_targets(args.config)
        if not targets:
            print(f"No sources defined in {args.config / 'sources.yaml'}.", file=sys.stderr)
            return 1

    with Fetcher(args.data / "raw") as fetcher:
        reports = [_probe_one(fetcher, sid, url, on) for sid, url, on in targets]

    for report in reports:
        _render(report, show_marker=not single)

    if single:
        return 0 if reports[0].usable else 1

    promotable = [r.source_id for r in reports if r.usable and not r.enabled]
    dead = [r.source_id for r in reports if r.enabled and not r.usable]
    print()
    if promotable:
        print(f"{len(promotable)} disabled source(s) look usable: {', '.join(promotable)}")
    if dead:
        print(f"{len(dead)} enabled source(s) yield nothing: {', '.join(dead)}")
    if not promotable and not dead:
        print("Every enabled source looks usable; nothing disabled is worth turning on.")

    if not any(r.reachable for r in reports):
        print("Nothing was reachable — check your network before trusting this.", file=sys.stderr)
        return 1
    return 0


def cmd_active_discover(args: argparse.Namespace) -> int:
    """Report what the ACTIVE Activity Search API actually holds near a place.

    This exists because coverage is an open question, not a known quantity.
    Active Network sells several products; this search index is fed from
    ACTIVE.com assets, and it is unconfirmed whether ActiveNet municipal
    instances (DeKalb County's registration system, for one) are indexed in it
    at all. Guessing would mean building a source config against data that may
    not exist.

    So rather than assume, this asks three questions in three calls:

      1. How many kids' activities are there near here at all?
      2. Which organisations do they belong to?  (-> org_id for a source)
      3. Which source systems fed them?          (-> is ActiveNet in here?)

    Facets do the work: `per_page=0` returns counts without asset bodies, which
    is one cheap call per question against a 2-per-second budget.
    """
    api_key = os.environ.get(args.api_key_env, "").strip()
    if not api_key:
        print(
            f"${args.api_key_env} is not set. Export it first:\n"
            f"  export {args.api_key_env}=...\n"
            f"Do not put it in config/sources.yaml — that file is committed.",
            file=sys.stderr,
        )
        return 1

    base: dict[str, object] = {"near": args.near, "radius": args.radius}
    if args.kids:
        base["kids"] = "true"
    if args.start_date:
        base["start_date"] = args.start_date

    if args.org_query:
        return _show_org_ids(args, api_key, base)

    questions = [
        ("total kids activities", {**base, "per_page": 0}, None),
        ("by organisation", {**base, "per_page": 0, "facets": "organizationName"}, "organizationName"),  # noqa: E501
        ("by source system", {**base, "per_page": 0, "facets": "sourceSystemName"}, "sourceSystemName"),  # noqa: E501
        ("by category", {**base, "per_page": 0, "facets": "categoryName"}, "categoryName"),
    ]

    base = args.api_base or api_base()
    print(f"ACTIVE search near {args.near} within {args.radius} miles")
    if base != "https://api.amp.active.com/v2/search":
        print(f"endpoint: {base}")
    if args.start_date:
        print(f"start_date={args.start_date}")
    print()

    with Fetcher(args.data / "raw", delay_seconds=max(0.5, args.delay)) as fetcher:
        for label, params, facet in questions:
            url = build_search_url(params, api_key, base)
            try:
                payload = json.loads(fetcher.get(url).text)
            except Exception as exc:  # noqa: BLE001 - report and continue
                print(f"{label}: failed — {redact(exc)}")
                continue

            total = payload.get("total_results")
            if facet is None:
                print(f"{label}: {total}")
                if not total:
                    print(
                        "\n  Zero results. Either nothing is indexed near here, or "
                        "\n  this account's key does not cover these assets. Widen "
                        "\n  --radius and drop --start-date before concluding anything."
                    )
                continue

            print(f"{label} (of {total}):")
            for name, count in _facet_counts(payload, facet)[: args.top]:
                print(f"  {count:>6}  {name}")
            print()

    print(
        "Next: pick an organisation above, find its organizationGuid with\n"
        "  campradar active-discover --org-query 'Callanwolde'\n"
        "then use it as params.org_id in a source of adapter: activesearch."
    )
    return 0


def _show_org_ids(args: argparse.Namespace, api_key: str, base: dict) -> int:
    """List `organizationGuid` values matching a name, for use as params.org_id.

    Targeting a source by organisation rather than by keyword is what makes an
    `activesearch` source stable: a name search drifts as ACTIVE re-titles
    assets, whereas the GUID is the provider itself.
    """
    params = {**base, "query": args.org_query, "per_page": 50}
    url = build_search_url(params, api_key, args.api_base or api_base())
    with Fetcher(args.data / "raw", delay_seconds=max(0.5, args.delay)) as fetcher:
        try:
            payload = json.loads(fetcher.get(url).text)
        except Exception as exc:  # noqa: BLE001
            print(f"lookup failed — {redact(exc)}", file=sys.stderr)
            return 1

    found: dict[str, tuple[str, int]] = {}
    for asset in payload.get("results") or []:
        if not isinstance(asset, dict):
            continue
        org = asset.get("organization")
        if not isinstance(org, dict):
            continue
        guid = str(org.get("organizationGuid") or "").strip()
        name = str(org.get("organizationName") or "").strip()
        if not guid or name in ("", "N/A"):
            continue
        prior = found.get(guid, (name, 0))
        found[guid] = (name, prior[1] + 1)

    if not found:
        print(f"No organisations found for {args.org_query!r} near {args.near}.")
        print("Try --no-kids, a wider --radius, or a shorter query.")
        return 1

    print(f"Organisations matching {args.org_query!r} (assets seen on page 1):")
    for guid, (name, count) in sorted(found.items(), key=lambda kv: -kv[1][1]):
        print(f"  {count:>4}  {name}")
        print(f"        org_id: {guid}")
    return 0


def _facet_counts(payload: dict, field: str) -> list[tuple[str, int]]:
    """Pull `(value, count)` pairs out of a facet response.

    Active has shipped more than one facet envelope over the years, so this
    tolerates a couple of shapes rather than indexing blindly into one. An
    unrecognised shape yields nothing, which prints as an empty section — a
    visible non-answer, not a crash.
    """
    facets = payload.get("facets")
    buckets: list[tuple[str, int]] = []

    def absorb(node: object) -> None:
        if isinstance(node, dict):
            name = node.get("name") or node.get("value") or node.get("term")
            count = node.get("count") or node.get("total")
            if isinstance(name, str) and isinstance(count, int):
                buckets.append((name, count))
                return
            for value in node.values():
                absorb(value)
        elif isinstance(node, list):
            for item in node:
                absorb(item)

    absorb(facets)
    if not buckets:
        absorb(payload.get("facet_values"))
    return sorted(buckets, key=lambda pair: -pair[1])


def cmd_tribe_discover(args: argparse.Namespace) -> int:
    """Report what is actually in a site's Events Calendar.

    Written because `callanwolde-tribe` came back empty and there was no way to
    tell which of three very different things had happened: the date window
    excluded everything, the `search` term matched nothing, or the provider
    keeps camps somewhere other than the events calendar entirely. Those need
    opposite fixes, so guessing is worse than measuring.

    Four questions, cheapest first:

      1. Does the calendar have anything at all, unfiltered?
      2. What categories exist, and how many events in each?
      3. What do the soonest events actually look like?
      4. If --search was given, how many match it?

    A calendar full of concerts and no camps is a real and useful answer: it
    means this adapter is the wrong tool for that provider, and no amount of
    parameter tuning will change it.
    """
    base = args.base_url.rstrip("/")
    print(f"Events Calendar at {base}")

    with Fetcher(args.data / "raw", delay_seconds=max(0.5, args.delay)) as fetcher:

        def get(url: str) -> dict | None:
            try:
                return json.loads(fetcher.get(url).text)
            except Exception as exc:  # noqa: BLE001 - report and carry on
                print(f"  failed: {redact(exc)}")
                return None

        window: dict[str, object] = {}
        if args.start_date:
            window["start_date"] = args.start_date
        if args.end_date:
            window["end_date"] = args.end_date

        print("\n-- everything in the window --")
        payload = get(build_events_url(base, {**window, "per_page": 1}))
        if payload is None:
            print("\nNo API here. Check /wp-json/ lists 'tribe/events/v1'.")
            return 1
        total = payload.get("total")
        print(f"  {total} event(s)")
        if not total:
            print(
                "  Nothing in this window. Widen or drop --start-date/--end-date\n"
                "  before concluding the calendar is empty."
            )

        print("\n-- categories --")
        cats = get(f"{base}{TRIBE_CATEGORIES_PATH}?per_page=100")
        rows = (cats or {}).get("categories") or []
        if not rows:
            print("  none reported (the calendar may not use categories)")
        for row in sorted(rows, key=lambda r: -int(r.get("count") or 0))[: args.top]:
            print(f"  {int(row.get('count') or 0):>5}  {row.get('slug')}  ({row.get('name')})")

        print("\n-- soonest events --")
        payload = get(build_events_url(base, {**window, "per_page": args.top}))
        for event in (payload or {}).get("events") or []:
            if not isinstance(event, dict):
                continue
            slugs = ",".join(
                str(c.get("slug")) for c in event.get("categories") or [] if isinstance(c, dict)
            )
            title = (strip_html(event.get("title")) or "?")[:52]
            print(f"  {str(event.get('start_date'))[:10]}  {title:<53} [{slugs}]")

        if args.search:
            print(f"\n-- matching search={args.search!r} --")
            payload = get(build_events_url(base, {**window, "search": args.search, "per_page": 1}))
            matched = (payload or {}).get("total")
            print(f"  {matched} event(s)")
            if not matched:
                print(
                    "  Zero. Either the wording differs, or this provider does not\n"
                    "  put camps in its events calendar at all — compare against the\n"
                    "  category list above before adding a source."
                )

    print(
        "\nNext: pick a slug from the category list and set it as\n"
        "  category_slugs: [<slug>]\n"
        "on a source of adapter: tribe, and drop `search`."
    )
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    """Write an .ics of everything currently in state."""
    state = load_state(args.data / "state.json")
    if not state:
        print("No state yet — run `campradar refresh` first.", file=sys.stderr)
        return 1

    sessions = [
        record.session
        for record in state.values()
        if args.include_past or record.session.end_date >= date.today()
    ]
    args.output.write_text(render_calendar(sessions), encoding="utf-8")
    print(f"Wrote {len(sessions)} events to {args.output}")
    return 0


def _add_global_flags(parser: argparse.ArgumentParser, *, with_defaults: bool) -> None:
    """Attach `--verbose`, `--config` and `--data` to a parser.

    Called once for the top-level parser and once per subcommand, so that
    either ordering works:

        campradar --verbose refresh
        campradar refresh --verbose

    Argparse does not do this on its own — a flag declared only on the parent
    is rejected once a subcommand has been seen, which is a surprising failure
    to meet for the first time inside CI.

    Two details here are load-bearing:

    * Each parser gets *fresh* action objects rather than sharing them via
      ``parents=``. Shared actions are the same mutable objects, and
      ``set_defaults`` rewrites ``action.default`` in place — so configuring
      the top-level defaults would silently reconfigure the subparsers too.
    * Subparsers use ``SUPPRESS`` (``with_defaults=False``). A subcommand
      parses into its own namespace which is then copied over the outer one,
      so a real default there would overwrite a value the top-level parser
      already read. With SUPPRESS, an absent flag sets nothing and the
      top-level value survives.
    """
    verbose_default = False if with_defaults else argparse.SUPPRESS
    config_default = DEFAULT_CONFIG if with_defaults else argparse.SUPPRESS
    data_default = DEFAULT_DATA if with_defaults else argparse.SUPPRESS

    parser.add_argument(
        "-v", "--verbose", action="store_true", default=verbose_default,
        help="log every fetch and parse step",
    )
    parser.add_argument(
        "--config", type=Path, default=config_default,
        help=f"directory holding sources.yaml and breaks.yaml (default: {DEFAULT_CONFIG})",
    )
    parser.add_argument(
        "--data", type=Path, default=data_default,
        help=f"directory for state.json and the fetch cache (default: {DEFAULT_DATA})",
    )


def build_parser() -> argparse.ArgumentParser:
    """Build the full parser. See `_add_global_flags` for the flag placement."""
    parser = argparse.ArgumentParser(prog="campradar", description=__doc__)
    _add_global_flags(parser, with_defaults=True)

    sub = parser.add_subparsers(dest="command", required=True)

    refresh = sub.add_parser("refresh", help="fetch all sources and update state")
    _add_global_flags(refresh, with_defaults=False)
    refresh.add_argument("--site-data", type=Path, default=DEFAULT_SITE_DATA)
    refresh.add_argument(
        "--previous-url",
        default=None,
        metavar="URL",
        help=(
            "read prior state from a published sessions.json instead of "
            "data/state.json. Lets CI track changes without commit access; "
            "e.g. https://USER.github.io/camp-radar/assets/data/sessions.json"
        ),
    )
    refresh.set_defaults(func=cmd_refresh)

    probe = sub.add_parser("probe", help="check a URL for usable JSON-LD")
    _add_global_flags(probe, with_defaults=False)
    probe.add_argument(
        "url",
        nargs="?",
        default=None,
        help="page to inspect; omit to survey every source in sources.yaml",
    )
    probe.set_defaults(func=cmd_probe)

    discover = sub.add_parser(
        "active-discover",
        help="ask the ACTIVE API what it holds near a place (coverage check)",
    )
    _add_global_flags(discover, with_defaults=False)
    discover.add_argument("--near", default="Decatur,GA,US", help="place to search around")
    discover.add_argument("--radius", type=int, default=25, help="miles (default 25)")
    discover.add_argument(
        "--start-date",
        default=None,
        metavar="RANGE",
        help="ACTIVE range, e.g. 2026-08-01..2027-08-31; omit to see everything",
    )
    discover.add_argument(
        "--no-kids",
        dest="kids",
        action="store_false",
        help="drop the kids=true filter (useful when a query returns nothing)",
    )
    discover.add_argument(
        "--org-query",
        default=None,
        metavar="NAME",
        help="look up organizationGuid values by name instead of showing facets",
    )
    discover.add_argument("--top", type=int, default=25, help="facet rows to show")
    discover.add_argument("--delay", type=float, default=0.6, help="seconds between calls")
    discover.add_argument("--api-key-env", default="ACTIVE_API_KEY", dest="api_key_env")
    discover.add_argument(
        "--api-base",
        default=None,
        metavar="URL",
        help="override the endpoint, to test against a fixture server",
    )
    discover.set_defaults(func=cmd_active_discover, kids=True)

    tribe = sub.add_parser(
        "tribe-discover",
        help="ask an Events Calendar site what is actually in it",
    )
    _add_global_flags(tribe, with_defaults=False)
    tribe.add_argument("base_url", help="site root, e.g. https://callanwolde.org")
    tribe.add_argument("--search", default=None, help="also report how many match this term")
    tribe.add_argument("--start-date", default=None, metavar="YYYY-MM-DD")
    tribe.add_argument("--end-date", default=None, metavar="YYYY-MM-DD")
    tribe.add_argument("--top", type=int, default=25, help="rows to show")
    tribe.add_argument("--delay", type=float, default=1.0, help="seconds between calls")
    tribe.set_defaults(func=cmd_tribe_discover)

    export = sub.add_parser("export", help="write an .ics from current state")
    _add_global_flags(export, with_defaults=False)
    export.add_argument("-o", "--output", type=Path, default=Path("camps.ics"))
    export.add_argument("--include-past", action="store_true")
    export.set_defaults(func=cmd_export)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _configure_logging(args.verbose)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
