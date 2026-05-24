"""Tests for task_store.SqliteTaskStore and FR-7 user preference methods.

Covers:
  - SqliteTaskStore: create, get, list (filter by status/deleted), pending-due,
    completed-on, update, complete, cancel, snooze-count, soft-delete, restore.
  - reminder_store.cancel_for_task integration via complete_task / cancel_task /
    soft_delete_task (reminder cancellation happens atomically in the store).
  - SqliteUserStore: get/set daily_summary_time, get/set morning_default_time,
    validation of HH:MM format.
"""
from __future__ import annotations

import pytest

from reminder_store import SqliteReminderStore
from task_store import SqliteTaskStore
from user_store import SqliteUserStore


# ── Fixtures ──────────────────────────────────────────────────────────────────

DEADLINE_FUTURE = "2099-12-31 09:00:00"
DEADLINE_PAST   = "2020-01-01 09:00:00"


@pytest.fixture()
def task_store(db_conn):
    return SqliteTaskStore(conn=db_conn)


@pytest.fixture()
def reminder_store(db_conn):
    return SqliteReminderStore(conn=db_conn)


@pytest.fixture()
def user_id(store):
    """A real user id satisfying FK on tasks.user_id."""
    u = store.create_user(name="Task User", role="member")
    return u.id


@pytest.fixture()
def other_user_id(store):
    u = store.create_user(name="Other User", role="member")
    return u.id


@pytest.fixture()
def a_task(task_store, user_id):
    """A single pending task with a future deadline."""
    return task_store.create_task(user_id, "Buy groceries", DEADLINE_FUTURE)


# ═════════════════════════════════════════════════════════════════════════════
# create_task
# ═════════════════════════════════════════════════════════════════════════════


class TestCreateTask:

    def test_returns_dict_with_id(self, task_store, user_id):
        task = task_store.create_task(user_id, "Write report", DEADLINE_FUTURE)
        assert isinstance(task["id"], int) and task["id"] > 0

    def test_defaults(self, task_store, user_id):
        task = task_store.create_task(user_id, "Read book", DEADLINE_FUTURE)
        assert task["status"] == "pending"
        assert task["category"] == "task"
        assert task["scope"] == "private"
        assert task["source"] == "telegram"
        assert task["snooze_count"] == 0
        assert task["deleted_at"] is None
        assert task["completed_at"] is None

    def test_custom_fields(self, task_store, user_id):
        task = task_store.create_task(
            user_id, "Study English", DEADLINE_FUTURE,
            description="Chapter 3",
            category="study",
            source="web",
            recurring_rule="weekly:MON,WED@07:00",
            reminder_offsets="3600,1800",
        )
        assert task["category"] == "study"
        assert task["source"] == "web"
        assert task["recurring_rule"] == "weekly:MON,WED@07:00"
        assert task["reminder_offsets"] == "3600,1800"
        assert task["description"] == "Chapter 3"

    def test_empty_title_raises(self, task_store, user_id):
        with pytest.raises(ValueError):
            task_store.create_task(user_id, "", DEADLINE_FUTURE)

    def test_whitespace_title_raises(self, task_store, user_id):
        with pytest.raises(ValueError):
            task_store.create_task(user_id, "   ", DEADLINE_FUTURE)

    def test_empty_deadline_raises(self, task_store, user_id):
        with pytest.raises(ValueError):
            task_store.create_task(user_id, "Do something", "")

    def test_title_stripped(self, task_store, user_id):
        task = task_store.create_task(user_id, "  Hello  ", DEADLINE_FUTURE)
        assert task["title"] == "Hello"


# ═════════════════════════════════════════════════════════════════════════════
# get_task
# ═════════════════════════════════════════════════════════════════════════════


class TestGetTask:

    def test_returns_task(self, task_store, a_task):
        result = task_store.get_task(a_task["id"])
        assert result is not None
        assert result["id"] == a_task["id"]

    def test_returns_none_for_missing(self, task_store):
        assert task_store.get_task(99999) is None

    def test_includes_soft_deleted(self, task_store, a_task):
        task_store.soft_delete_task(a_task["id"])
        result = task_store.get_task(a_task["id"])
        assert result is not None
        assert result["deleted_at"] is not None


