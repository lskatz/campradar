"""Smoke tests for argument parsing.

These exist because of a real failure: `--verbose` was originally declared only
on the top-level parser, so `campradar refresh --verbose` was rejected while
`campradar --verbose refresh` worked. Nothing caught it until it broke a
scheduled GitHub Actions run, because the unit tests all called the library
directly and never went through the parser.

Mostly parsing, plus the command paths that never touch the network, so
these stay fast and offline.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from campradar.cli import (
    _probe_targets,
    build_parser,
    cmd_export,
    cmd_probe,
    cmd_refresh,
)


def parse(argv: list[str]):
    return build_parser().parse_args(argv)


class TestFlagPlacement:
    """Global flags must work on either side of the subcommand."""

    @pytest.mark.parametrize(
        "argv",
        [
            ["refresh", "--verbose"],   # the ordering CI used, and the one that broke
            ["--verbose", "refresh"],
            ["refresh", "-v"],
            ["-v", "refresh"],
        ],
    )
    def test_verbose_accepted_in_any_position(self, argv):
        assert parse(argv).verbose is True

    @pytest.mark.parametrize(
        "argv",
        [
            ["refresh", "--config", "custom"],
            ["--config", "custom", "refresh"],
        ],
    )
    def test_config_accepted_in_any_position(self, argv):
        assert parse(argv).config == Path("custom")

    @pytest.mark.parametrize(
        "argv",
        [
            ["refresh", "--data", "elsewhere"],
            ["--data", "elsewhere", "refresh"],
        ],
    )
    def test_data_accepted_in_any_position(self, argv):
        assert parse(argv).data == Path("elsewhere")

    def test_flags_work_on_every_subcommand(self):
        assert parse(["probe", "https://example.org", "--verbose"]).verbose is True
        assert parse(["export", "--verbose"]).verbose is True


class TestDefaultsSurviveSubparsers:
    """The SUPPRESS trick: a subparser must not clobber the parent's value."""

    def test_leading_flag_is_not_overwritten_by_subparser_default(self):
        """`--config custom refresh` must not silently fall back to `config/`."""
        assert parse(["--config", "custom", "refresh"]).config == Path("custom")

    def test_defaults_apply_when_no_flag_given(self):
        args = parse(["refresh"])
        assert args.config == Path("config")
        assert args.data == Path("data")
        assert args.verbose is False


class TestDispatch:
    def test_each_subcommand_binds_its_handler(self):
        assert parse(["refresh"]).func is cmd_refresh
        assert parse(["probe", "https://example.org"]).func is cmd_probe
        assert parse(["export"]).func is cmd_export

    def test_subcommand_is_required(self):
        with pytest.raises(SystemExit):
            parse([])

    def test_unknown_subcommand_is_rejected(self):
        with pytest.raises(SystemExit):
            parse(["frobnicate"])


class TestSubcommandOptions:
    def test_probe_url_is_optional(self):
        """`make probe` runs bare — a required positional made the target unusable."""
        assert parse(["probe"]).url is None

    def test_probe_accepts_a_single_url(self):
        assert parse(["probe", "https://example.org"]).url == "https://example.org"

    def test_export_output_defaults_and_overrides(self):
        assert parse(["export"]).output == Path("camps.ics")
        assert parse(["export", "-o", "spring.ics"]).output == Path("spring.ics")

    def test_refresh_site_data_default(self):
        assert parse(["refresh"]).site_data == Path("site/assets/data")


class TestProbeTargets:
    """`campradar probe` with no URL surveys sources.yaml.

    This is what `make probe` invokes, and it regressed once already: the
    Makefile and README both documented a whole-config survey while the parser
    demanded a positional URL, so the target could never succeed.
    """

    @staticmethod
    def write_config(tmp_path: Path, body: str) -> Path:
        config = tmp_path / "config"
        config.mkdir()
        (config / "sources.yaml").write_text(body, encoding="utf-8")
        return config

    def test_disabled_sources_are_included(self, tmp_path):
        """Retired and placeholder entries are the point of probing, not noise."""
        config = self.write_config(
            tmp_path,
            """
            sources:
              - id: on-by-default
                adapter: jsonld
                urls: [https://a.example/camps]
              - id: retired
                adapter: jsonld
                enabled: false
                urls: [https://b.example/camps]
            """,
        )
        assert _probe_targets(config) == [
            ("on-by-default", "https://a.example/camps", True),
            ("retired", "https://b.example/camps", False),
        ]

    def test_every_url_of_a_multi_url_source_is_probed(self, tmp_path):
        config = self.write_config(
            tmp_path,
            """
            sources:
              - id: two-pages
                adapter: jsonld
                enabled: true
                urls: [https://a.example/one, https://a.example/two]
            """,
        )
        assert [url for _id, url, _on in _probe_targets(config)] == [
            "https://a.example/one",
            "https://a.example/two",
        ]

    def test_empty_config_is_an_error_not_a_silent_pass(self, tmp_path):
        """Exiting 0 here would let `make probe && make update` look healthy."""
        config = self.write_config(tmp_path, "sources: []\n")
        args = parse(["probe", "--config", str(config), "--data", str(tmp_path / "data")])
        assert cmd_probe(args) == 1
