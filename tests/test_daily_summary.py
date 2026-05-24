"""Tests for FR-7 sub-task 7.5b — daily summary commands and scheduled jobs.

Groups:
  1. _cmd_tom_tat_hom_nay — on-demand daily summary Telegram command
  2. _cmd_cau_hinh_tong_ket — configure / disable daily summary time
  3. _cmd_cau_hinh_gio_mac_dinh — configure morning default time
  4. scan_reminders job — wrapper that calls reminder_engine.tick()
  5. send_daily_summary job — per-user scheduled summary delivery
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from audit import SqliteAuditLog
from deps import CoreDeps
from task_store import SqliteTaskStore
from user_store import SqliteUserStore

VN_TZ = timezone(timedelta(hours=7))
_TODAY = datetime.now(VN_TZ).strftime("%Y-%m-%d")
TODAY_DL = f"{_TODAY}T15:00:00+07:00"   # pending task due today at 15:00
FUTURE_DL = "2099-01-01T09:00:00+07:00"


def _run(coro):
    return asyncio.run(coro)


def _now_at(hour: int, minute: int = 0) -> datetime:
    """Return a tz-aware datetime at the given hour:minute today in VN_TZ."""
    d = datetime.now(VN_TZ).date()
    return datetime(d.year, d.month, d.day, hour, minute, tzinfo=VN_TZ)


# ── Lazy imports (symbols don't exist yet → RED) ──────────────────────────────


def _import_cmd_tom_tat():
    from core_handler import _cmd_tom_tat_hom_nay  # noqa: PLC0415
    return _cmd_tom_tat_hom_nay


def _import_cmd_cau_hinh_tong_ket():
    from core_handler import _cmd_cau_hinh_tong_ket  # noqa: PLC0415
    return _cmd_cau_hinh_tong_ket


def _import_cmd_cau_hinh_gio_mac_dinh():
    from core_handler import _cmd_cau_hinh_gio_mac_dinh  # noqa: PLC0415
    return _cmd_cau_hinh_gio_mac_dinh


def _import_scan_reminders():
    from scheduled_jobs import scan_reminders  # noqa: PLC0415
    return scan_reminders


def _import_send_daily_summary():
    from scheduled_jobs import send_daily_summary  # noqa: PLC0415
    return send_daily_summary


# ── Fake helpers ──────────────────────────────────────────────────────────────


class FakeChannel:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send(self, chat_id, text, use_markdown=True):
        self.sent.append((chat_id, text))

    async def send_with_inline_keyboard(self, chat_id, text, buttons, use_markdown=False):
        self.sent.append((chat_id, text))

    async def delete_message(self, chat_id, message_id):
        return True

    @property
    def last_text(self) -> str:
        return self.sent[-1][1] if self.sent else ""


class FakeUser:
    def __init__(self, user_id: int) -> None:
        self.id = user_id
        self.role = "member"


def _make_deps(
    db_conn,
    user_store,
    *,
    task_store=None,
    notification_service=None,
    reminder_engine=None,
) -> CoreDeps:
    audit = SqliteAuditLog(conn=db_conn)
    return CoreDeps(
        llm=None,  # type: ignore[arg-type]
        notes=None,  # type: ignore[arg-type]
        wiki=None,  # type: ignore[arg-type]
        channel=FakeChannel(),
        user_store=user_store,
        note_index=None,  # type: ignore[arg-type]
        memory_store=None,  # type: ignore[arg-type]
        elevation_store=None,  # type: ignore[arg-type]
        audit=audit,
        task_store=task_store,
        notification_service=notification_service,
        reminder_engine=reminder_engine,
    )


# ── Group 1: _cmd_tom_tat_hom_nay ────────────────────────────────────────────


class TestCmdTomTatHomNay:
    def test_shows_completed_today_in_reply(self, db_conn):
        _cmd = _import_cmd_tom_tat()
        us = SqliteUserStore(conn=db_conn)
        u = us.create_user(name="T", role="member")
        ts = SqliteTaskStore(conn=db_conn)
        task = ts.create_task(user_id=u.id, title="Done today", deadline=TODAY_DL)
        ts.complete_task(task["id"], completed_at=f"{_TODAY} 10:00:00")
        deps = _make_deps(db_conn, us, task_store=ts)
        _run(_cmd("c1", FakeUser(u.id), deps))
        # Reply must mention completed count (at least "1" or "xong")
        text = deps.channel.last_text.lower()
        assert "xong" in text or "hoàn thành" in text or "1" in text

    def test_shows_pending_today_in_reply(self, db_conn):
        _cmd = _import_cmd_tom_tat()
        us = SqliteUserStore(conn=db_conn)
        u = us.create_user(name="T", role="member")
        ts = SqliteTaskStore(conn=db_conn)
        ts.create_task(user_id=u.id, title="Pending today", deadline=TODAY_DL)
        deps = _make_deps(db_conn, us, task_store=ts)
        _run(_cmd("c1", FakeUser(u.id), deps))
        text = deps.channel.last_text.lower()
        assert "còn" in text or "pending" in text or "1" in text

    def test_no_tasks_sends_non_empty_reply(self, db_conn):
        _cmd = _import_cmd_tom_tat()
        us = SqliteUserStore(conn=db_conn)
        u = us.create_user(name="T", role="member")
        ts = SqliteTaskStore(conn=db_conn)
        deps = _make_deps(db_conn, us, task_store=ts)
        _run(_cmd("c1", FakeUser(u.id), deps))
        assert deps.channel.last_text != ""

    def test_other_users_tasks_not_included(self, db_conn):
        _cmd = _import_cmd_tom_tat()
        us = SqliteUserStore(conn=db_conn)
        owner = us.create_user(name="Owner", role="member")
        viewer = us.create_user(name="Viewer", role="member")
        ts = SqliteTaskStore(conn=db_conn)
        # owner has tasks today; viewer has none
        ts.create_task(user_id=owner.id, title="Owner task", deadline=TODAY_DL)
        deps = _make_deps(db_conn, us, task_store=ts)
        _run(_cmd("c1", FakeUser(viewer.id), deps))
        # viewer's summary should show 0 tasks, not owner's task title
        assert "Owner task" not in deps.channel.last_text


# ── Group 2: _cmd_cau_hinh_tong_ket ──────────────────────────────────────────


class TestCmdCauHinhTongKet:
    def test_set_valid_time_saves_to_store(self, db_conn):
        _cmd = _import_cmd_cau_hinh_tong_ket()
        us = SqliteUserStore(conn=db_conn)
        u = us.create_user(name="T", role="member")
        deps = _make_deps(db_conn, us)
        _run(_cmd("c1", "20:00", FakeUser(u.id), deps))
        assert us.get_daily_summary_time(u.id) == "20:00"

    def test_set_tat_disables_summary(self, db_conn):
        _cmd = _import_cmd_cau_hinh_tong_ket()
        us = SqliteUserStore(conn=db_conn)
        u = us.create_user(name="T", role="member")
        deps = _make_deps(db_conn, us)
        _run(_cmd("c1", "tắt", FakeUser(u.id), deps))
        assert us.get_daily_summary_time(u.id) == "off"

    def test_invalid_format_shows_error_and_does_not_save(self, db_conn):
        _cmd = _import_cmd_cau_hinh_tong_ket()
        us = SqliteUserStore(conn=db_conn)
        u = us.create_user(name="T", role="member")
        deps = _make_deps(db_conn, us)
        _run(_cmd("c1", "abc", FakeUser(u.id), deps))
        assert deps.channel.last_text != ""
        assert us.get_daily_summary_time(u.id) is None  # unchanged

    def test_invalid_hours_shows_error_and_does_not_save(self, db_conn):
        _cmd = _import_cmd_cau_hinh_tong_ket()
        us = SqliteUserStore(conn=db_conn)
        u = us.create_user(name="T", role="member")
        deps = _make_deps(db_conn, us)
        _run(_cmd("c1", "25:00", FakeUser(u.id), deps))
        assert deps.channel.last_text != ""
        assert us.get_daily_summary_time(u.id) is None  # unchanged


# ── Group 3: _cmd_cau_hinh_gio_mac_dinh ──────────────────────────────────────


class TestCmdCauHinhGioMacDinh:
    def test_set_valid_morning_time_saves_to_store(self, db_conn):
        _cmd = _import_cmd_cau_hinh_gio_mac_dinh()
        us = SqliteUserStore(conn=db_conn)
        u = us.create_user(name="T", role="member")
        deps = _make_deps(db_conn, us)
        _run(_cmd("c1", "08:30", FakeUser(u.id), deps))
        assert us.get_morning_default_time(u.id) == "08:30"

    def test_invalid_format_shows_error_and_does_not_save(self, db_conn):
        _cmd = _import_cmd_cau_hinh_gio_mac_dinh()
        us = SqliteUserStore(conn=db_conn)
        u = us.create_user(name="T", role="member")
        deps = _make_deps(db_conn, us)
        _run(_cmd("c1", "9h", FakeUser(u.id), deps))
        assert deps.channel.last_text != ""
        assert us.get_morning_default_time(u.id) is None  # unchanged


# ── Group 4: scan_reminders ───────────────────────────────────────────────────


class TestScanRemindersJob:
    def test_calls_reminder_engine_tick(self, db_conn):
        scan_reminders = _import_scan_reminders()
        us = SqliteUserStore(conn=db_conn)
        engine = MagicMock()
        engine.tick.return_value = {"fired": 2, "missed": 0, "recurring_expanded": 1}
        deps = _make_deps(db_conn, us, reminder_engine=engine)
        scan_reminders(deps)
        engine.tick.assert_called_once()

    def test_returns_empty_dict_if_no_engine(self, db_conn):
        scan_reminders = _import_scan_reminders()
        us = SqliteUserStore(conn=db_conn)
        deps = _make_deps(db_conn, us)  # no reminder_engine
        result = scan_reminders(deps)
        assert result == {}


# ── Group 5: send_daily_summary ───────────────────────────────────────────────


class TestSendDailySummaryJob:
    def test_sends_to_user_matching_configured_time(self, db_conn):
        send_daily_summary = _import_send_daily_summary()
        us = SqliteUserStore(conn=db_conn)
        u = us.create_user(name="T", role="member")
        us.set_daily_summary_time(u.id, "21:00")
        ts = SqliteTaskStore(conn=db_conn)
        ts.create_task(user_id=u.id, title="Task A", deadline=TODAY_DL)
        notif = MagicMock()
        deps = _make_deps(db_conn, us, task_store=ts, notification_service=notif)
        send_daily_summary(deps, now=_now_at(21, 0))
        notif.enqueue.assert_called_once()

    def test_null_daily_summary_time_uses_default_21_00(self, db_conn):
        send_daily_summary = _import_send_daily_summary()
        us = SqliteUserStore(conn=db_conn)
        u = us.create_user(name="T", role="member")
        # daily_summary_time is NULL → system default 21:00
        ts = SqliteTaskStore(conn=db_conn)
        ts.create_task(user_id=u.id, title="Task A", deadline=TODAY_DL)
        notif = MagicMock()
        deps = _make_deps(db_conn, us, task_store=ts, notification_service=notif)
        send_daily_summary(deps, now=_now_at(21, 0))
        notif.enqueue.assert_called_once()

    def test_off_skips_user(self, db_conn):
        send_daily_summary = _import_send_daily_summary()
        us = SqliteUserStore(conn=db_conn)
        u = us.create_user(name="T", role="member")
        us.set_daily_summary_time(u.id, "off")
        ts = SqliteTaskStore(conn=db_conn)
        ts.create_task(user_id=u.id, title="Task A", deadline=TODAY_DL)
        notif = MagicMock()
        deps = _make_deps(db_conn, us, task_store=ts, notification_service=notif)
        send_daily_summary(deps, now=_now_at(21, 0))
        notif.enqueue.assert_not_called()

    def test_skips_if_no_tasks(self, db_conn):
        send_daily_summary = _import_send_daily_summary()
        us = SqliteUserStore(conn=db_conn)
        u = us.create_user(name="T", role="member")
        us.set_daily_summary_time(u.id, "21:00")
        ts = SqliteTaskStore(conn=db_conn)  # no tasks created
        notif = MagicMock()
        deps = _make_deps(db_conn, us, task_store=ts, notification_service=notif)
        send_daily_summary(deps, now=_now_at(21, 0))
        notif.enqueue.assert_not_called()
