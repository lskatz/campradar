"""Smoke tests for the data model.

Not comprehensive by design — these cover the invariants that, if broken,
would silently corrupt everything downstream: identity stability, date
validation, and the permissive-age rule.
"""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from campradar.models import CampSession, RegistrationStatus, slugify


def make_session(**overrides) -> CampSession:
    """Build a valid session, overriding whichever fields a test cares about."""
    defaults = dict(
        provider_slug="fernbank-science-center",
        title="Junior Naturalists",
        start_date=date(2027, 4, 5),
        end_date=date(2027, 4, 9),
        min_age=6,
        max_age=10,
        source_id="test",
    )
    return CampSession(**{**defaults, **overrides})


class TestIdentity:
    def test_key_is_stable_across_identical_sessions(self):
        assert make_session().key == make_session().key

    def test_key_ignores_price_and_status_changes(self):
        """A price tweak must not look like a brand-new camp."""
        cheap = make_session(price_usd=200.0)
        dear = make_session(price_usd=250.0, registration_status=RegistrationStatus.FULL)
        assert cheap.key == dear.key

    def test_key_survives_marketing_noise_in_title(self):
        """'Junior Naturalists' and 'Junior Naturalists Summer Camp' are one camp."""
        plain = make_session(title="Junior Naturalists")
        padded = make_session(title="Junior Naturalists Summer Camp")
        assert plain.key == padded.key

    def test_key_differs_for_different_start_dates(self):
        week_one = make_session(start_date=date(2027, 4, 5), end_date=date(2027, 4, 9))
        week_two = make_session(start_date=date(2027, 4, 12), end_date=date(2027, 4, 16))
        assert week_one.key != week_two.key


class TestValidation:
    def test_rejects_backwards_date_range(self):
        with pytest.raises(ValidationError):
            make_session(start_date=date(2027, 4, 9), end_date=date(2027, 4, 5))

    def test_rejects_backwards_age_range(self):
        with pytest.raises(ValidationError):
            make_session(min_age=12, max_age=6)


class TestEligibility:
    def test_age_inside_range_is_eligible(self):
        assert make_session().suits_age(8) is True

    def test_age_outside_range_is_not(self):
        assert make_session().suits_age(14) is False

    def test_unstated_ages_are_permissive(self):
        """Recall beats precision: never hide a camp because ages weren't stated."""
        session = make_session(min_age=None, max_age=None)
        assert session.suits_age(4) is True
        assert session.suits_age(17) is True


class TestCoverage:
    def test_duration_counts_inclusively(self):
        assert make_session().duration_days == 5

    def test_single_day_camp_is_one_day(self):
        single = make_session(start_date=date(2027, 4, 5), end_date=date(2027, 4, 5))
        assert single.duration_days == 1

    def test_covers_last_day(self):
        """Off-by-one here would leave a family without childcare on a Friday."""
        assert make_session().covers(date(2027, 4, 9)) is True
        assert make_session().covers(date(2027, 4, 10)) is False


def test_slugify_handles_accents_and_punctuation():
    assert slugify("Café  Möller & Sons!") == "cafe-moller-sons"
