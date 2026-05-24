"""Tests for FR-7 sub-task 7.5 — inline keyboard / callback_query handling.

Two groups:
  1. TelegramAdapter.parse_webhook — callback_query path (parse + raw fields).
  2. _handle_callback dispatch in core_handler:
       done:<id>          → complete task + cancel reminders
       snooze:<id>:<min>  → call engine.snooze
       view:<id>          → send task detail
       unknown action     → send error reply
       answer_callback_query always called with correct cq_id
"""
from __future__ import annotations

import asyncio
from datetime import timedelta, timezone
from unittest.mock import MagicMock

import pytest

from audit import SqliteAuditLog
from channel_telegram import TelegramAdapter
from deps import CoreDeps
from interfaces import ChannelMessage
from task_store import SqliteTaskStore
from user_store import SqliteUserStore

VN_TZ = timezone(timedelta(hours=7))
FUTURE_DL = "2099-01-01T09:00:00+07:00"
CHAT_ID = "9999"
CQ_ID = "cq-abc123"


def _run(coro):
    return asyncio.run(coro)


def _import_handle_callback():
    """Lazy import so TelegramAdapter tests still run if _handle_callback not yet implemented."""
    from cmd_task import _handle_callback  # noqa: PLC0415
    return _handle_callback


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
    def __init__(self, user_id: int) -> None:
        self.id = user_id
        self.role = "member"


def _make_deps(db_conn, user_store, *, task_store=None, reminder_engine=None) -> CoreDeps:
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
        reminder_engine=reminder_engine,
    )


def _make_callback_msg(callback_data: str, cq_id: str = CQ_ID) -> ChannelMessage:
    return ChannelMessage(
        channel="telegram",
        chat_id=CHAT_ID,
        text="",
        raw={
            "callback_data": callback_data,
            "callback_query_id": cq_id,
            "message_id": 42,
        },
    )


# ── TelegramAdapter.parse_webhook — callback_query path ──────────────────────


class TestParseWebhookCallbackQuery:
    def _adapter(self) -> TelegramAdapter:
        return TelegramAdapter(token="test_token", allowed_chat_id=CHAT_ID)

    def _cq_payload(self, callback_data: str, chat_id: str = CHAT_ID) -> dict:
        return {
            "callback_query": {
                "id": CQ_ID,
                "data": callback_data,
                "from": {"id": 1, "first_name": "Test"},
                "message": {
                    "message_id": 42,
                    "chat": {"id": int(chat_id)},
                },
            }
        }

    def test_callback_query_returns_channel_message(self):
        adapter = self._adapter()
        result = adapter.parse_webhook(self._cq_payload("done:5"))
        assert result is not None

    def test_callback_query_sets_callback_data_in_raw(self):
        adapter = self._adapter()
        result = adapter.parse_webhook(self._cq_payload("snooze:3:15"))
        assert result is not None
        assert result.raw.get("callback_data") == "snooze:3:15"

    def test_callback_query_sets_correct_chat_id(self):
        adapter = self._adapter()
        result = adapter.parse_webhook(self._cq_payload("done:5"))
        assert result is not None
        assert result.chat_id == CHAT_ID

    def test_empty_payload_returns_none(self):
        adapter = self._adapter()
        result = adapter.parse_webhook({})
        assert result is None


# ── _handle_callback — done ───────────────────────────────────────────────────


class TestHandleCallbackDone:
    def test_done_marks_task_completed(self, db_conn):
        _handle_callback = _import_handle_callback()
        us = SqliteUserStore(conn=db_conn)
        u = us.create_user(name="T", role="member")
        ts = SqliteTaskStore(conn=db_conn)
        task = ts.create_task(user_id=u.id, title="Finish me", deadline=FUTURE_DL)
        deps = _make_deps(db_conn, us, task_store=ts)
        _run(_handle_callback(_make_callback_msg(f"done:{task['id']}"), FakeUser(u.id), deps))
        assert ts.get_task(task["id"])["status"] == "completed"

    def test_done_cancels_reminders_via_engine(self, db_conn):
        _handle_callback = _import_handle_callback()
        us = SqliteUserStore(conn=db_conn)
        u = us.create_user(name="T", role="member")
        ts = SqliteTaskStore(conn=db_conn)
        task = ts.create_task(user_id=u.id, title="Done", deadline=FUTURE_DL)
        engine = MagicMock()
        deps = _make_deps(db_conn, us, task_store=ts, reminder_engine=engine)
        _run(_handle_callback(_make_callback_msg(f"done:{task['id']}"), FakeUser(u.id), deps))
        engine.cancel_all_for_task.assert_called_once_with(task["id"])

    def test_done_wrong_owner_sends_not_found(self, db_conn):
        _handle_callback = _import_handle_callback()
        us = SqliteUserStore(conn=db_conn)
        owner = us.create_user(name="Owner", role="member")
        other = us.create_user(name="Other", role="member")
        ts = SqliteTaskStore(conn=db_conn)
        task = ts.create_task(user_id=owner.id, title="Private", deadline=FUTURE_DL)
        deps = _make_deps(db_conn, us, task_store=ts)
        _run(_handle_callback(_make_callback_msg(f"done:{task['id']}"), FakeUser(other.id), deps))
        assert "Không tìm thấy" in deps.channel.last_text


