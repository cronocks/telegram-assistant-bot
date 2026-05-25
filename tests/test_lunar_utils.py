"""Tests for lunar_utils — FR-8."""
from datetime import date

import pytest

from lunar_utils import compute_anniversary_solar_date, day_of_week_vn, lunar_to_solar


# ── lunar_to_solar — known Vietnamese Tet anchors ─────────────────────────────


def test_lunar_to_solar_tet_2024():
    # Lunar New Year 2024 fell on solar 2024-02-10.
    assert lunar_to_solar(2024, 1, 1) == date(2024, 2, 10)


def test_lunar_to_solar_tet_2025():
    # Lunar New Year 2025 fell on solar 2025-01-29.
    assert lunar_to_solar(2025, 1, 1) == date(2025, 1, 29)


def test_lunar_to_solar_mid_year():
    # Lunar 3/10/2024 = solar 2024-04-18 (verified via lunardate).
    assert lunar_to_solar(2024, 3, 10) == date(2024, 4, 18)


# ── compute_anniversary_solar_date — solar passthrough ────────────────────────


def test_solar_anniversary_returns_same_year_date():
    assert compute_anniversary_solar_date("solar", 8, 15, 2025) == date(2025, 8, 15)


def test_solar_feb_29_in_non_leap_year_falls_back_to_28():
    # Stored as Feb 29 (only possible in leap year); when recomputed for a
    # non-leap year, must fall back gracefully to Feb 28.
    assert compute_anniversary_solar_date("solar", 2, 29, 2025) == date(2025, 2, 28)


def test_solar_feb_29_in_leap_year_returns_29():
    assert compute_anniversary_solar_date("solar", 2, 29, 2024) == date(2024, 2, 29)


# ── compute_anniversary_solar_date — lunar conversion ─────────────────────────


def test_lunar_anniversary_returns_solar_for_given_year():
    # Lunar 1/1 in solar year 2024 = 2024-02-10
    assert compute_anniversary_solar_date("lunar", 1, 1, 2024) == date(2024, 2, 10)


def test_lunar_anniversary_recomputes_per_year():
    d1 = compute_anniversary_solar_date("lunar", 1, 1, 2024)
    d2 = compute_anniversary_solar_date("lunar", 1, 1, 2025)
    # Different solar dates each year — confirms recompute is not cached.
    assert d1 != d2


# ── error handling ────────────────────────────────────────────────────────────


def test_invalid_date_type_raises():
    with pytest.raises(ValueError):
        compute_anniversary_solar_date("hebrew", 1, 1, 2025)


def test_lunar_day_30_in_short_month_falls_back():
    # Not every lunar month has 30 days. When user stored day=30 but the lunar
    # month has only 29 days in a given year, must fall back to day 29.
    # Lunar 9/2024 has only 29 days — verify graceful fallback.
    result = compute_anniversary_solar_date("lunar", 9, 30, 2024)
    # Should return some valid date (the 29th of that lunar month).
    expected = lunar_to_solar(2024, 9, 29)
    assert result == expected


# ── day_of_week_vn ────────────────────────────────────────────────────────────


def test_day_of_week_vn_wednesday():
    # 2025-01-01 is a Wednesday.
    assert day_of_week_vn(date(2025, 1, 1)) == "Thứ 4"


def test_day_of_week_vn_sunday():
    # 2025-01-05 is a Sunday.
    assert day_of_week_vn(date(2025, 1, 5)) == "Chủ nhật"


def test_day_of_week_vn_all_days():
    # 2025-01-06 Mon through 2025-01-12 Sun covers all 7 days.
    expected = ["Thứ 2", "Thứ 3", "Thứ 4", "Thứ 5", "Thứ 6", "Thứ 7", "Chủ nhật"]
    for i, name in enumerate(expected):
        assert day_of_week_vn(date(2025, 1, 6 + i)) == name


# ── compute_anniversary_solar_date — is_leap_month ────────────────────────────


def test_leap_month_returns_different_date_than_regular():
    # 2023 has lunar leap month 2. Regular and leap month 2 map to different
    # solar dates.
    d_regular = compute_anniversary_solar_date("lunar", 2, 15, 2023, is_leap_month=False)
    d_leap = compute_anniversary_solar_date("lunar", 2, 15, 2023, is_leap_month=True)
    assert d_regular != d_leap


def test_leap_month_fallback_when_year_has_no_leap():
    # 2025 has no lunar leap month 2, so is_leap_month=True falls back to regular.
    d_regular = compute_anniversary_solar_date("lunar", 2, 15, 2025, is_leap_month=False)
    d_leap = compute_anniversary_solar_date("lunar", 2, 15, 2025, is_leap_month=True)
    assert d_regular == d_leap


def test_solar_type_ignores_is_leap_month():
    # is_leap_month flag has no effect for solar anniversaries.
    d1 = compute_anniversary_solar_date("solar", 8, 15, 2025, is_leap_month=False)
    d2 = compute_anniversary_solar_date("solar", 8, 15, 2025, is_leap_month=True)
    assert d1 == d2 == date(2025, 8, 15)
