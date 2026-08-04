"""End to end: config in, TSV out, without a socket.

The Fetcher is real; only its transport is mocked. That keeps the caching and
throttling code on the tested path rather than stubbing past it.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from campradar import cli
from campradar.cli import COLUMNS
from campradar.fetch import Fetcher, FetchError

PAGE_URL = "https://example.org/camps"

CAMPS_YAML = """
providers:
  - slug: example-camps
    name: Example Camps (fixture)
    homepage: https://example.org/
sources:
  - id: example-camps
    provider_slug: example-camps
    adapter: jsonld
    enabled: true
    urls:
      - https://example.org/camps
"""


@pytest.fixture
def workspace(tmp_path, control_html, monkeypatch):
    """A throwaway repo whose one source serves the control fixture."""
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "camps.yaml").write_text(CAMPS_YAML)

    dates = tmp_path / "config" / "dates.yaml"
    dates.write_text(
        "breaks:\n"
        "  - {slug: fall-break, name: Fall Break, start: 2026-10-05, end: 2026-10-09}\n"
        "  - {slug: winter-break, name: Winter Break, start: 2026-12-21, end: 2027-01-04}\n"
        "  - {slug: february-break, name: February Break,"
        " start: 2027-02-15, end: 2027-02-19}\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == PAGE_URL:
            return httpx.Response(200, text=control_html)
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(
        cli, "Fetcher", lambda cache, **kw: Fetcher(cache, delay_seconds=0, client=client)
    )
    return tmp_path


def run(workspace, *args: str) -> int:
    return cli.main(
        [
            "--camps",
            str(workspace / "config" / "camps.yaml"),
            "--dates",
            str(workspace / "config" / "dates.yaml"),
            "--state",
            str(workspace / "data" / "camps.json"),
            *args,
        ]
    )


def tsv(capsys) -> list[list[str]]:
    # Strip newlines only. A plain .strip() would eat the trailing tab of a row
    # whose last column is empty, and silently under-count its cells.
    out = capsys.readouterr().out.strip("\n").split("\n")
    return [line.split("\t") for line in out]


def test_update_then_list(workspace, capsys):
    assert run(workspace, "update", "--cache", str(workspace / "cache")) == 0
    capsys.readouterr()

    assert run(workspace, "list") == 0
    rows = tsv(capsys)

    assert rows[0] == list(COLUMNS)
    assert len(rows) == 5  # header plus the four control sessions
    assert all(len(row) == len(COLUMNS) for row in rows)


def test_list_is_sorted_by_start_date(workspace, capsys):
    run(workspace, "update", "--cache", str(workspace / "cache"))
    capsys.readouterr()
    run(workspace, "list")
    rows = tsv(capsys)[1:]

    starts = [row[COLUMNS.index("start_date")] for row in rows]
    assert starts == sorted(starts)


def test_break_and_day_columns_are_filterable(workspace, capsys):
    run(workspace, "update", "--cache", str(workspace / "cache"))
    capsys.readouterr()
    run(workspace, "list")
    rows = tsv(capsys)[1:]

    by_break = {row[COLUMNS.index("breaks")]: row for row in rows}
    assert "fall-break" in by_break
    assert "" in by_break  # the March robotics workshop covers nothing needed

    art = next(r for r in rows if r[COLUMNS.index("breaks")] == "february-break")
    # Five-day session, three needed days. The two columns disagree on purpose.
    assert art[COLUMNS.index("needed_days")] == "2027-02-15,2027-02-16,2027-02-17"


def test_everything_is_new_on_the_first_run_and_not_on_the_second(workspace, capsys):
    cache = str(workspace / "cache")
    run(workspace, "update", "--cache", cache)
    capsys.readouterr()
    run(workspace, "list")
    assert {row[COLUMNS.index("is_new")] for row in tsv(capsys)[1:]} == {"1"}

    run(workspace, "update", "--cache", cache)
    capsys.readouterr()
    run(workspace, "list")
    assert {row[COLUMNS.index("is_new")] for row in tsv(capsys)[1:]} == {"0"}


def test_diff_summary_goes_to_stderr_so_stdout_stays_pipeable(workspace, capsys):
    run(workspace, "update", "--cache", str(workspace / "cache"))
    captured = capsys.readouterr()
    assert "4 new" in captured.err
    assert captured.out == ""


def test_list_before_update_fails_cleanly(workspace, capsys):
    assert run(workspace, "list") == 1
    assert "run `campradar update`" in capsys.readouterr().err


def test_a_dead_source_fails_the_run_without_writing_state(workspace, capsys):
    """Overwriting good state with an empty scrape is the expensive mistake."""
    (workspace / "config" / "camps.yaml").write_text(
        CAMPS_YAML.replace("https://example.org/camps", "https://example.org/gone")
    )
    assert run(workspace, "update", "--cache", str(workspace / "cache")) == 1
    assert "state not written" in capsys.readouterr().err
    assert not (workspace / "data" / "camps.json").exists()


def test_unknown_provider_slug_is_a_config_error(workspace, capsys):
    (workspace / "config" / "camps.yaml").write_text(
        CAMPS_YAML.replace("provider_slug: example-camps", "provider_slug: typo")
    )
    assert run(workspace, "update", "--cache", str(workspace / "cache")) == 1
    assert "unknown provider_slug" in capsys.readouterr().err


def test_tabs_in_a_title_cannot_break_the_table(workspace, capsys, monkeypatch):
    from campradar.store import merge, save_state
    from conftest import RUN_ONE, make_session

    state, _ = merge({}, [make_session("Messy\tTitle\nSecond line")], now=RUN_ONE)
    save_state(workspace / "data" / "camps.json", state, now=RUN_ONE)

    run(workspace, "list")
    rows = tsv(capsys)
    assert len(rows) == 2
    assert len(rows[1]) == len(COLUMNS)


# --------------------------------------------------------------------------
# fetch
# --------------------------------------------------------------------------


def test_not_modified_serves_the_cached_body(tmp_path):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.headers.get("If-None-Match"))
        if request.headers.get("If-None-Match") == '"v1"':
            return httpx.Response(304)
        return httpx.Response(200, text="hello", headers={"ETag": '"v1"'})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    fetcher = Fetcher(tmp_path / "cache", delay_seconds=0, client=client)

    first = fetcher.get("https://example.org/x")
    second = fetcher.get("https://example.org/x")

    assert first.from_cache is False
    assert second.from_cache is True
    assert second.text == "hello"
    assert calls == [None, '"v1"']


def test_an_http_error_is_raised_not_swallowed(tmp_path):
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    fetcher = Fetcher(tmp_path / "cache", delay_seconds=0, client=client)
    with pytest.raises(FetchError, match="500"):
        fetcher.get("https://example.org/x")


def test_a_bare_path_is_read_from_disk(tmp_path):
    """Saved pages are how you develop a parser without hammering a server."""
    page = tmp_path / "saved.html"
    page.write_text("<html><body>hi</body></html>", encoding="utf-8")

    fetcher = Fetcher(tmp_path / "cache", delay_seconds=0)
    assert fetcher.get(str(page)).text == "<html><body>hi</body></html>"
    assert fetcher.get(f"file://{page}").text == "<html><body>hi</body></html>"


def test_a_missing_local_file_is_a_fetch_error(tmp_path):
    fetcher = Fetcher(tmp_path / "cache", delay_seconds=0)
    with pytest.raises(FetchError):
        fetcher.get(str(tmp_path / "absent.html"))


def test_the_shipped_config_works_with_no_network(tmp_path, monkeypatch):
    """A fresh clone must produce output from `campradar update` alone.

    Real sources are stripped first. The suite must never touch a provider's
    server: it would be rude, it would be flaky, and a green build would then
    depend on someone else's uptime.
    """
    import yaml

    repo = Path(__file__).resolve().parent.parent
    monkeypatch.chdir(repo)

    shipped = yaml.safe_load((repo / "config" / "camps.yaml").read_text())
    shipped["sources"] = [
        s
        for s in shipped["sources"]
        if all(not str(u).startswith("http") for u in s.get("urls", [])) and not s.get("base_url")
    ]
    assert shipped["sources"], "no local source left to test with"

    camps = tmp_path / "camps.yaml"
    camps.write_text(yaml.safe_dump(shipped))
    state = tmp_path / "camps.json"

    assert (
        cli.main(
            [
                "--camps",
                str(camps),
                "--dates",
                str(repo / "config" / "dates.yaml"),
                "--state",
                str(state),
                "update",
                "--cache",
                str(tmp_path / "cache"),
                "--delay",
                "0",
            ]
        )
        == 0
    )

    # All four, not just the two that carry their own url. A local source path
    # is not a valid listing link, and falling back to it used to drop them.
    import json

    assert len(json.loads(state.read_text())["sessions"]) == 4


def test_the_shipped_config_is_valid(tmp_path):
    """Every source names a known adapter and a provider that exists."""
    repo = Path(__file__).resolve().parent.parent
    providers, sources = cli.load_camps_config(repo / "config" / "camps.yaml")

    assert providers
    assert sources
    enabled = [s["id"] for s in sources if s.get("enabled")]
    assert "tucker-rec" in enabled
