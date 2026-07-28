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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="campradar", description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)

    sub = parser.add_subparsers(dest="command", required=True)

    refresh = sub.add_parser("refresh", help="fetch all sources and update state")
    refresh.add_argument("--site-data", type=Path, default=DEFAULT_SITE_DATA)
    refresh.set_defaults(func=cmd_refresh)

    probe = sub.add_parser("probe", help="check a URL for usable JSON-LD")
    probe.add_argument("url")
    probe.set_defaults(func=cmd_probe)

    export = sub.add_parser("export", help="write an .ics from current state")
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
