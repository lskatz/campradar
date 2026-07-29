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

import httpx
import yaml

from .adapters import REGISTRY, AdapterError
from .delta import DeltaReport, load_state, merge, save_state, state_from_published
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


def _hydrate_previous(
    state_path: Path, previous_url: str | None
) -> dict[str, Any]:
    """Load prior state, preferring a published sessions.json when given.

    With `previous_url` set, the deployed site acts as the state store and CI
    needs no write access to the repository at all. A failure to reach it is
    logged but not fatal: the run continues from an empty state, which
    over-reports "new" for one cycle but never loses data.
    """
    if previous_url is None:
        return load_state(state_path)

    try:
        response = httpx.get(previous_url, timeout=20.0, follow_redirects=True)
        response.raise_for_status()
        state = state_from_published(response.json())
        log.info("hydrated %d sessions from %s", len(state), previous_url)
        return state
    except Exception as exc:  # noqa: BLE001 - never let this abort a run
        # Expected on the very first deploy, when nothing is published yet.
        log.warning("could not hydrate from %s (%s); starting fresh", previous_url, exc)
        return {}


def run_pipeline(
    *,
    config_dir: Path,
    data_dir: Path,
    site_data_dir: Path,
    now: datetime | None = None,
    previous_url: str | None = None,
) -> RunResult:
    """Fetch every source, merge into state, and write the site's data file.

    Args:
        previous_url: If set, prior state is read from this published
            `sessions.json` rather than from `data/state.json`. This is how CI
            runs without commit access — see docs/architecture.md.
    """
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

    previous = _hydrate_previous(state_path, previous_url)
    state, delta = merge(previous, scraped, now=now)
    result.delta = delta

    # Only persist *state* when something worked. Writing an empty state after
    # a total failure would mark every real session as "disappeared" and then,
    # on recovery, as "new" — a false alert storm.
    if not result.is_total_failure and previous_url is None:
        # Skipped when hydrating from a URL: in that mode the published site is
        # the state store, and CI has no business writing to the repo.
        save_state(state_path, state)

    # But always publish the site data. On a total failure `state` still holds
    # every prior session carried forward unchanged, so nothing is lost — and
    # the run block must report the failure, or the dashboard would look
    # perfectly healthy while quietly collecting nothing. A silently stale
    # dashboard is the worst outcome here: it is indistinguishable from a
    # working one right up until you miss a registration date.
    _write_site_data(site_data_dir, state, breaks, providers, delta, now, result)

    return result


def _write_site_data(
    site_data_dir: Path,
    state: dict[str, Any],
    breaks: list[SchoolBreak],
    providers: dict[str, Provider],
    delta: DeltaReport,
    now: datetime,
    result: "RunResult",
) -> None:
    """Emit the single JSON file the dashboard reads.

    Written under site/assets/data/ rather than site/_data/ because the
    dashboard fetches it at runtime and the whole site/ directory is served
    verbatim by GitHub Pages — no Jekyll, no build step. A path starting with
    an underscore would work too, but assets/data/ says what it is.

    Everything the browser needs ships in one request. There is no API and no
    server, which is what keeps hosting free and keeps visitor data — the kids'
    ages and names entered in the browser — from ever reaching us.
    """
    import json

    new_keys = {record.key for record in delta.new}
    site_data_dir.mkdir(parents=True, exist_ok=True)

    payload = {
        "generated_at": now.isoformat(),
        # Run metadata, so the dashboard can tell an unconfigured install
        # ("no sources enabled yet") apart from a broken one ("three sources
        # failed"). Without this an empty page looks the same in both cases,
        # which is exactly the wrong signal to give someone at 7am.
        "run": {
            "sources_ok": sorted(result.succeeded_sources),
            "sources_failed": sorted(result.failed_sources),
            "sessions_found": result.sessions_found,
        },
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
                # Published so that state_from_published() can rebuild full
                # state from the deployed site, which is what allows CI to run
                # without committing anything back to the repository.
                "last_seen": record.last_seen.isoformat(),
                "is_new": record.key in new_keys,
            }
            for record in sorted(state.values(), key=lambda r: r.session.start_date)
        ],
    }
    (site_data_dir / "sessions.json").write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8"
    )
