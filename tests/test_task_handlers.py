"""Tests for FR-7 sub-task 7.4 — Telegram task command handlers.

Exercises _cmd_tao_task, _cmd_xong_task, _cmd_huy_task, _cmd_danh_sach_task,
_cmd_xem_task, _cmd_lich_hoc, _cmd_hoan_task with:
  - FakeChannel that captures sent messages.
  - Real stores (SqliteTaskStore, SqliteReminderStore) wired to in-memory SQLite.
  - Mock task_parser and reminder_engine for LLM-heavy handlers.
"""
from __future__ import annotations

import asyncio
from datetime import timedelta, timezone
from unittest.mock import MagicMock

import pytest

from audit import SqliteAuditLog
from cmd_task import (
    _cmd_danh_sach_task,
    _cmd_hoan_task,
    _cmd_huy_task,
    _cmd_lich_hoc,
    _cmd_tao_task,
    _cmd_xem_task,
    _cmd_xong_task,
    _parse_task_id,
)
from deps import CoreDeps
from reminder_engine import ReminderEngine
from reminder_store import SqliteReminderStore
from task_parser import ParseError, ParsedTask
from task_store import SqliteTaskStore
from user_store import SqliteUserStore

VN_TZ = timezone(timedelta(hours=7))
FUTURE_DL = "2099-01-01T09:00:00+07:00"


# ── Fake helpers ──────────────────────────────────────────────────────────────


class FakeChannel:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.inline_sent: list[tuple[str, str, list]] = []
        self.answered_callbacks: list[str] = []

    async def send(self, chat_id, text, use_markdown=True):
        self.sent.append((chat_id, text))

    async def send_with_inline_keyboard(self, chat_id, text, buttons, use_markdown=False):
        self.inline_sent.append((chat_id, text, buttons))
        self.sent.append((chat_id, text))

    async def answer_callback_query(self, callback_query_id, text=""):
        self.answered_callbacks.append(callback_query_id)

    async def delete_message(self, chat_id, message_id):
        return True

    @property
    def last_text(self) -> str:
        return self.sent[-1][1] if self.sent else ""


class FakeUser:
    """Minimal user stub; id must match an existing row in the users table."""
    def __init__(self, user_id: int) -> None:
        self.id = user_id
        self.role = "member"


def _make_parser(parsed: ParsedTask | None = None, exc: Exception | None = None) -> MagicMock:
    """Return a mock task_parser. If exc is given, parse() raises it; else returns parsed."""
    parser = MagicMock()
    if exc is not None:
        parser.parse.side_effect = exc
    else:
        parser.parse.return_value = parsed
    return parser


def _make_deps(
    db_conn,
    user_store,
    *,
    task_store=None,
    reminder_store=None,
    reminder_engine=None,
    task_parser=None,
) -> CoreDeps:
    """Build a minimal CoreDeps with task-related stores wired."""
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
        reminder_store=reminder_store,
        reminder_engine=reminder_engine,
        task_parser=task_parser,
    )


def _run(coro):
    return asyncio.run(coro)


# ── _parse_task_id ────────────────────────────────────────────────────────────


class TestParseTaskId:
    def test_valid_integer(self):
        assert _parse_task_id("5") == 5

    def test_valid_integer_with_trailing(self):
        assert _parse_task_id("12 extra") == 12

    def test_empty_returns_none(self):
        assert _parse_task_id("") is None

    def test_non_numeric_returns_none(self):
        assert _parse_task_id("abc") is None

    def test_strips_whitespace(self):
        assert _parse_task_id("  7 ") == 7


# ── _cmd_tao_task ─────────────────────────────────────────────────────────────


