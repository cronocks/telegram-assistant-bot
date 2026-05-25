"""lunar_utils.py — Vietnamese lunar↔solar calendar conversion.

Thin wrapper around `lunardate` (Vietnamese lunar = Chinese lunar — same system).
Used by AnniversaryEngine to compute the solar date for a stored anniversary in
any given year.

Anniversaries are stored with the *original* lunar month/day; this module is
called every year (via AnniversaryEngine.compute_year) to map that lunar date
onto the solar calendar of the target year — Decision #47.
"""
from __future__ import annotations

from datetime import date

from lunardate import LunarDate

VALID_DATE_TYPES = {"lunar", "solar"}

_DAY_NAMES_VN = {0: "Thứ 2", 1: "Thứ 3", 2: "Thứ 4", 3: "Thứ 5", 4: "Thứ 6", 5: "Thứ 7", 6: "Chủ nhật"}


def day_of_week_vn(d: date) -> str:
    """Return Vietnamese day-of-week string for a date (e.g. 'Thứ 4', 'Chủ nhật')."""
    return _DAY_NAMES_VN[d.weekday()]


def lunar_to_solar(year: int, month: int, day: int) -> date:
    """Convert a lunar (year, month, day) to its solar date.

    Raises ValueError if the lunar date is invalid in that year (e.g. day=30
    in a month that only has 29 days).
    """
    return LunarDate(year, month, day).toSolarDate()


def compute_anniversary_solar_date(
    date_type: str,
    month: int,
    day: int,
    year: int,
    *,
    is_leap_month: bool = False,
) -> date:
    """Compute the solar date for a stored anniversary in a given solar year.

    For 'solar' anniversaries: returns date(year, month, day). Feb 29 in a
    non-leap year falls back to Feb 28.

    For 'lunar' anniversaries: converts (year, month, day) lunar → solar.
    If the stored day exceeds the actual length of that lunar month in the
    target year, falls back to the last day of that month (day 29).
    """
    if date_type not in VALID_DATE_TYPES:
        raise ValueError(f"lunar_utils: date_type must be lunar|solar, got {date_type}")

    if date_type == "solar":
        try:
            return date(year, month, day)
        except ValueError:
            # Feb 29 in non-leap year, etc. — fall back one day.
            return date(year, month, day - 1)

    # Lunar: honour is_leap_month when the target year has that leap month;
    # fall back to the regular occurrence if not (standard Vietnamese practice).
    if is_leap_month:
        try:
            return LunarDate(year, month, day, isLeapMonth=True).toSolarDate()
        except ValueError:
            pass  # year has no such leap month — fall through to regular month

    # Regular month (or day-30 fallback when the month has only 29 days).
    try:
        return LunarDate(year, month, day).toSolarDate()
    except ValueError:
        return LunarDate(year, month, day - 1).toSolarDate()