# ═════════════════════════════════════════════════════════════════════════════
# list_for_user
# ═════════════════════════════════════════════════════════════════════════════


class TestListForUser:

    def test_returns_own_tasks_only(self, task_store, user_id, other_user_id):
        task_store.create_task(user_id, "Mine", DEADLINE_FUTURE)
        task_store.create_task(other_user_id, "Theirs", DEADLINE_FUTURE)
        results = task_store.list_for_user(user_id)
        assert all(r["user_id"] == user_id for r in results)
        assert len(results) == 1

    def test_excludes_soft_deleted_by_default(self, task_store, user_id, a_task):
        task_store.soft_delete_task(a_task["id"])
        results = task_store.list_for_user(user_id)
        assert all(r["id"] != a_task["id"] for r in results)

    def test_include_deleted_flag(self, task_store, user_id, a_task):
        task_store.soft_delete_task(a_task["id"])
        results = task_store.list_for_user(user_id, include_deleted=True)
        ids = [r["id"] for r in results]
        assert a_task["id"] in ids

    def test_filter_by_status(self, task_store, user_id):
        t1 = task_store.create_task(user_id, "Task A", DEADLINE_FUTURE)
        t2 = task_store.create_task(user_id, "Task B", DEADLINE_FUTURE)
        task_store.complete_task(t1["id"])
        pending = task_store.list_for_user(user_id, status="pending")
        completed = task_store.list_for_user(user_id, status="completed")
        assert all(r["status"] == "pending" for r in pending)
        assert all(r["status"] == "completed" for r in completed)
        assert t2["id"] in [r["id"] for r in pending]
        assert t1["id"] in [r["id"] for r in completed]

    def test_ordered_by_deadline_asc(self, task_store, user_id):
        task_store.create_task(user_id, "Later", "2099-12-31 09:00:00")
        task_store.create_task(user_id, "Earlier", "2050-01-01 09:00:00")
        results = task_store.list_for_user(user_id)
        deadlines = [r["deadline"] for r in results]
        assert deadlines == sorted(deadlines)


# ═════════════════════════════════════════════════════════════════════════════
# list_pending_due
# ═════════════════════════════════════════════════════════════════════════════


class TestListPendingDue:

    def test_returns_tasks_at_or_before_cutoff(self, task_store, user_id):
        task_store.create_task(user_id, "Due soon", "2030-06-01 09:00:00")
        task_store.create_task(user_id, "Due later", "2050-06-01 09:00:00")
        results = task_store.list_pending_due("2030-06-01 09:00:00")
        assert len(results) == 1
        assert results[0]["title"] == "Due soon"

    def test_excludes_completed(self, task_store, user_id):
        t = task_store.create_task(user_id, "Done", "2030-01-01 09:00:00")
        task_store.complete_task(t["id"])
        results = task_store.list_pending_due("2099-12-31 09:00:00")
        assert all(r["status"] == "pending" for r in results)

    def test_excludes_soft_deleted(self, task_store, user_id):
        t = task_store.create_task(user_id, "Deleted", "2030-01-01 09:00:00")
        task_store.soft_delete_task(t["id"])
        results = task_store.list_pending_due("2099-12-31 09:00:00")
        assert all(r["id"] != t["id"] for r in results)

    def test_filter_by_user_id(self, task_store, user_id, other_user_id):
        task_store.create_task(user_id, "Mine", "2030-01-01 09:00:00")
        task_store.create_task(other_user_id, "Theirs", "2030-01-01 09:00:00")
        results = task_store.list_pending_due("2099-12-31 09:00:00", user_id=user_id)
        assert all(r["user_id"] == user_id for r in results)


# ═════════════════════════════════════════════════════════════════════════════
# list_completed_on
# ═════════════════════════════════════════════════════════════════════════════


class TestListCompletedOn:

    def test_returns_completed_on_date(self, task_store, user_id):
        t = task_store.create_task(user_id, "Done today", DEADLINE_FUTURE)
        task_store.complete_task(t["id"], completed_at="2026-05-23 10:00:00")
        results = task_store.list_completed_on(user_id, "2026-05-23")
        assert len(results) == 1
        assert results[0]["id"] == t["id"]

    def test_excludes_other_dates(self, task_store, user_id):
        t = task_store.create_task(user_id, "Done yesterday", DEADLINE_FUTURE)
        task_store.complete_task(t["id"], completed_at="2026-05-22 10:00:00")
        results = task_store.list_completed_on(user_id, "2026-05-23")
        assert results == []