class TestCmdTaoTask:
    @pytest.fixture()
    def setup(self, db_conn):
        us = SqliteUserStore(conn=db_conn)
        u = us.create_user(name="Test", role="member")
        user = FakeUser(u.id)
        ts = SqliteTaskStore(conn=db_conn)
        parsed = ParsedTask(
            title="Mua sữa",
            deadline_iso=FUTURE_DL,
            recurring_rule=None,
            category="task",
        )
        parser = _make_parser(parsed)
        deps = _make_deps(db_conn, us, task_store=ts, task_parser=parser)
        return deps, user

    def test_creates_task_and_replies(self, setup):
        deps, user = setup
        _run(_cmd_tao_task("c1", "mua sua 5h chieu mai", user, deps))
        assert "Mua sữa" in deps.channel.last_text

    def test_reply_contains_task_id(self, setup):
        deps, user = setup
        _run(_cmd_tao_task("c1", "mua sua 5h chieu mai", user, deps))
        assert "#1" in deps.channel.last_text

    def test_no_task_parser_replies_not_activated(self, db_conn):
        us = SqliteUserStore(conn=db_conn)
        u = us.create_user(name="T", role="member")
        user = FakeUser(u.id)
        deps = _make_deps(db_conn, us)  # no task_parser
        _run(_cmd_tao_task("c1", "something", user, deps))
        assert "chưa được kích hoạt" in deps.channel.last_text

    def test_empty_body_sends_hint(self, db_conn):
        us = SqliteUserStore(conn=db_conn)
        u = us.create_user(name="T", role="member")
        user = FakeUser(u.id)
        ts = SqliteTaskStore(conn=db_conn)
        parser = _make_parser(ParsedTask("T", FUTURE_DL, None, "task"))
        deps = _make_deps(db_conn, us, task_store=ts, task_parser=parser)
        _run(_cmd_tao_task("c1", "", user, deps))
        assert "task:" in deps.channel.last_text.lower() or "mô tả" in deps.channel.last_text

    def test_parse_error_forwarded_to_user(self, db_conn):
        us = SqliteUserStore(conn=db_conn)
        u = us.create_user(name="T", role="member")
        user = FakeUser(u.id)
        ts = SqliteTaskStore(conn=db_conn)
        parser = _make_parser(exc=ParseError("past_deadline", "Deadline đã qua."))
        deps = _make_deps(db_conn, us, task_store=ts, task_parser=parser)
        _run(_cmd_tao_task("c1", "something yesterday", user, deps))
        assert "Deadline đã qua" in deps.channel.last_text

    def test_recurring_rule_shown_in_reply(self, db_conn):
        us = SqliteUserStore(conn=db_conn)
        u = us.create_user(name="T", role="member")
        user = FakeUser(u.id)
        ts = SqliteTaskStore(conn=db_conn)
        parsed = ParsedTask("Học tiếng Anh", FUTURE_DL, "weekly:MON@07:00", "study")
        parser = _make_parser(parsed)
        deps = _make_deps(db_conn, us, task_store=ts, task_parser=parser)
        _run(_cmd_tao_task("c1", "hoc tieng anh thu 2 luc 7h", user, deps))
        assert "weekly:MON@07:00" in deps.channel.last_text

    def test_reminder_engine_scheduled(self, db_conn):
        us = SqliteUserStore(conn=db_conn)
        u = us.create_user(name="T", role="member")
        user = FakeUser(u.id)
        ts = SqliteTaskStore(conn=db_conn)
        parsed = ParsedTask("Mua sữa", FUTURE_DL, None, "task")
        parser = _make_parser(parsed)
        engine = MagicMock()
        deps = _make_deps(db_conn, us, task_store=ts, task_parser=parser, reminder_engine=engine)
        _run(_cmd_tao_task("c1", "mua sua", user, deps))
        engine.schedule_for_task.assert_called_once()


# ── _cmd_xong_task ────────────────────────────────────────────────────────────


