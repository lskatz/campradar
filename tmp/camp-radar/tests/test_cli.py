"""Smoke tests for argument parsing.

These exist because of a real failure: `--verbose` was originally declared only
on the top-level parser, so `campradar refresh --verbose` was rejected while
`campradar --verbose refresh` worked. Nothing caught it until it broke a
scheduled GitHub Actions run, because the unit tests all called the library
directly and never went through the parser.

Parsing only — no command is executed, so these stay fast and offline.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from campradar.cli import build_parser, cmd_export, cmd_probe, cmd_refresh


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
    def test_probe_requires_a_url(self):
        with pytest.raises(SystemExit):
            parse(["probe"])

    def test_export_output_defaults_and_overrides(self):
        assert parse(["export"]).output == Path("camps.ics")
        assert parse(["export", "-o", "spring.ics"]).output == Path("spring.ics")

    def test_refresh_site_data_default(self):
        assert parse(["refresh"]).site_data == Path("site/assets/data")
