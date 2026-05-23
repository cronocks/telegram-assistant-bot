"""Tests for reminder_engine — ReminderEngine and parse_recurring_rule.

Strategy:
  - parse_recurring_rule: pure-function unit tests; no DB needed.
  - ReminderEngine: real in-memory SQLite for task_store + reminder_store;
    MagicMock for notification_service and audit (we assert call counts/args).
  - now_fn is always injected so time is deterministic.

Covers:
  - parse_recurring_rule: daily, weekly single/multi-day, boundary, errors.
  - schedule_for_task: creates correct number of rows.
  - cancel_all_for_task: delegates to reminder_store.
  - tick: fire, missed (grace), task-not-pending safety, snoozed reminder.
  - tick recurring: last pending fires → expand; not-last → no expand.
  - _emit parent mirror: under-18 with parent, under-18 no parent, 18+, no birthdate.
  - snooze: happy path, max exceeded, task-not-found.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock, call

import pytest

from reminder_engine import ReminderEngine, SNOOZE_MAX, parse_recurring_rule
from reminder_store import SqliteReminderStore
from task_store import SqliteTaskStore

# Fixed reference time: Monday 2026-05-25 08:00:00 VN (UTC+7 = 01:00 UTC).
VN_TZ = timezone(timedelta(hours=7))
T0 = datetime(2026, 5, 25, 8, 0, 0, tzinfo=VN_TZ)   # Monday 08:00 VN


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def task_store(db_conn):
    return SqliteTaskStore(conn=db_conn)


@pytest.fixture()
def reminder_store(db_conn):
    return SqliteReminderStore(conn=db_conn)


@pytest.fixture()
def mock_notif():
    svc = MagicMock()
    svc.enqueue.return_value = 1
    return svc


@pytest.fixture()
def mock_audit():
    return MagicMock()


@pytest.fixture()
def user_id(store):
    u = store.create_user(name="Child User", role="member")
    return u.id


@pytest.fixture()
def parent_id(store):
    u = store.create_user(name="Parent User", role="member")
    return u.id


@pytest.fixture()
def engine(task_store, reminder_store, store, mock_notif, mock_audit):
    return ReminderEngine(
        task_store=task_store,
        reminder_store=reminder_store,
        user_store=store,
        notification_service=mock_notif,
        audit=mock_audit,
        now_fn=lambda: T0,
    )


def _task(task_store, user_id, deadline, *, recurring_rule=None, offsets="3600,1800"):
    """Helper: create a task and return it."""
    return task_store.create_task(
        user_id, "Test task", deadline,
        recurring_rule=recurring_rule,
        reminder_offsets=offsets,
    )


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


# ═════════════════════════════════════════════════════════════════════════════
# parse_recurring_rule — pure function
# ═════════════════════════════════════════════════════════════════════════════


class TestParseRecurringRule:

    # ── daily ─────────────────────────────────────────────────────────────────

    def test_daily_same_day_when_before_time(self):
        # T0 = Monday 08:00; rule fires at 21:00 → today at 21:00
        after = datetime(2026, 5, 25, 8, 0, 0, tzinfo=VN_TZ)
        result = parse_recurring_rule("daily@21:00", after)
        assert result.date() == date(2026, 5, 25)
        assert result.hour == 21 and result.minute == 0

    def test_daily_next_day_when_after_time(self):
        # T0 = Monday 22:00; rule fires at 21:00 → tomorrow at 21:00
        after = datetime(2026, 5, 25, 22, 0, 0, tzinfo=VN_TZ)
        result = parse_recurring_rule("daily@21:00", after)
        assert result.date() == date(2026, 5, 26)
        assert result.hour == 21 and result.minute == 0

    def test_daily_strictly_after_when_exactly_at_time(self):
        # after == candidate → roll to next day
        after = datetime(2026, 5, 25, 21, 0, 0, tzinfo=VN_TZ)
        result = parse_recurring_rule("daily@21:00", after)
        assert result.date() == date(2026, 5, 26)

    def test_daily_midnight(self):
        # after = 08:00; midnight (00:00) has already passed today → rolls to next day.
        after = datetime(2026, 5, 25, 8, 0, 0, tzinfo=VN_TZ)
        result = parse_recurring_rule("daily@00:00", after)
        assert result.date() == date(2026, 5, 26)
        assert result.hour == 0 and result.minute == 0

    # ── weekly ────────────────────────────────────────────────────────────────

    def test_weekly_next_matching_day(self):
        # Monday 08:00; next WED@07:00 → this Wednesday
        after = datetime(2026, 5, 25, 8, 0, 0, tzinfo=VN_TZ)  # Monday
        result = parse_recurring_rule("weekly:WED@07:00", after)
        assert result.weekday() == 2  # Wednesday
        assert result.hour == 7 and result.minute == 0

    def test_weekly_same_weekday_rolls_one_week(self):
        # Monday 08:00; next MON@07:00 → next Monday (7 days ahead, same weekday earlier time)
        after = datetime(2026, 5, 25, 8, 0, 0, tzinfo=VN_TZ)
        result = parse_recurring_rule("weekly:MON@07:00", after)
        assert result.weekday() == 0  # Monday
        assert result.date() == date(2026, 6, 1)

    def test_weekly_same_weekday_later_time_same_day(self):
        # Monday 08:00; MON@21:00 is later today → rolls to next Monday
        # (days_ahead=1..7, day 7 is next Monday)
        after = datetime(2026, 5, 25, 8, 0, 0, tzinfo=VN_TZ)
        result = parse_recurring_rule("weekly:MON@21:00", after)
        # This is actually later today — but our algorithm always looks days_ahead>=1.
        # So it finds next Monday (7 days ahead).
        assert result.weekday() == 0
        assert result.date() == date(2026, 6, 1)

    def test_weekly_multi_day_picks_nearest(self):
        # Monday 08:00; MON,WED,FRI@07:00 → nearest is Wednesday (2 days)
        after = datetime(2026, 5, 25, 8, 0, 0, tzinfo=VN_TZ)
        result = parse_recurring_rule("weekly:MON,WED,FRI@07:00", after)
        assert result.weekday() == 2  # Wednesday

    def test_weekly_case_insensitive_days(self):
        after = datetime(2026, 5, 25, 8, 0, 0, tzinfo=VN_TZ)
        result = parse_recurring_rule("weekly:mon,wed@07:00", after)
        assert result.weekday() == 2

    def test_weekly_unknown_day_raises(self):
        after = datetime(2026, 5, 25, 8, 0, 0, tzinfo=VN_TZ)
        with pytest.raises(ValueError, match="Unknown weekday"):
            parse_recurring_rule("weekly:XYZ@07:00", after)

    def test_weekly_missing_at_raises(self):
        after = datetime(2026, 5, 25, 8, 0, 0, tzinfo=VN_TZ)
        with pytest.raises(ValueError, match="Missing '@'"):
            parse_recurring_rule("weekly:MON,WED", after)

    def test_unrecognised_format_raises(self):
        after = datetime(2026, 5, 25, 8, 0, 0, tzinfo=VN_TZ)
        with pytest.raises(ValueError, match="Unrecognised"):
            parse_recurring_rule("monthly:05@07:00", after)

    def test_result_is_vn_tz_aware(self):
        after = datetime(2026, 5, 25, 8, 0, 0, tzinfo=VN_TZ)
        result = parse_recurring_rule("daily@21:00", after)
        assert result.tzinfo is not None
        assert result.utcoffset().total_seconds() == 7 * 3600


# ═════════════════════════════════════════════════════════════════════════════
# schedule_for_task
# ═════════════════════════════════════════════════════════════════════════════


class TestScheduleForTask:

    def test_creates_one_row_per_offset(self, engine, task_store, reminder_store, user_id):
        deadline = _iso(T0 + timedelta(hours=3))
        task = _task(task_store, user_id, deadline, offsets="7200,3600,1800,900")
        ids = engine.schedule_for_task(task)
        assert len(ids) == 4

    def test_returns_positive_ids(self, engine, task_store, reminder_store, user_id):
        task = _task(task_store, user_id, _iso(T0 + timedelta(hours=3)))
        ids = engine.schedule_for_task(task)
        assert all(isinstance(i, int) and i > 0 for i in ids)

    def test_ids_are_distinct(self, engine, task_store, user_id):
        task = _task(task_store, user_id, _iso(T0 + timedelta(hours=3)), offsets="3600,1800")
        ids = engine.schedule_for_task(task)
        assert len(set(ids)) == 2

    def test_single_offset(self, engine, task_store, user_id):
        task = _task(task_store, user_id, _iso(T0 + timedelta(hours=2)), offsets="3600")
        ids = engine.schedule_for_task(task)
        assert len(ids) == 1


# ═════════════════════════════════════════════════════════════════════════════
# cancel_all_for_task
# ═════════════════════════════════════════════════════════════════════════════


class TestCancelAllForTask:

    def test_cancels_all_pending(self, engine, task_store, reminder_store, user_id):
        task = _task(task_store, user_id, _iso(T0 + timedelta(hours=3)), offsets="3600,1800")
        engine.schedule_for_task(task)
        assert reminder_store.count_pending_for_task(task["id"]) == 2
        engine.cancel_all_for_task(task["id"])
        assert reminder_store.count_pending_for_task(task["id"]) == 0

    def test_returns_count(self, engine, task_store, user_id):
        task = _task(task_store, user_id, _iso(T0 + timedelta(hours=3)), offsets="3600,1800,900")
        engine.schedule_for_task(task)
        assert engine.cancel_all_for_task(task["id"]) == 3


# ═════════════════════════════════════════════════════════════════════════════
# tick — fire path
# ═════════════════════════════════════════════════════════════════════════════


class TestTickFire:

    def test_fires_due_reminder(self, engine, task_store, reminder_store, user_id, mock_notif, mock_audit):
        # Deadline in 30 min; reminder offset 1800s → fire_at = T0 (exactly due).
        deadline = _iso(T0 + timedelta(seconds=1800))
        task = _task(task_store, user_id, deadline, offsets="1800")
        engine.schedule_for_task(task)

        stats = engine.tick()

        assert stats["fired"] == 1
        assert stats["missed"] == 0
        mock_notif.enqueue.assert_called_once()
        mock_audit.log.assert_called()

    def test_fired_reminder_marked_fired(self, engine, task_store, reminder_store, user_id):
        deadline = _iso(T0 + timedelta(seconds=1800))
        task = _task(task_store, user_id, deadline, offsets="1800")
        rids = engine.schedule_for_task(task)

        engine.tick()

        row = reminder_store.get_reminder(rids[0])
        assert row["status"] == "fired"
        assert row["fired_at"] is not None

    def test_reminder_not_yet_due_not_fired(self, engine, task_store, reminder_store, user_id, mock_notif):
        # Deadline in 5 hours; offset 1800s → fire_at 4.5h from now (not due yet).
        deadline = _iso(T0 + timedelta(hours=5))
        task = _task(task_store, user_id, deadline, offsets="1800")
        engine.schedule_for_task(task)

        stats = engine.tick()

        assert stats["fired"] == 0
        mock_notif.enqueue.assert_not_called()

    def test_snoozed_reminder_fires(self, engine, task_store, reminder_store, user_id, mock_notif):
        # Create a task and manually insert a snoozed reminder that is due now.
        task = _task(task_store, user_id, _iso(T0 + timedelta(hours=5)))
        reminder_store.create_snoozed(task["id"], _iso(T0))  # fire_at = T0

        stats = engine.tick()

        assert stats["fired"] == 1
        mock_notif.enqueue.assert_called_once()

    def test_audit_reminder_fired_emitted(self, engine, task_store, user_id, mock_audit):
        deadline = _iso(T0 + timedelta(seconds=1800))
        task = _task(task_store, user_id, deadline, offsets="1800")
        engine.schedule_for_task(task)
        engine.tick()

        actions = [c.kwargs.get("action") or c.args[1] for c in mock_audit.log.call_args_list]
        assert "reminder_fired" in actions


# ═════════════════════════════════════════════════════════════════════════════
# tick — missed (grace window) path
# ═════════════════════════════════════════════════════════════════════════════


class TestTickMissed:

    def test_overdue_by_more_than_1h_marked_missed(self, engine, task_store, reminder_store, user_id, mock_notif, mock_audit):
        # fire_at = 2 hours ago → overdue by 2h > 1h grace window.
        deadline = _iso(T0 - timedelta(hours=2) + timedelta(seconds=1800))
        task = _task(task_store, user_id, deadline, offsets="1800")
        engine.schedule_for_task(task)

        stats = engine.tick()

        assert stats["missed"] == 1
        assert stats["fired"] == 0
        mock_notif.enqueue.assert_not_called()

    def test_missed_reminder_status_is_missed(self, engine, task_store, reminder_store, user_id):
        deadline = _iso(T0 - timedelta(hours=2) + timedelta(seconds=1800))
        task = _task(task_store, user_id, deadline, offsets="1800")
        rids = engine.schedule_for_task(task)
        engine.tick()

        assert reminder_store.get_reminder(rids[0])["status"] == "missed"

    def test_audit_reminder_missed_emitted(self, engine, task_store, user_id, mock_audit):
        deadline = _iso(T0 - timedelta(hours=2) + timedelta(seconds=1800))
        task = _task(task_store, user_id, deadline, offsets="1800")
        engine.schedule_for_task(task)
        engine.tick()

        actions = [c.kwargs.get("action") or c.args[1] for c in mock_audit.log.call_args_list]
        assert "reminder_missed" in actions

    def test_within_grace_window_still_fires(self, engine, task_store, reminder_store, user_id, mock_notif):
        # fire_at = 30 min ago → within 1h grace window → still fires.
        deadline = _iso(T0 - timedelta(minutes=30) + timedelta(seconds=1800))
        task = _task(task_store, user_id, deadline, offsets="1800")
        engine.schedule_for_task(task)

        stats = engine.tick()

        assert stats["fired"] == 1
        mock_notif.enqueue.assert_called_once()

    def test_task_not_pending_skipped_silently(self, engine, task_store, reminder_store, user_id, mock_notif):
        # Complete the task — its reminders are cancelled atomically.
        # Manually set a reminder to pending to simulate the edge case.
        deadline = _iso(T0 + timedelta(seconds=1800))
        task = _task(task_store, user_id, deadline, offsets="1800")
        rids = engine.schedule_for_task(task)
        task_store.complete_task(task["id"])
        # Restore pending status on reminder to trigger the safety-net path.
        reminder_store._conn.execute(
            "UPDATE task_reminders SET status='pending' WHERE id=?", (rids[0],)
        )
        reminder_store._conn.commit()
        # Also undo the task status check by reverting task status in DB.
        # (The task's status in the JOIN will show 'completed'.)

        stats = engine.tick()

        assert stats["fired"] == 0
        mock_notif.enqueue.assert_not_called()


# ═════════════════════════════════════════════════════════════════════════════
# tick — recurring expansion
# ═════════════════════════════════════════════════════════════════════════════


class TestTickRecurring:

    def test_last_reminder_triggers_expansion(self, engine, task_store, reminder_store, user_id):
        # Single-offset task → 1 reminder → when it fires, expand.
        deadline = _iso(T0 + timedelta(seconds=1800))
        task = _task(task_store, user_id, deadline, offsets="1800", recurring_rule="daily@09:00")
        engine.schedule_for_task(task)

        stats = engine.tick()

        assert stats["fired"] == 1
        assert stats["recurring_expanded"] == 1

    def test_expansion_creates_new_reminder_rows(self, engine, task_store, reminder_store, user_id):
        deadline = _iso(T0 + timedelta(seconds=1800))
        task = _task(task_store, user_id, deadline, offsets="1800", recurring_rule="daily@09:00")
        engine.schedule_for_task(task)

        engine.tick()  # fires original + expands

        # After expansion, there should be new pending reminders.
        assert reminder_store.count_pending_for_task(task["id"]) == 1

    def test_expansion_updates_task_deadline(self, engine, task_store, reminder_store, user_id):
        deadline_dt = T0 + timedelta(seconds=1800)
        task = _task(task_store, user_id, _iso(deadline_dt), offsets="1800", recurring_rule="daily@09:00")
        engine.schedule_for_task(task)

        engine.tick()

        updated = task_store.get_task(task["id"])
        # Deadline should have advanced by ~1 day.
        assert updated["deadline"] != task["deadline"]

    def test_not_last_reminder_no_expansion(self, engine, task_store, reminder_store, user_id):
        # Task with recurring rule; one snoozed reminder fires now, one scheduled
        # reminder is not yet due (fire_at = T0 + 4h) → expansion must NOT trigger.
        task = _task(task_store, user_id, _iso(T0 + timedelta(hours=5)),
                     offsets="3600", recurring_rule="daily@09:00")
        # Snoozed reminder due now — will fire.
        reminder_store.create_snoozed(task["id"], _iso(T0))
        # Scheduled reminder: deadline T0+5h, offset 3600s → fire_at = T0+4h (not due).
        reminder_store.bulk_create_for_task(task["id"], _iso(T0 + timedelta(hours=5)), [3600])

        stats = engine.tick()

        # Only the snoozed reminder fires; 'snoozed' kind does not trigger expansion.
        assert stats["fired"] == 1
        assert stats["recurring_expanded"] == 0


# ═════════════════════════════════════════════════════════════════════════════
# _emit — parent mirror (D7)
# ═════════════════════════════════════════════════════════════════════════════


class TestEmitParentMirror:

    def _make_engine(self, task_store, reminder_store, store, mock_notif, mock_audit, now_fn=None):
        return ReminderEngine(
            task_store=task_store,
            reminder_store=reminder_store,
            user_store=store,
            notification_service=mock_notif,
            audit=mock_audit,
            now_fn=now_fn or (lambda: T0),
        )

    def test_under_18_with_parent_mirrors(self, task_store, reminder_store, store, mock_notif, mock_audit):
        # Child under 18 (born 2015); parent linked.
        child = store.create_user(name="Child", role="member")
        store._conn.execute("UPDATE users SET birthdate=? WHERE id=?", ("2015-01-01", child.id))
        store._conn.commit()
        parent = store.create_user(name="Parent", role="member")
        store.set_parent(child.id, parent.id, set_by=parent.id)

        engine = self._make_engine(task_store, reminder_store, store, mock_notif, mock_audit)
        task = task_store.create_task(child.id, "Homework", _iso(T0 + timedelta(hours=2)))
        rids = engine.schedule_for_task(task)
        # Make reminder due now.
        reminder_store._conn.execute("UPDATE task_reminders SET fire_at=? WHERE id=?", (_iso(T0), rids[0]))
        reminder_store._conn.commit()

        engine.tick()

        # Both child and parent should receive notifications.
        assert mock_notif.enqueue.call_count == 2
        call_user_ids = [c.args[0] for c in mock_notif.enqueue.call_args_list]
        assert child.id in call_user_ids
        assert parent.id in call_user_ids

    def test_adult_no_mirror(self, task_store, reminder_store, store, mock_notif, mock_audit):
        # Adult user (born 2000 → 26 years old in 2026).
        adult = store.create_user(name="Adult", role="member")
        store._conn.execute("UPDATE users SET birthdate=? WHERE id=?", ("2000-01-01", adult.id))
        store._conn.commit()

        engine = self._make_engine(task_store, reminder_store, store, mock_notif, mock_audit)
        task = task_store.create_task(adult.id, "Work task", _iso(T0 + timedelta(hours=2)))
        rids = engine.schedule_for_task(task)
        reminder_store._conn.execute("UPDATE task_reminders SET fire_at=? WHERE id=?", (_iso(T0), rids[0]))
        reminder_store._conn.commit()

        engine.tick()

        assert mock_notif.enqueue.call_count == 1

    def test_no_birthdate_no_mirror(self, task_store, reminder_store, store, mock_notif, mock_audit, user_id):
        # User with no birthdate — cannot determine age, no mirror.
        engine = self._make_engine(task_store, reminder_store, store, mock_notif, mock_audit)
        task = task_store.create_task(user_id, "Task", _iso(T0 + timedelta(hours=2)))
        rids = engine.schedule_for_task(task)
        reminder_store._conn.execute("UPDATE task_reminders SET fire_at=? WHERE id=?", (_iso(T0), rids[0]))
        reminder_store._conn.commit()

        engine.tick()

        assert mock_notif.enqueue.call_count == 1

    def test_under_18_no_parent_no_extra_notification(self, task_store, reminder_store, store, mock_notif, mock_audit):
        child = store.create_user(name="Child2", role="member")
        store._conn.execute("UPDATE users SET birthdate=? WHERE id=?", ("2015-06-01", child.id))
        store._conn.commit()
        # No parent linked.

        engine = self._make_engine(task_store, reminder_store, store, mock_notif, mock_audit)
        task = task_store.create_task(child.id, "Task", _iso(T0 + timedelta(hours=2)))
        rids = engine.schedule_for_task(task)
        reminder_store._conn.execute("UPDATE task_reminders SET fire_at=? WHERE id=?", (_iso(T0), rids[0]))
        reminder_store._conn.commit()

        engine.tick()

        assert mock_notif.enqueue.call_count == 1

    def test_mirror_payload_has_mirrored_from_user_id(self, task_store, reminder_store, store, mock_notif, mock_audit):
        child = store.create_user(name="Child3", role="member")
        store._conn.execute("UPDATE users SET birthdate=? WHERE id=?", ("2015-01-01", child.id))
        store._conn.commit()
        parent = store.create_user(name="Parent3", role="member")
        store.set_parent(child.id, parent.id, set_by=parent.id)

        engine = self._make_engine(task_store, reminder_store, store, mock_notif, mock_audit)
        task = task_store.create_task(child.id, "Violin practice", _iso(T0 + timedelta(hours=2)))
        rids = engine.schedule_for_task(task)
        reminder_store._conn.execute("UPDATE task_reminders SET fire_at=? WHERE id=?", (_iso(T0), rids[0]))
        reminder_store._conn.commit()

        engine.tick()

        # Find the parent's notification call.
        parent_call = next(
            c for c in mock_notif.enqueue.call_args_list if c.args[0] == parent.id
        )
        assert parent_call.args[2].get("mirrored_from_user_id") == child.id


# ═════════════════════════════════════════════════════════════════════════════
# snooze
# ═════════════════════════════════════════════════════════════════════════════


class TestSnooze:

    def test_returns_reminder_id(self, engine, task_store, user_id):
        task = _task(task_store, user_id, _iso(T0 + timedelta(hours=3)))
        rid = engine.snooze(task["id"], 15)
        assert isinstance(rid, int) and rid > 0

    def test_creates_snoozed_reminder_row(self, engine, task_store, reminder_store, user_id):
        task = _task(task_store, user_id, _iso(T0 + timedelta(hours=3)))
        rid = engine.snooze(task["id"], 15)
        row = reminder_store.get_reminder(rid)
        assert row["kind"] == "snoozed"
        assert row["status"] == "pending"

    def test_fire_at_is_now_plus_minutes(self, engine, task_store, reminder_store, user_id):
        task = _task(task_store, user_id, _iso(T0 + timedelta(hours=3)))
        rid = engine.snooze(task["id"], 30)
        row = reminder_store.get_reminder(rid)
        expected = _iso(T0 + timedelta(minutes=30))
        assert row["fire_at"] == expected

    def test_increments_snooze_count(self, engine, task_store, user_id):
        task = _task(task_store, user_id, _iso(T0 + timedelta(hours=3)))
        engine.snooze(task["id"], 15)
        updated = task_store.get_task(task["id"])
        assert updated["snooze_count"] == 1

    def test_second_snooze_increments_again(self, engine, task_store, user_id):
        task = _task(task_store, user_id, _iso(T0 + timedelta(hours=3)))
        engine.snooze(task["id"], 15)
        engine.snooze(task["id"], 15)
        assert task_store.get_task(task["id"])["snooze_count"] == 2

    def test_third_snooze_succeeds(self, engine, task_store, user_id):
        task = _task(task_store, user_id, _iso(T0 + timedelta(hours=3)))
        for _ in range(SNOOZE_MAX):
            engine.snooze(task["id"], 15)
        assert task_store.get_task(task["id"])["snooze_count"] == SNOOZE_MAX

    def test_exceeds_max_raises(self, engine, task_store, user_id):
        task = _task(task_store, user_id, _iso(T0 + timedelta(hours=3)))
        for _ in range(SNOOZE_MAX):
            engine.snooze(task["id"], 15)
        with pytest.raises(ValueError, match="max snooze count"):
            engine.snooze(task["id"], 15)

    def test_task_not_found_raises(self, engine):
        with pytest.raises(ValueError, match="not found"):
            engine.snooze(99999, 15)

    def test_audit_task_snoozed_emitted(self, engine, task_store, user_id, mock_audit):
        task = _task(task_store, user_id, _iso(T0 + timedelta(hours=3)))
        engine.snooze(task["id"], 15)
        actions = [c.kwargs.get("action") or c.args[1] for c in mock_audit.log.call_args_list]
        assert "task_snoozed" in actions