class TestCmdXongTask:
    def test_completes_task(self, db_conn):
        us = SqliteUserStore(conn=db_conn)
        u = us.create_user(name="T", role="member")
        user = FakeUser(u.id)
        ts = SqliteTaskStore(conn=db_conn)
        task = ts.create_task(user_id=u.id, title="Do it", deadline=FUTURE_DL)
        deps = _make_deps(db_conn, us, task_store=ts)
        _run(_cmd_xong_task("c1", str(task["id"]), user, deps))
        updated = ts.get_task(task["id"])
        assert updated["status"] == "completed"

    def test_reply_contains_title(self, db_conn):
        us = SqliteUserStore(conn=db_conn)
        u = us.create_user(name="T", role="member")
        user = FakeUser(u.id)
        ts = SqliteTaskStore(conn=db_conn)
        task = ts.create_task(user_id=u.id, title="Do it", deadline=FUTURE_DL)
        deps = _make_deps(db_conn, us, task_store=ts)
        _run(_cmd_xong_task("c1", str(task["id"]), user, deps))
        assert "Do it" in deps.channel.last_text

    def test_wrong_user_cannot_complete(self, db_conn):
        us = SqliteUserStore(conn=db_conn)
        owner = us.create_user(name="Owner", role="member")
        requester = us.create_user(name="Other", role="member")
        user = FakeUser(requester.id)
        ts = SqliteTaskStore(conn=db_conn)
        task = ts.create_task(user_id=owner.id, title="Owned", deadline=FUTURE_DL)
        deps = _make_deps(db_conn, us, task_store=ts)
        _run(_cmd_xong_task("c1", str(task["id"]), user, deps))
        assert "Không tìm thấy" in deps.channel.last_text

    def test_invalid_id_shows_syntax(self, db_conn):
        us = SqliteUserStore(conn=db_conn)
        u = us.create_user(name="T", role="member")
        user = FakeUser(u.id)
        ts = SqliteTaskStore(conn=db_conn)
        deps = _make_deps(db_conn, us, task_store=ts)
        _run(_cmd_xong_task("c1", "abc", user, deps))
        assert "Cú pháp" in deps.channel.last_text

    def test_cancels_reminders_via_engine(self, db_conn):
        us = SqliteUserStore(conn=db_conn)
        u = us.create_user(name="T", role="member")
        user = FakeUser(u.id)
        ts = SqliteTaskStore(conn=db_conn)
        task = ts.create_task(user_id=u.id, title="Do it", deadline=FUTURE_DL)
        engine = MagicMock()
        deps = _make_deps(db_conn, us, task_store=ts, reminder_engine=engine)
        _run(_cmd_xong_task("c1", str(task["id"]), user, deps))
        engine.cancel_all_for_task.assert_called_once_with(task["id"])


# ── _cmd_huy_task ─────────────────────────────────────────────────────────────


class TestCmdHuyTask:
    def test_cancels_task(self, db_conn):
        us = SqliteUserStore(conn=db_conn)
        u = us.create_user(name="T", role="member")
        user = FakeUser(u.id)
        ts = SqliteTaskStore(conn=db_conn)
        task = ts.create_task(user_id=u.id, title="Cancel me", deadline=FUTURE_DL)
        deps = _make_deps(db_conn, us, task_store=ts)
        _run(_cmd_huy_task("c1", str(task["id"]), user, deps))
        updated = ts.get_task(task["id"])
        assert updated["status"] == "cancelled"

    def test_reply_contains_title(self, db_conn):
        us = SqliteUserStore(conn=db_conn)
        u = us.create_user(name="T", role="member")
        user = FakeUser(u.id)
        ts = SqliteTaskStore(conn=db_conn)
        task = ts.create_task(user_id=u.id, title="Cancel me", deadline=FUTURE_DL)
        deps = _make_deps(db_conn, us, task_store=ts)
        _run(_cmd_huy_task("c1", str(task["id"]), user, deps))
        assert "Cancel me" in deps.channel.last_text

    def test_wrong_user_denied(self, db_conn):
        us = SqliteUserStore(conn=db_conn)
        owner = us.create_user(name="Owner", role="member")
        requester = us.create_user(name="Other", role="member")
        user = FakeUser(requester.id)
        ts = SqliteTaskStore(conn=db_conn)
        task = ts.create_task(user_id=owner.id, title="Owned", deadline=FUTURE_DL)
        deps = _make_deps(db_conn, us, task_store=ts)
        _run(_cmd_huy_task("c1", str(task["id"]), user, deps))
        assert "Không tìm thấy" in deps.channel.last_text


# ── _cmd_danh_sach_task ───────────────────────────────────────────────────────


class TestCmdDanhSachTask:
    def test_shows_pending_tasks(self, db_conn):
        us = SqliteUserStore(conn=db_conn)
        u = us.create_user(name="T", role="member")
        user = FakeUser(u.id)
        ts = SqliteTaskStore(conn=db_conn)
        ts.create_task(user_id=u.id, title="Task A", deadline=FUTURE_DL)
        ts.create_task(user_id=u.id, title="Task B", deadline=FUTURE_DL)
        deps = _make_deps(db_conn, us, task_store=ts)
        _run(_cmd_danh_sach_task("c1", user, deps))
        assert "Task A" in deps.channel.last_text
        assert "Task B" in deps.channel.last_text

    def test_empty_list_message(self, db_conn):
        us = SqliteUserStore(conn=db_conn)
        u = us.create_user(name="T", role="member")
        user = FakeUser(u.id)
        ts = SqliteTaskStore(conn=db_conn)
        deps = _make_deps(db_conn, us, task_store=ts)
        _run(_cmd_danh_sach_task("c1", user, deps))
        assert "Không có" in deps.channel.last_text

    def test_does_not_show_other_users_tasks(self, db_conn):
        us = SqliteUserStore(conn=db_conn)
        owner = us.create_user(name="Owner", role="member")
        requester = us.create_user(name="Other", role="member")
        user = FakeUser(requester.id)
        ts = SqliteTaskStore(conn=db_conn)
        ts.create_task(user_id=owner.id, title="Private Task", deadline=FUTURE_DL)
        deps = _make_deps(db_conn, us, task_store=ts)
        _run(_cmd_danh_sach_task("c1", user, deps))
        assert "Private Task" not in deps.channel.last_text