# ═════════════════════════════════════════════════════════════════════════════
# update_task
# ═════════════════════════════════════════════════════════════════════════════


class TestUpdateTask:

    def test_updates_title(self, task_store, a_task):
        updated = task_store.update_task(a_task["id"], title="New title")
        assert updated["title"] == "New title"

    def test_updates_deadline(self, task_store, a_task):
        new_dl = "2088-01-01 12:00:00"
        updated = task_store.update_task(a_task["id"], deadline=new_dl)
        assert updated["deadline"] == new_dl

    def test_ignores_disallowed_fields(self, task_store, a_task):
        result = task_store.update_task(a_task["id"], user_id=9999)
        # user_id should not change — disallowed field is ignored
        assert result["user_id"] == a_task["user_id"]

    def test_no_fields_returns_unchanged(self, task_store, a_task):
        result = task_store.update_task(a_task["id"])
        assert result["id"] == a_task["id"]

    def test_returns_none_for_missing(self, task_store):
        assert task_store.update_task(99999, title="x") is None


# ═════════════════════════════════════════════════════════════════════════════
# complete_task
# ═════════════════════════════════════════════════════════════════════════════


class TestCompleteTask:

    def test_sets_status_completed(self, task_store, a_task):
        result = task_store.complete_task(a_task["id"])
        assert result["status"] == "completed"
        assert result["completed_at"] is not None

    def test_cancels_pending_reminders(self, task_store, reminder_store, user_id):
        t = task_store.create_task(user_id, "With reminders", DEADLINE_FUTURE)
        reminder_store.bulk_create_for_task(t["id"], DEADLINE_FUTURE, [3600, 1800])
        assert reminder_store.count_pending_for_task(t["id"]) == 2
        task_store.complete_task(t["id"])
        assert reminder_store.count_pending_for_task(t["id"]) == 0

    def test_custom_completed_at(self, task_store, a_task):
        ts = "2026-05-23 08:00:00"
        result = task_store.complete_task(a_task["id"], completed_at=ts)
        assert result["completed_at"] == ts

    def test_already_completed_no_double_write(self, task_store, a_task):
        ts1 = "2026-05-23 08:00:00"
        task_store.complete_task(a_task["id"], completed_at=ts1)
        task_store.complete_task(a_task["id"], completed_at="2026-05-24 09:00:00")
        result = task_store.get_task(a_task["id"])
        # completed_at should be the first call's value — second UPDATE finds no 'pending' row
        assert result["completed_at"] == ts1


# ═════════════════════════════════════════════════════════════════════════════
# cancel_task
# ═════════════════════════════════════════════════════════════════════════════


class TestCancelTask:

    def test_sets_status_cancelled(self, task_store, a_task):
        result = task_store.cancel_task(a_task["id"])
        assert result["status"] == "cancelled"

    def test_cancels_pending_reminders(self, task_store, reminder_store, user_id):
        t = task_store.create_task(user_id, "To cancel", DEADLINE_FUTURE)
        reminder_store.bulk_create_for_task(t["id"], DEADLINE_FUTURE, [3600])
        task_store.cancel_task(t["id"])
        assert reminder_store.count_pending_for_task(t["id"]) == 0


# ═════════════════════════════════════════════════════════════════════════════
# increment_snooze
# ═════════════════════════════════════════════════════════════════════════════


class TestIncrementSnooze:

    def test_starts_at_zero(self, task_store, a_task):
        assert a_task["snooze_count"] == 0

    def test_increments_by_one(self, task_store, a_task):
        count = task_store.increment_snooze(a_task["id"])
        assert count == 1

    def test_accumulates(self, task_store, a_task):
        task_store.increment_snooze(a_task["id"])
        count = task_store.increment_snooze(a_task["id"])
        assert count == 2

    def test_returns_zero_for_missing(self, task_store):
        assert task_store.increment_snooze(99999) == 0


