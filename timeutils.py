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


def current_week_start() -> datetime:
    """
    Trả về datetime của thứ 2 đầu tuần này (00:00:00 GMT+7).
    weekday(): Monday=0, ..., Sunday=6.
    """
    n = now_local()
    monday = n - timedelta(days=n.weekday())
    return monday.replace(hour=0, minute=0, second=0, microsecond=0)


def current_week_end() -> datetime:
    """Trả về datetime của chủ nhật cuối tuần này (23:59:59 GMT+7)."""
    sunday = current_week_start() + timedelta(days=6)
    return sunday.replace(hour=23, minute=59, second=59, microsecond=999999)


def current_week_range_str() -> str:
    """Chuỗi hiển thị range tuần này, dạng '20/04 - 26/04'."""
    start = current_week_start()
    end = current_week_start() + timedelta(days=6)
    return f"{start.strftime('%d/%m')} - {end.strftime('%d/%m')}"
