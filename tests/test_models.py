"""Model boundaries: the identity rules and the validators."""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from campradar.models import slugify, title_fingerprint
from conftest import make_session


def test_key_survives_marketing_rewrites():
    """The same week retitled between seasons must keep its identity."""
    a = make_session("Camp Kingfisher Summer Camp 2027")
    b = make_session("Kingfisher Camp 2027")
    assert a.key == b.key


def test_key_distinguishes_different_dates():
    a = make_session("Discovery", start=date(2026, 10, 5))
    b = make_session("Discovery", start=date(2027, 2, 15))
    assert a.key != b.key


def test_key_ignores_price_and_status():
    """A price tweak is not a new camp."""
    from campradar.models import RegistrationStatus

    a = make_session("Discovery", price_usd=100)
    b = make_session("Discovery", price_usd=350, status=RegistrationStatus.FULL)
    assert a.key == b.key


def test_key_preserves_word_order():
    """Robotics for Girls and Girls for Robotics may be different programmes."""
    assert make_session("Robotics for Girls").key != make_session("Girls for Robotics").key


def test_fingerprint_of_pure_noise_still_produces_something():
    assert title_fingerprint("Summer Camp") == "summer-camp"


def test_slugify_strips_accents_and_punctuation():
    assert slugify("Café Créatif — Ages 5+") == "cafe-creatif-ages-5"


def test_one_day_camp_lasts_one_day():
    assert make_session("Teacher Workday", start=date(2026, 11, 3)).duration_days == 1


def test_covers_is_inclusive():
    session = make_session("Week", start=date(2026, 10, 5), end=date(2026, 10, 9))
    assert session.covers(date(2026, 10, 5))
    assert session.covers(date(2026, 10, 9))
    assert not session.covers(date(2026, 10, 10))


def test_backwards_dates_are_rejected():
    with pytest.raises(ValidationError, match="precedes"):
        make_session("Impossible", start=date(2026, 10, 9), end=date(2026, 10, 5))


def test_inverted_age_range_is_rejected():
    with pytest.raises(ValidationError, match="below min_age"):
        make_session("Impossible", min_age=12, max_age=6)


def test_unstated_ages_are_allowed():
    """Not stated is the common case and must not be an error."""
    session = make_session("Vague")
    assert session.min_age is None
    assert session.max_age is None
