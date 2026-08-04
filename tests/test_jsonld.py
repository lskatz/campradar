"""Parser edge cases beyond what the control fixture exercises."""

from __future__ import annotations

import pytest

from campradar.jsonld import extract_jsonld_objects, is_event, parse_age_text, parse_page


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Ages 6-12", (6, 12)),
        ("ages 6 to 12", (6, 12)),
        ("6\u201312 years", (6, 12)),
        ("For rising grades 3-5", (8, 10)),  # grades win over the bare digits
        ("grades 6 through 8", (11, 13)),
        ("A great week outdoors", (None, None)),
        ("", (None, None)),
        (None, (None, None)),
        ("Ages 4-99", (None, None)),  # out of range, so not stated
    ],
)
def test_parse_age_text(text, expected):
    assert parse_age_text(text) == expected


def test_grades_are_checked_before_ages():
    """The load-bearing ordering.

    "rising grades 3-5" also matches the age pattern, which would file a camp
    for eight-year-olds under three-year-olds.
    """
    assert parse_age_text("Camp for rising grades 3-5") == (8, 10)


def test_extract_flattens_graph_wrappers():
    html = """
    <script type="application/ld+json">
    {"@graph": [{"@type": "Event", "name": "A"}, {"@type": "WebPage", "name": "B"}]}
    </script>
    """
    objects = extract_jsonld_objects(html)
    assert [o["name"] for o in objects] == ["A", "B"]
    assert [o["name"] for o in objects if is_event(o)] == ["A"]


def test_extract_survives_one_broken_block_among_good_ones():
    html = """
    <script type="application/ld+json">{ not json at all </script>
    <script type="application/ld+json">{"@type": "Event", "name": "Survivor"}</script>
    """
    assert [o["name"] for o in extract_jsonld_objects(html)] == ["Survivor"]


def test_a_page_with_no_markup_yields_nothing():
    sessions = parse_page(
        "<html><body><p>Camps coming soon!</p></body></html>",
        source_id="s",
        provider_slug="p",
        fallback_url="https://example.org/",
    )
    assert sessions == []


def test_missing_end_date_falls_back_to_start():
    html = """
    <script type="application/ld+json">
    {"@type": "Event", "name": "One Day", "startDate": "2026-11-03"}
    </script>
    """
    session = parse_page(
        html, source_id="s", provider_slug="p", fallback_url="https://example.org/"
    )[0]
    assert session.start_date == session.end_date
    assert session.duration_days == 1


def test_a_listing_that_ends_before_it_starts_is_skipped():
    """Skipped, not raised. One bad row must not lose the rest of the page."""
    html = """
    <script type="application/ld+json">
    [{"@type": "Event", "name": "Backwards",
      "startDate": "2026-11-05", "endDate": "2026-11-01"},
     {"@type": "Event", "name": "Fine", "startDate": "2026-11-03"}]
    </script>
    """
    sessions = parse_page(
        html, source_id="s", provider_slug="p", fallback_url="https://example.org/"
    )
    assert [s.title for s in sessions] == ["Fine"]


def test_url_falls_back_to_the_page_when_the_listing_has_none():
    html = """
    <script type="application/ld+json">
    {"@type": "Event", "name": "No Link", "startDate": "2026-11-03"}
    </script>
    """
    session = parse_page(
        html, source_id="s", provider_slug="p", fallback_url="https://example.org/camps"
    )[0]
    assert str(session.url) == "https://example.org/camps"