# ── _cmd_xem_task ─────────────────────────────────────────────────────────────


class TestCmdXemTask:
    def test_shows_task_detail(self, db_conn):
        us = SqliteUserStore(conn=db_conn)
        u = us.create_user(name="T", role="member")
        user = FakeUser(u.id)
        ts = SqliteTaskStore(conn=db_conn)
        task = ts.create_task(user_id=u.id, title="Detail task", deadline=FUTURE_DL)
        deps = _make_deps(db_conn, us, task_store=ts)
        _run(_cmd_xem_task("c1", str(task["id"]), user, deps))
        assert "Detail task" in deps.channel.last_text

    def test_invalid_id_shows_syntax(self, db_conn):
        us = SqliteUserStore(conn=db_conn)
        u = us.create_user(name="T", role="member")
        user = FakeUser(u.id)
        ts = SqliteTaskStore(conn=db_conn)
        deps = _make_deps(db_conn, us, task_store=ts)
        _run(_cmd_xem_task("c1", "xyz", user, deps))
        assert "Cú pháp" in deps.channel.last_text

    def test_wrong_user_denied(self, db_conn):
        us = SqliteUserStore(conn=db_conn)
        owner = us.create_user(name="Owner", role="member")
        requester = us.create_user(name="Other", role="member")
        user = FakeUser(requester.id)
        ts = SqliteTaskStore(conn=db_conn)
        task = ts.create_task(user_id=owner.id, title="Private", deadline=FUTURE_DL)
        deps = _make_deps(db_conn, us, task_store=ts)
        _run(_cmd_xem_task("c1", str(task["id"]), user, deps))
        assert "Không tìm thấy" in deps.channel.last_text


# ── _cmd_lich_hoc ─────────────────────────────────────────────────────────────


class TestCmdLichHoc:
    def test_forces_study_category(self, db_conn):
        us = SqliteUserStore(conn=db_conn)
        u = us.create_user(name="T", role="member")
        user = FakeUser(u.id)
        ts = SqliteTaskStore(conn=db_conn)
        # LLM returns category='task' — handler must override to 'study'.
        parsed = ParsedTask("Học toán", FUTURE_DL, None, "task")
        parser = _make_parser(parsed)
        deps = _make_deps(db_conn, us, task_store=ts, task_parser=parser)
        _run(_cmd_lich_hoc("c1", "toan thu 2 luc 7h", user, deps))
        task = ts.get_task(1)
        assert task["category"] == "study"

    def test_reply_contains_book_emoji(self, db_conn):
        us = SqliteUserStore(conn=db_conn)
        u = us.create_user(name="T", role="member")
        user = FakeUser(u.id)
        ts = SqliteTaskStore(conn=db_conn)
        parsed = ParsedTask("Học toán", FUTURE_DL, None, "study")
        parser = _make_parser(parsed)
        deps = _make_deps(db_conn, us, task_store=ts, task_parser=parser)
        _run(_cmd_lich_hoc("c1", "toan thu 2 luc 7h", user, deps))
        assert "📚" in deps.channel.last_text

    def test_empty_body_sends_hint(self, db_conn):
        us = SqliteUserStore(conn=db_conn)
        u = us.create_user(name="T", role="member")
        user = FakeUser(u.id)
        ts = SqliteTaskStore(conn=db_conn)
        parser = _make_parser(ParsedTask("T", FUTURE_DL, None, "study"))
        deps = _make_deps(db_conn, us, task_store=ts, task_parser=parser)
        _run(_cmd_lich_hoc("c1", "", user, deps))
        assert "lich hoc" in deps.channel.last_text.lower() or "mô tả" in deps.channel.last_text


# ── _cmd_hoan_task ────────────────────────────────────────────────────────────