# ═════════════════════════════════════════════════════════════════════════════
# soft_delete / restore
# ═════════════════════════════════════════════════════════════════════════════


class TestSoftDelete:

    def test_sets_deleted_at(self, task_store, a_task):
        task_store.soft_delete_task(a_task["id"])
        result = task_store.get_task(a_task["id"])
        assert result["deleted_at"] is not None

    def test_returns_true_on_delete(self, task_store, a_task):
        assert task_store.soft_delete_task(a_task["id"]) is True

    def test_returns_false_on_already_deleted(self, task_store, a_task):
        task_store.soft_delete_task(a_task["id"])
        assert task_store.soft_delete_task(a_task["id"]) is False

    def test_cancels_pending_reminders(self, task_store, reminder_store, user_id):
        t = task_store.create_task(user_id, "To delete", DEADLINE_FUTURE)
        reminder_store.bulk_create_for_task(t["id"], DEADLINE_FUTURE, [3600])
        task_store.soft_delete_task(t["id"])
        assert reminder_store.count_pending_for_task(t["id"]) == 0

    def test_restore_clears_deleted_at(self, task_store, a_task):
        task_store.soft_delete_task(a_task["id"])
        task_store.restore_task(a_task["id"])
        result = task_store.get_task(a_task["id"])
        assert result["deleted_at"] is None

    def test_restore_returns_false_when_not_deleted(self, task_store, a_task):
        assert task_store.restore_task(a_task["id"]) is False


# ═════════════════════════════════════════════════════════════════════════════
# User task preferences (FR-7 D8, D16) — SqliteUserStore methods
# ═════════════════════════════════════════════════════════════════════════════


class TestDailySummaryTime:

    def test_default_is_none(self, store, user_id):
        assert store.get_daily_summary_time(user_id) is None

    def test_set_valid_hhmm(self, store, user_id):
        store.set_daily_summary_time(user_id, "21:00")
        assert store.get_daily_summary_time(user_id) == "21:00"

    def test_set_off(self, store, user_id):
        store.set_daily_summary_time(user_id, "off")
        assert store.get_daily_summary_time(user_id) == "off"

    def test_reset_to_none(self, store, user_id):
        store.set_daily_summary_time(user_id, "20:00")
        store.set_daily_summary_time(user_id, None)
        assert store.get_daily_summary_time(user_id) is None

    def test_invalid_format_raises(self, store, user_id):
        with pytest.raises(ValueError):
            store.set_daily_summary_time(user_id, "9pm")

    def test_hour_out_of_range_raises(self, store, user_id):
        with pytest.raises(ValueError):
            store.set_daily_summary_time(user_id, "25:00")

    def test_minute_out_of_range_raises(self, store, user_id):
        with pytest.raises(ValueError):
            store.set_daily_summary_time(user_id, "09:60")

    def test_midnight_valid(self, store, user_id):
        store.set_daily_summary_time(user_id, "00:00")
        assert store.get_daily_summary_time(user_id) == "00:00"

    def test_returns_none_for_unknown_user(self, store):
        assert store.get_daily_summary_time(99999) is None


class TestMorningDefaultTime:

    def test_default_is_none(self, store, user_id):
        assert store.get_morning_default_time(user_id) is None

    def test_set_valid_hhmm(self, store, user_id):
        store.set_morning_default_time(user_id, "09:00")
        assert store.get_morning_default_time(user_id) == "09:00"

    def test_reset_to_none(self, store, user_id):
        store.set_morning_default_time(user_id, "08:30")
        store.set_morning_default_time(user_id, None)
        assert store.get_morning_default_time(user_id) is None

    def test_invalid_format_raises(self, store, user_id):
        with pytest.raises(ValueError):
            store.set_morning_default_time(user_id, "9h00")

    def test_off_not_valid_for_morning_time(self, store, user_id):
        # 'off' is only valid for daily_summary_time, not morning_default_time
        with pytest.raises(ValueError):
            store.set_morning_default_time(user_id, "off")

    def test_boundary_23_59_valid(self, store, user_id):
        store.set_morning_default_time(user_id, "23:59")
        assert store.get_morning_default_time(user_id) == "23:59"