# ── _handle_callback — snooze ─────────────────────────────────────────────────


class TestHandleCallbackSnooze:
    def test_snooze_15_calls_engine_with_correct_args(self, db_conn):
        _handle_callback = _import_handle_callback()
        us = SqliteUserStore(conn=db_conn)
        u = us.create_user(name="T", role="member")
        ts = SqliteTaskStore(conn=db_conn)
        task = ts.create_task(user_id=u.id, title="Snooze", deadline=FUTURE_DL)
        engine = MagicMock()
        deps = _make_deps(db_conn, us, task_store=ts, reminder_engine=engine)
        _run(_handle_callback(_make_callback_msg(f"snooze:{task['id']}:15"), FakeUser(u.id), deps))
        engine.snooze.assert_called_once_with(task["id"], 15)

    def test_snooze_60_calls_engine_with_correct_args(self, db_conn):
        _handle_callback = _import_handle_callback()
        us = SqliteUserStore(conn=db_conn)
        u = us.create_user(name="T", role="member")
        ts = SqliteTaskStore(conn=db_conn)
        task = ts.create_task(user_id=u.id, title="Snooze 60", deadline=FUTURE_DL)
        engine = MagicMock()
        deps = _make_deps(db_conn, us, task_store=ts, reminder_engine=engine)
        _run(_handle_callback(_make_callback_msg(f"snooze:{task['id']}:60"), FakeUser(u.id), deps))
        engine.snooze.assert_called_once_with(task["id"], 60)

    def test_snooze_max_exceeded_sends_error_reply(self, db_conn):
        _handle_callback = _import_handle_callback()
        us = SqliteUserStore(conn=db_conn)
        u = us.create_user(name="T", role="member")
        ts = SqliteTaskStore(conn=db_conn)
        task = ts.create_task(user_id=u.id, title="Over limit", deadline=FUTURE_DL)
        engine = MagicMock()
        engine.snooze.side_effect = ValueError("Đã hoãn tối đa 3 lần.")
        deps = _make_deps(db_conn, us, task_store=ts, reminder_engine=engine)
        _run(_handle_callback(_make_callback_msg(f"snooze:{task['id']}:15"), FakeUser(u.id), deps))
        assert "tối đa" in deps.channel.last_text or "max" in deps.channel.last_text.lower()


# ── _handle_callback — view ───────────────────────────────────────────────────


class TestHandleCallbackView:
    def test_view_sends_task_title_in_reply(self, db_conn):
        _handle_callback = _import_handle_callback()
        us = SqliteUserStore(conn=db_conn)
        u = us.create_user(name="T", role="member")
        ts = SqliteTaskStore(conn=db_conn)
        task = ts.create_task(user_id=u.id, title="View me", deadline=FUTURE_DL)
        deps = _make_deps(db_conn, us, task_store=ts)
        _run(_handle_callback(_make_callback_msg(f"view:{task['id']}"), FakeUser(u.id), deps))
        assert "View me" in deps.channel.last_text


# ── _handle_callback — misc ───────────────────────────────────────────────────


class TestHandleCallbackMisc:
    def test_unknown_action_sends_non_empty_reply(self, db_conn):
        _handle_callback = _import_handle_callback()
        us = SqliteUserStore(conn=db_conn)
        u = us.create_user(name="T", role="member")
        ts = SqliteTaskStore(conn=db_conn)
        deps = _make_deps(db_conn, us, task_store=ts)
        _run(_handle_callback(_make_callback_msg("xyz:99"), FakeUser(u.id), deps))
        assert deps.channel.last_text != ""

    def test_answer_callback_query_called_with_cq_id(self, db_conn):
        _handle_callback = _import_handle_callback()
        us = SqliteUserStore(conn=db_conn)
        u = us.create_user(name="T", role="member")
        ts = SqliteTaskStore(conn=db_conn)
        task = ts.create_task(user_id=u.id, title="Ack me", deadline=FUTURE_DL)
        deps = _make_deps(db_conn, us, task_store=ts)
        _run(_handle_callback(_make_callback_msg(f"done:{task['id']}", cq_id=CQ_ID), FakeUser(u.id), deps))
        assert CQ_ID in deps.channel.answered_callbacks