class TestCmdHoanTask:
    def test_snoozes_task(self, db_conn):
        us = SqliteUserStore(conn=db_conn)
        u = us.create_user(name="T", role="member")
        user = FakeUser(u.id)
        ts = SqliteTaskStore(conn=db_conn)
        rs = SqliteReminderStore(conn=db_conn)
        task = ts.create_task(user_id=u.id, title="Snooze me", deadline=FUTURE_DL)
        engine = ReminderEngine(
            task_store=ts,
            reminder_store=rs,
            user_store=us,
            notification_service=MagicMock(),
            audit=SqliteAuditLog(conn=db_conn),
        )
        deps = _make_deps(
            db_conn, us, task_store=ts, reminder_store=rs, reminder_engine=engine,
        )
        _run(_cmd_hoan_task("c1", f"{task['id']} 30", user, deps))
        assert "hoãn" in deps.channel.last_text.lower() or "Snooze me" in deps.channel.last_text

    def test_invalid_syntax_shows_hint(self, db_conn):
        us = SqliteUserStore(conn=db_conn)
        u = us.create_user(name="T", role="member")
        user = FakeUser(u.id)
        ts = SqliteTaskStore(conn=db_conn)
        rs = SqliteReminderStore(conn=db_conn)
        engine = MagicMock()
        deps = _make_deps(
            db_conn, us, task_store=ts, reminder_store=rs, reminder_engine=engine,
        )
        _run(_cmd_hoan_task("c1", "abc", user, deps))
        assert "Cú pháp" in deps.channel.last_text

    def test_snooze_max_error_forwarded(self, db_conn):
        us = SqliteUserStore(conn=db_conn)
        u = us.create_user(name="T", role="member")
        user = FakeUser(u.id)
        ts = SqliteTaskStore(conn=db_conn)
        rs = SqliteReminderStore(conn=db_conn)
        engine = MagicMock()
        engine.snooze.side_effect = ValueError("max snooze count")
        task = ts.create_task(user_id=u.id, title="Over limit", deadline=FUTURE_DL)
        deps = _make_deps(
            db_conn, us, task_store=ts, reminder_store=rs, reminder_engine=engine,
        )
        _run(_cmd_hoan_task("c1", f"{task['id']} 15", user, deps))
        assert "max snooze" in deps.channel.last_text


# ── Inline keyboard — tao task ────────────────────────────────────────────────


class TestCmdTaoTaskInlineButtons:
    @pytest.fixture()
    def setup(self, db_conn):
        us = SqliteUserStore(conn=db_conn)
        u = us.create_user(name="Test", role="member")
        user = FakeUser(u.id)
        ts = SqliteTaskStore(conn=db_conn)
        parsed = ParsedTask("Mua sữa", FUTURE_DL, None, "task")
        parser = _make_parser(parsed)
        deps = _make_deps(db_conn, us, task_store=ts, task_parser=parser)
        return deps, user

    def test_reply_uses_inline_keyboard(self, setup):
        deps, user = setup
        _run(_cmd_tao_task("c1", "mua sua 5h chieu mai", user, deps))
        assert len(deps.channel.inline_sent) == 1

    def test_buttons_contain_done_action(self, setup):
        deps, user = setup
        _run(_cmd_tao_task("c1", "mua sua 5h chieu mai", user, deps))
        buttons = deps.channel.inline_sent[0][2]
        flat = [btn for row in buttons for btn in row]
        assert any(btn["callback_data"].startswith("done:") for btn in flat)

    def test_buttons_contain_snooze_options(self, setup):
        deps, user = setup
        _run(_cmd_tao_task("c1", "mua sua 5h chieu mai", user, deps))
        buttons = deps.channel.inline_sent[0][2]
        flat = [btn for row in buttons for btn in row]
        assert any("snooze:" in btn["callback_data"] for btn in flat)


# ── Inline keyboard — lich hoc ────────────────────────────────────────────────


class TestCmdLichHocInlineButtons:
    def test_reply_uses_inline_keyboard(self, db_conn):
        us = SqliteUserStore(conn=db_conn)
        u = us.create_user(name="T", role="member")
        user = FakeUser(u.id)
        ts = SqliteTaskStore(conn=db_conn)
        parsed = ParsedTask("Học toán", FUTURE_DL, None, "study")
        parser = _make_parser(parsed)
        deps = _make_deps(db_conn, us, task_store=ts, task_parser=parser)
        _run(_cmd_lich_hoc("c1", "toan thu 2 luc 7h", user, deps))
        assert len(deps.channel.inline_sent) == 1
