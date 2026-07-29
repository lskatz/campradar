"""Command-line entry point.

Uses argparse rather than a CLI framework to keep the dependency list short —
this runs unattended in CI, where every extra package is another thing that can
break a scheduled job nobody is watching.

Commands:
    refresh   Fetch all sources, update state, write site data.
    probe     Check whether a URL exposes usable JSON-LD before writing an adapter.
    export    Write an .ics file from current state.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

from .adapters.jsonld import extract_jsonld_objects
from .delta import load_state
from .fetch import Fetcher
from .icsgen import render_calendar
from .pipeline import run_pipeline

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


def cmd_probe(args: argparse.Namespace) -> int:
    """Report the JSON-LD a page exposes, to decide if `jsonld` will work."""
    with Fetcher(args.data / "raw") as fetcher:
        result = fetcher.get(args.url)

    objects = extract_jsonld_objects(result.text)
    if not objects:
        print("No JSON-LD found. This source needs a bespoke adapter.")
        return 1

    print(f"Found {len(objects)} JSON-LD object(s):")
    for obj in objects:
        obj_type = obj.get("@type", "?")
        name = str(obj.get("name", ""))[:60]
        has_date = "startDate" in obj
        marker = "usable" if has_date else "no startDate"
        print(f"  [{marker:>12}] {obj_type}: {name}")
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
    probe.add_argument("url")
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
