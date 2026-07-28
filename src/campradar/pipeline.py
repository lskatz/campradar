"""Run every configured source and publish the result.

The pipeline is intentionally boring. All the interesting logic lives in the
adapters (getting data out of the world) and in `delta` (working out what
changed); this module just sequences them and handles failure.

Failure policy: one source raising `AdapterError` is logged and recorded in the
run report, but does not stop the run. A run only fails outright if *every*
source failed, which is the signal that something systemic broke — a bad
deploy, no network — rather than one provider redesigning their site.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from .adapters import REGISTRY, AdapterError
from .delta import DeltaReport, load_state, merge, save_state
from .fetch import Fetcher
from .models import CampSession, Provider

__all__ = ["RunResult", "run_pipeline", "load_sources", "load_breaks", "SchoolBreak"]

log = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class SchoolBreak:
    """A window when school is out and childcare is needed."""

    name: str
    start: date
    end: date

    def contains(self, day: date) -> bool:
        return self.start <= day <= self.end


@dataclass(slots=True)
class RunResult:
    """Outcome of one full pipeline run."""

    sessions_found: int = 0
    delta: DeltaReport | None = None
    failed_sources: list[str] = field(default_factory=list)
    succeeded_sources: list[str] = field(default_factory=list)

    @property
    def total_sources(self) -> int:
        return len(self.failed_sources) + len(self.succeeded_sources)

    @property
    def is_total_failure(self) -> bool:
        """True when nothing worked, which is the only condition worth failing CI over."""
        return self.total_sources > 0 and not self.succeeded_sources


# --------------------------------------------------------------------------
# config loading
# --------------------------------------------------------------------------


def load_sources(path: Path) -> tuple[list[dict[str, Any]], dict[str, Provider]]:
    """Read sources.yaml into source configs and a provider lookup."""
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    providers = {
        item["slug"]: Provider.model_validate(item) for item in data.get("providers", [])
    }
    return data.get("sources", []), providers


def load_breaks(path: Path) -> list[SchoolBreak]:
    """Read breaks.yaml.

    Hand-maintained on purpose: the district publishes its calendar as a flat
    image with no PDF or alt text, so OCR would be both fragile and pointless
    for roughly five minutes of typing once a year.
    """
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return [
        SchoolBreak(name=item["name"], start=item["start"], end=item["end"])
        for item in data.get("breaks", [])
    ]


# --------------------------------------------------------------------------
# the run
# --------------------------------------------------------------------------


def run_pipeline(
    *,
    config_dir: Path,
    data_dir: Path,
    site_data_dir: Path,
    now: datetime | None = None,
) -> RunResult:
    """Fetch every source, merge into state, and write the site's data file."""
    now = now or datetime.now(timezone.utc)
    result = RunResult()

    source_configs, providers = load_sources(config_dir / "sources.yaml")
    breaks = load_breaks(config_dir / "breaks.yaml")
    state_path = data_dir / "state.json"

    scraped: list[CampSession] = []
    with Fetcher(data_dir / "raw") as fetcher:
        for source_config in source_configs:
            if not source_config.get("enabled", True):
                continue
            source_id = source_config["id"]
            adapter_name = source_config["adapter"]

            adapter_cls = REGISTRY.get(adapter_name)
            if adapter_cls is None:
                log.error("%s: unknown adapter %r", source_id, adapter_name)
                result.failed_sources.append(source_id)
                continue

            try:
                sessions = adapter_cls(source_config).run(fetcher)
            except AdapterError as exc:
                log.error("%s", exc)
                result.failed_sources.append(source_id)
                continue

            log.info("%s: %d sessions", source_id, len(sessions))
            scraped.extend(sessions)
            result.succeeded_sources.append(source_id)

    result.sessions_found = len(scraped)

    previous = load_state(state_path)
    state, delta = merge(previous, scraped, now=now)
    result.delta = delta

    # Only persist when something actually worked. Writing an empty state after
    # a total failure would mark every real session as "disappeared" and then,
    # on recovery, as "new" — a false alert storm.
    if not result.is_total_failure:
        save_state(state_path, state)
        _write_site_data(site_data_dir, state, breaks, providers, delta, now)

    return result


def _write_site_data(
    site_data_dir: Path,
    state: dict[str, Any],
    breaks: list[SchoolBreak],
    providers: dict[str, Provider],
    delta: DeltaReport,
    now: datetime,
) -> None:
    """Emit the single JSON file the dashboard reads.

    Written under site/assets/data/ rather than site/_data/ because Jekyll
    treats _data as build-time input and never serves it; the browser fetches
    this file directly at runtime.

    Everything the browser needs ships in one request. There is no API and no
    server, which is what keeps hosting free and keeps visitor data — the kids'
    ages and names entered in the browser — from ever reaching us.
    """
    import json

    new_keys = {record.key for record in delta.new}
    site_data_dir.mkdir(parents=True, exist_ok=True)

    payload = {
        "generated_at": now.isoformat(),
        "breaks": [
            {"name": b.name, "start": b.start.isoformat(), "end": b.end.isoformat()}
            for b in breaks
        ],
        "providers": {
            slug: json.loads(provider.model_dump_json()) for slug, provider in providers.items()
        },
        "sessions": [
            {
                **json.loads(record.session.model_dump_json()),
                "key": record.key,
                "first_seen": record.first_seen.isoformat(),
                "is_new": record.key in new_keys,
            }
            for record in sorted(state.values(), key=lambda r: r.session.start_date)
        ],
    }
    (site_data_dir / "sessions.json").write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8"
    )
