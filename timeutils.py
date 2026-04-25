"""
timeutils.py — Tiện ích thời gian theo múi giờ Hà Nội (GMT+7).

Dùng datetime.timezone với offset cố định, không phụ thuộc tzdata.
"""
from datetime import datetime, timezone, timedelta
from config import TIMEZONE_OFFSET_HOURS

VIETNAM_TZ = timezone(timedelta(hours=TIMEZONE_OFFSET_HOURS))


def now_local() -> datetime:
    """Lấy thời gian hiện tại ở múi giờ Hà Nội (timezone-aware)."""
    return datetime.now(VIETNAM_TZ)


def today_str() -> str:
    """Ngày hôm nay theo VN, format YYYY-MM-DD."""
    return now_local().strftime("%Y-%m-%d")


def time_str() -> str:
    """Giờ phút hiện tại theo VN, format HH:MM."""
    return now_local().strftime("%H:%M")


def filename_timestamp() -> str:
    """Timestamp dùng cho tên file: YYYY-MM-DD_HHMM."""
    return now_local().strftime("%Y-%m-%d_%H%M")


def datetime_str() -> str:
    """Datetime đầy đủ cho frontmatter: YYYY-MM-DD HH:MM."""
    return now_local().strftime("%Y-%m-%d %H:%M")


def daily_journal_filename(date: str = None) -> str:
    """Tên file nhật ký theo ngày."""
    if not date:
        date = today_str()
    return f"{date}_NhatKy.md"
