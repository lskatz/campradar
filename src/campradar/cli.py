"""Command line interface. Two commands, and that is the whole program.

    campradar update    fetch every enabled source, update the local file
    campradar list      print the local file as TSV

The diff summary from `update` goes to stderr, not stdout, so that

    campradar update && campradar list > camps.tsv

stays clean in a pipe.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from . import jsonld, recdesk
from .fetch import Fetcher, FetchError
from .models import Provider, SessionRecord
from .store import (
    NeededRange,
    coverage,
    load_needed_ranges,
    load_state,
    merge,
    save_state,
)

__all__ = ["main"]

log = logging.getLogger("campradar")

DEFAULT_CAMPS = Path("config/camps.yaml")
DEFAULT_DATES = Path("config/dates.yaml")
DEFAULT_STATE = Path("data/camps.json")
DEFAULT_CACHE = Path("data/cache")

#: Maps the `adapter:` key in camps.yaml to a reader. Every reader has the same
#: signature — (source config, fetcher) -> list[CampSession] — which is the
#: whole contract. Add a parser here and it becomes configurable.
READERS = {
    "jsonld": jsonld.read_source,
    "recdesk": recdesk.read_source,
}

#: Column order for `list`. This is the tool's public contract — append new
#: columns at the end so that anything parsing by position keeps working.
COLUMNS = (
    "key",
    "first_seen",
    "is_new",
    "start_date",
    "end_date",
    "breaks",
    "needed_days",
    "title",
    "provider",
    "source_id",
    "min_age",
    "max_age",
    "daily_start",
    "daily_end",
    "price_usd",
    "status",
    "url",
)


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------


def load_camps_config(path: Path) -> tuple[dict[str, Provider], list[dict[str, Any]]]:
    """Read `camps.yaml` into providers (by slug) and sources (in order)."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    providers = {p["slug"]: Provider.model_validate(p) for p in raw.get("providers", [])}
    sources = list(raw.get("sources", []))

    for source in sources:
        slug = source.get("provider_slug", source["id"])
        if slug not in providers:
            raise ValueError(f"source {source['id']}: unknown provider_slug {slug!r}")
        adapter = source.get("adapter", "jsonld")
        if adapter not in READERS:
            known = ", ".join(sorted(READERS))
            raise ValueError(f"source {source['id']}: unknown adapter {adapter!r} (known: {known})")
    return providers, sources


# --------------------------------------------------------------------------
# update
# --------------------------------------------------------------------------


def cmd_update(args: argparse.Namespace) -> int:
    providers, sources = load_camps_config(args.camps)
    enabled = [s for s in sources if s.get("enabled", True)]
    if not enabled:
        print("no enabled sources in", args.camps, file=sys.stderr)
        return 1

    now = datetime.now(UTC)
    scraped = []
    failures: list[str] = []

    with Fetcher(args.cache, delay_seconds=args.delay) as fetcher:
        for source in enabled:
            reader = READERS[source.get("adapter", "jsonld")]
            try:
                found = reader(source, fetcher)
            except (FetchError, ValueError) as exc:
                # Loud per source, but one dead site does not stop the run.
                failures.append(source["id"])
                print(f"  !! {source['id']}: {exc}", file=sys.stderr)
                continue
            print(f"  {source['id']}: {len(found)} session(s)", file=sys.stderr)
            scraped.extend(found)

    if len(failures) == len(enabled):
        # Writing an empty scrape over good state would mark every camp as
        # disappeared and then re-report them all as new on the next run.
        print("every source failed; state not written", file=sys.stderr)
        return 1

    previous, _ = load_state(args.state)
    state, report = merge(previous, scraped, now=now)
    save_state(args.state, state, now=now)

    print(f"{report.summary()} ({len(state)} total) -> {args.state}", file=sys.stderr)
    for record in report.new:
        print(f"  + {record.session.start_date} {record.session.title}", file=sys.stderr)
    return 0


# --------------------------------------------------------------------------
# list
# --------------------------------------------------------------------------


def _cell(value: Any) -> str:
    """One TSV cell. Missing is empty string; tabs and newlines cannot survive."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    text = str(value)
    return text.replace("\t", " ").replace("\r", " ").replace("\n", " ").strip()


def row_for(
    record: SessionRecord,
    *,
    ranges: list[NeededRange],
    providers: dict[str, Provider],
    last_run: datetime | None,
) -> list[str]:
    """Render one record as TSV cells, in `COLUMNS` order."""
    session = record.session
    slugs, days = coverage(session, ranges)
    provider = providers.get(session.provider_slug)
    is_new = last_run is not None and record.first_seen >= last_run

    values: dict[str, Any] = {
        "key": record.key,
        "first_seen": record.first_seen.isoformat(),
        "is_new": is_new,
        "start_date": session.start_date.isoformat(),
        "end_date": session.end_date.isoformat(),
        "breaks": ",".join(slugs),
        "needed_days": ",".join(d.isoformat() for d in days),
        "title": session.title,
        "provider": provider.name if provider else session.provider_slug,
        "source_id": session.source_id,
        "min_age": session.min_age,
        "max_age": session.max_age,
        "daily_start": session.daily_start,
        "daily_end": session.daily_end,
        "price_usd": session.price_usd,
        "status": session.registration_status.value,
        "url": session.url,
    }
    return [_cell(values[name]) for name in COLUMNS]


def cmd_list(args: argparse.Namespace) -> int:
    state, last_run = load_state(args.state)
    if not state:
        print(f"no data in {args.state}; run `campradar update` first", file=sys.stderr)
        return 1

    providers, _ = load_camps_config(args.camps)
    ranges = load_needed_ranges(args.dates)

    # Sorted by start date so the file reads as a calendar, with the key as a
    # tiebreaker to keep the output byte-stable between runs.
    records = sorted(state.values(), key=lambda r: (r.session.start_date, r.key))

    out = sys.stdout
    print("\t".join(COLUMNS), file=out)
    for record in records:
        print(
            "\t".join(row_for(record, ranges=ranges, providers=providers, last_run=last_run)),
            file=out,
        )
    return 0


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="campradar", description="Track school-break camps.")
    parser.add_argument("--camps", type=Path, default=DEFAULT_CAMPS)
    parser.add_argument("--dates", type=Path, default=DEFAULT_DATES)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("-v", "--verbose", action="store_true")

    sub = parser.add_subparsers(dest="command", required=True)

    update = sub.add_parser("update", help="fetch sources and update the local file")
    update.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    update.add_argument("--delay", type=float, default=1.5, help="seconds between requests")
    update.set_defaults(func=cmd_update)

    listing = sub.add_parser("list", help="print the local file as TSV")
    listing.set_defaults(func=cmd_list)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        return int(args.func(args))
    except FileNotFoundError as exc:
        print(f"missing file: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
