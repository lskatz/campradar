"""Command-line entry point.

Uses argparse rather than a CLI framework to keep the dependency list short —
this runs unattended in CI, where every extra package is another thing that can
break a scheduled job nobody is watching.

Commands:
    refresh   Fetch all sources, update state, write site data.
    probe     Survey every configured source — or one URL — for usable JSON-LD.
    export    Write an .ics file from current state.
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .adapters.jsonld import extract_jsonld_objects, is_event
from .delta import load_state
from .fetch import Fetcher
from .icsgen import render_calendar
from .pipeline import load_sources, run_pipeline

DEFAULT_CONFIG = Path("config")
DEFAULT_DATA = Path("data")
DEFAULT_SITE_DATA = Path("site/assets/data")


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)-7s %(name)s: %(message)s",
    )


def cmd_refresh(args: argparse.Namespace) -> int:
    """Run the pipeline and report what changed."""
    result = run_pipeline(
        config_dir=args.config,
        data_dir=args.data,
        site_data_dir=args.site_data,
        previous_url=args.previous_url,
    )

    print(f"Sources: {len(result.succeeded_sources)} ok, {len(result.failed_sources)} failed")
    if result.failed_sources:
        print(f"  failed: {', '.join(result.failed_sources)}")
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
        report.error = str(exc).splitlines()[0]
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
    if show_marker:
        marker = "[on]" if report.enabled else "[off]"
        print(f"{marker:<5} {report.source_id}")
        # In a sweep the id is the heading, so each status line carries the URL.
        where = f"  {report.url}"
    else:
        # Probing one page: the URL is the heading and would only repeat below.
        print(report.url)
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
