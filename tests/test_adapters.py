"""Smoke tests for the JSON-LD adapter and the calendar writer.

The fixture HTML below mirrors the shapes really seen in the wild: a @graph
wrapper, a bare event, one deliberately malformed script block, and one event
with no start date. A parser that survives all four will survive most real
provider pages.
"""

from __future__ import annotations

from datetime import date

from campradar.adapters.jsonld import (
    JsonLdAdapter,
    extract_jsonld_objects,
    parse_age_text,
)
from campradar.icsgen import render_calendar
from campradar.models import CampSession, RegistrationStatus

FIXTURE_HTML = """
<html><head>
<script type="application/ld+json">
{"@context":"https://schema.org","@graph":[
  {"@type":"Event","name":"Pond Explorers","startDate":"2027-04-05",
   "endDate":"2027-04-09","typicalAgeRange":"6-10",
   "offers":{"@type":"Offer","price":"325.00","availability":"https://schema.org/InStock"},
   "url":"https://example.org/pond"}
]}
</script>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"Event","name":"Robotics Week",
 "startDate":"2027-04-12T09:00:00-04:00","endDate":"2027-04-16T15:00:00-04:00",
 "description":"For rising grades 3-5. Build and program a robot.",
 "offers":{"availability":"https://schema.org/SoldOut"}}
</script>
<script type="application/ld+json">{ this is not json }</script>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"Event","name":"Undated Mystery Camp"}
</script>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"Organization","name":"Not A Camp"}
</script>
</head><body></body></html>
"""


class FakeFetcher:
    """Stands in for `Fetcher`, returning fixture HTML without any network."""

    def __init__(self, html: str) -> None:
        self.html = html
        self.urls_requested: list[str] = []

    def get(self, url: str):
        self.urls_requested.append(url)

        class Result:
            text = self.html

        return Result()


class TestExtraction:
    def test_finds_objects_across_graph_and_bare_blocks(self):
        objects = extract_jsonld_objects(FIXTURE_HTML)
        names = {obj.get("name") for obj in objects}
        assert "Pond Explorers" in names
        assert "Robotics Week" in names

    def test_malformed_block_does_not_lose_the_good_ones(self):
        """One broken script tag must not cost us the rest of the page."""
        assert len(extract_jsonld_objects(FIXTURE_HTML)) >= 4

    def test_empty_page_yields_nothing(self):
        assert extract_jsonld_objects("<html><body>hi</body></html>") == []


class TestAgeParsing:
    def test_plain_age_range(self):
        assert parse_age_text("Ages 6-12") == (6, 12)

    def test_en_dash_range(self):
        assert parse_age_text("ages 6–12 years") == (6, 12)

    def test_grades_convert_to_ages(self):
        assert parse_age_text("For rising grades 3-5") == (8, 10)

    def test_unparseable_text_returns_nothing(self):
        assert parse_age_text("A wonderful week in the woods") == (None, None)

    def test_none_input_is_safe(self):
        assert parse_age_text(None) == (None, None)


class TestAdapter:
    def build(self) -> list[CampSession]:
        adapter = JsonLdAdapter(
            {
                "id": "test-source",
                "provider_slug": "fernbank-science-center",
                "urls": ["https://example.org/camps"],
            }
        )
        return adapter.run(FakeFetcher(FIXTURE_HTML))

    def test_parses_expected_sessions(self):
        sessions = self.build()
        titles = {s.title for s in sessions}
        assert titles == {"Pond Explorers", "Robotics Week"}

    def test_skips_non_event_types(self):
        assert "Not A Camp" not in {s.title for s in self.build()}

    def test_skips_events_without_a_start_date(self):
        """No date means it can't be placed on a calendar, so it isn't useful."""
        assert "Undated Mystery Camp" not in {s.title for s in self.build()}

    def test_reads_price_and_availability(self):
        pond = next(s for s in self.build() if s.title == "Pond Explorers")
        assert pond.price_usd == 325.00
        assert pond.registration_status is RegistrationStatus.OPEN

    def test_maps_sold_out_to_full(self):
        robotics = next(s for s in self.build() if s.title == "Robotics Week")
        assert robotics.registration_status is RegistrationStatus.FULL

    def test_falls_back_to_scanning_prose_for_ages(self):
        robotics = next(s for s in self.build() if s.title == "Robotics Week")
        assert (robotics.min_age, robotics.max_age) == (8, 10)

    def test_handles_datetime_start_dates(self):
        robotics = next(s for s in self.build() if s.title == "Robotics Week")
        assert robotics.start_date == date(2027, 4, 12)


class TestCalendar:
    def session(self) -> CampSession:
        return CampSession(
            provider_slug="fernbank-science-center",
            title="Pond Explorers",
            start_date=date(2027, 4, 5),
            end_date=date(2027, 4, 9),
            source_id="test",
        )

    def test_produces_a_wellformed_calendar(self):
        output = render_calendar([self.session()])
        assert output.startswith("BEGIN:VCALENDAR")
        assert output.rstrip().endswith("END:VCALENDAR")

    def test_dtend_is_exclusive(self):
        """A Mon-Fri camp must end Saturday, or Friday silently disappears."""
        output = render_calendar([self.session()])
        assert "DTSTART;VALUE=DATE:20270405" in output
        assert "DTEND;VALUE=DATE:20270410" in output

    def test_escapes_commas_in_titles(self):
        session = self.session().model_copy(update={"title": "Art, Clay & Fire"})
        assert "Art\\, Clay & Fire" in render_calendar([session])

    def test_uses_crlf_line_endings(self):
        assert "\r\n" in render_calendar([self.session()])

    def test_empty_input_still_produces_valid_output(self):
        output = render_calendar([])
        assert "BEGIN:VCALENDAR" in output and "BEGIN:VEVENT" not in output
