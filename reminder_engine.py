"""reminder_engine.py — FR-7 reminder lifecycle engine.

`ReminderEngine` is the single entry point for all reminder operations:
  - `tick()`: called every minute by APScheduler; fires due reminders,
    marks overdue-by-grace as missed, and expands recurring tasks lazily.
  - `schedule_for_task()`: creates reminder rows when a task is created/edited.
  - `cancel_all_for_task()`: cancels pending reminders on task completion/deletion.
  - `snooze()`: creates a snoozed reminder row; enforces max-3 limit (D6).

Key design decisions (see docs/FR-7-PLAN.md):
  D5  — default offsets 2h/1h/30m/15m; configurable per task.
  D6  — snooze max 3 (strict); 4th request raises ValueError.
  D7  — parent mirror: runtime age check; no DB flag mutation.
  D11 — lazy recurring expansion: next occurrence computed only when the last
         pending reminder for the current occurrence fires.
  D12 — grace window 1 hour: reminders overdue by >1h are marked 'missed', not fired.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Callable

from timeutils import VIETNAM_TZ

if TYPE_CHECKING:
    from interfaces import AuditLog, NotificationService, UserStore
    from reminder_store import SqliteReminderStore
    from task_store import SqliteTaskStore

logger = logging.getLogger(__name__)

# Maximum number of snoozes allowed per task (D6, Q6).
SNOOZE_MAX = 3

# Grace window: reminders overdue by more than this are marked 'missed' (D12).
GRACE_HOURS = 1

# Weekday name → Python weekday() integer (Monday=0, Sunday=6).
_DAY_MAP: dict[str, int] = {
    "MON": 0, "TUE": 1, "WED": 2, "THU": 3, "FRI": 4, "SAT": 5, "SUN": 6,
}


# ── Pure helpers ──────────────────────────────────────────────────────────────

def parse_recurring_rule(rule: str, after_dt: datetime) -> datetime:
    """Compute the next occurrence datetime for a recurring rule string.

    Supported formats:
      ``"daily@HH:MM"``              — fires every day at HH:MM VN time.
      ``"weekly:MON,WED,FRI@HH:MM"`` — fires on the listed weekdays at HH:MM VN time.

    Returns a tz-aware datetime (VIETNAM_TZ) strictly *after* ``after_dt``.

    Raises:
        ValueError: unrecognised format, unknown weekday token, or no next occurrence
                    found within a 7-day window.
    """
    rule = rule.strip()
    after_vn = after_dt.astimezone(VIETNAM_TZ)

    if rule.startswith("daily@"):
        hh, mm = _parse_hhmm(rule[len("daily@"):])
        candidate = after_vn.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if candidate <= after_vn:
            candidate += timedelta(days=1)
        return candidate

    if rule.startswith("weekly:"):
        rest = rule[len("weekly:"):]
        if "@" not in rest:
            raise ValueError(f"Missing '@' in weekly rule: {rule!r}")
        days_part, time_part = rest.split("@", 1)
        hh, mm = _parse_hhmm(time_part)

        target_days: set[int] = set()
        for token in days_part.split(","):
            token = token.strip().upper()
            if token not in _DAY_MAP:
                raise ValueError(f"Unknown weekday token {token!r} in rule: {rule!r}")
            target_days.add(_DAY_MAP[token])

        # Scan 1–7 days ahead; the matching weekday is guaranteed in this window.
        for days_ahead in range(1, 8):
            candidate = after_vn + timedelta(days=days_ahead)
            if candidate.weekday() in target_days:
                candidate = candidate.replace(hour=hh, minute=mm, second=0, microsecond=0)
                if candidate > after_vn:
                    return candidate

        raise ValueError(f"No next occurrence found for rule: {rule!r}")

    raise ValueError(f"Unrecognised recurring rule format: {rule!r}")


# ── Engine ────────────────────────────────────────────────────────────────────

class ReminderEngine:
    """Drives the reminder lifecycle: scheduling, firing, snoozing, and recurring expansion."""

    def __init__(
        self,
        task_store: "SqliteTaskStore",
        reminder_store: "SqliteReminderStore",
        user_store: "UserStore",
        notification_service: "NotificationService",
        audit: "AuditLog",
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self._tasks = task_store
        self._reminders = reminder_store
        self._users = user_store
        self._notif = notification_service
        self._audit = audit
        self._now_fn: Callable[[], datetime] = now_fn or (
            lambda: datetime.now(VIETNAM_TZ)
        )

    # ── Public API ────────────────────────────────────────────────────────────

    def tick(self) -> dict:
        """Process all reminders due now. Called every minute by APScheduler.

        For each ready row:
          - If overdue by > 1h (grace window expired, D12) → mark 'missed' + audit.
          - If task is no longer pending → mark 'missed' (silent; reminders should
            have been cancelled on task state change, this is the safety net).
          - Otherwise → emit notification, mark 'fired'; if it was the last pending
            reminder for a recurring task, expand the next occurrence (D11).

        Returns:
            dict with keys ``fired``, ``missed``, ``recurring_expanded``.
        """
        now = self._now_fn()
        now_iso = _iso(now)
        grace_cutoff = now - timedelta(hours=GRACE_HOURS)
        stats = {"fired": 0, "missed": 0, "recurring_expanded": 0}

        for row in self._reminders.list_ready_to_fire(now_iso):
            fire_at = _parse_iso(row["fire_at"])

            # Grace window check (D12).
            if fire_at < grace_cutoff:
                self._reminders.mark_missed(row["id"])
                self._audit.log(
                    actor_user_id=None,
                    action="reminder_missed",
                    target_type="task",
                    target_id=row["task_id"],
                    payload={
                        "fire_at": row["fire_at"],
                        "missed_seconds": int((now - fire_at).total_seconds()),
                    },
                )
                stats["missed"] += 1
                continue

            # Safety net: task is no longer pending.
            task_status = row.get("task_status")
            if task_status is not None and task_status != "pending":
                self._reminders.mark_missed(row["id"])
                stats["missed"] += 1
                continue

            # Resolve owner for notification routing and age check.
            owner = self._users.get_user_by_id(row["user_id"])
            if owner is None:
                logger.warning(
                    "tick: owner user_id=%s not found for reminder %s — skipping",
                    row["user_id"], row["id"],
                )
                self._reminders.mark_missed(row["id"])
                stats["missed"] += 1
                continue

            # Fetch full task row (needed for _emit title/deadline and recurring fields).
            task = self._tasks.get_task(row["task_id"])
            self._emit(row, task, owner)
            self._reminders.mark_fired(row["id"], fired_at=now_iso)
            stats["fired"] += 1

            # Lazy recurring expansion (D11): expand only when the last pending
            # reminder for this occurrence fires.
            recurring_rule = row.get("recurring_rule")
            if recurring_rule and row.get("kind") == "scheduled":
                remaining = self._reminders.count_pending_for_task(row["task_id"])
                if remaining == 0 and task is not None:
                    if self._expand_recurring(task):
                        stats["recurring_expanded"] += 1

        return stats

    def schedule_for_task(self, task: dict) -> list[int]:
        """Create reminder rows for a newly created or updated task.

        Parses ``task["reminder_offsets"]`` (CSV of seconds) and inserts one
        row per offset into ``task_reminders``.

        Returns list of new reminder ids.
        """
        offsets = _parse_offsets(task["reminder_offsets"])
        rows = self._reminders.bulk_create_for_task(
            task["id"], task["deadline"], offsets
        )
        return [r["id"] for r in rows]

    def cancel_all_for_task(self, task_id: int) -> int:
        """Cancel all pending reminders for a task. Returns count of rows cancelled."""
        return self._reminders.cancel_for_task(task_id)

    def snooze(self, task_id: int, minutes: int) -> int:
        """Create a snoozed reminder at now + minutes. Returns new reminder id.

        Increments ``tasks.snooze_count`` and emits a ``task_snoozed`` audit row.

        Raises:
            ValueError: task not found, or ``snooze_count`` already at ``SNOOZE_MAX`` (3).
        """
        task = self._tasks.get_task(task_id)
        if task is None:
            raise ValueError(f"snooze: task {task_id} not found")
        if task["snooze_count"] >= SNOOZE_MAX:
            raise ValueError(
                f"snooze: task {task_id} has reached max snooze count ({SNOOZE_MAX})"
            )

        self._tasks.increment_snooze(task_id)
        fire_at_iso = _iso(self._now_fn() + timedelta(minutes=minutes))

        self._audit.log(
            actor_user_id=task["user_id"],
            action="task_snoozed",
            target_type="task",
            target_id=task_id,
            payload={
                "snooze_minutes": minutes,
                "new_fire_at": fire_at_iso,
                "snooze_count": task["snooze_count"] + 1,
            },
        )

        reminder = self._reminders.create_snoozed(task_id, fire_at_iso)
        return reminder["id"]

    # ── Internal ──────────────────────────────────────────────────────────────

    def _emit(self, reminder: dict, task: dict | None, owner) -> None:
        """Enqueue notification for owner; mirror to parent if owner under-18 (D7).

        Emits a ``reminder_fired`` audit row with delivery metadata.
        """
        if task is None:
            return

        text = _build_reminder_text(task, reminder)
        payload: dict = {
            "kind": "reminder",
            "task_id": task["id"],
            "text": text,
            "offset_seconds": reminder.get("offset_seconds", 0),
        }

        # Notification to the task owner.
        self._notif.enqueue(owner.id, "telegram", payload)
        mirrored_to: list[int] = []

        # Parent mirror — runtime age check only; no DB flag (D7, Decision #22).
        if owner.birthdate is not None:
            today_vn = self._now_fn().astimezone(VIETNAM_TZ).date()
            age = _age_in_years(owner.birthdate, today_vn)
            if age < 18:
                try:
                    parent = self._users.get_parent(owner.id)
                except Exception:
                    parent = None
                if parent is not None:
                    mirror_payload = {**payload, "mirrored_from_user_id": owner.id}
                    self._notif.enqueue(parent.id, "telegram", mirror_payload)
                    mirrored_to.append(parent.id)

        self._audit.log(
            actor_user_id=None,
            action="reminder_fired",
            target_type="task",
            target_id=task["id"],
            payload={
                "offset_seconds": reminder.get("offset_seconds", 0),
                "channels_delivered": ["telegram"],
                "mirrored_to_parent": mirrored_to,
            },
        )

    def _expand_recurring(self, task: dict) -> bool:
        """Compute the next occurrence deadline and insert new reminder rows.

        Returns True on success, False if expansion fails (logs the exception).
        """
        try:
            deadline_dt = _parse_iso(task["deadline"])
            next_deadline = parse_recurring_rule(
                task["recurring_rule"], after_dt=deadline_dt
            )
            next_iso = _iso(next_deadline)
            offsets = _parse_offsets(task.get("reminder_offsets", "7200,3600,1800,900"))
            self._reminders.bulk_create_for_task(task["id"], next_iso, offsets)
            self._tasks.update_task(task["id"], deadline=next_iso)
            logger.info(
                "_expand_recurring: task %s expanded to next deadline %s",
                task["id"], next_iso,
            )
            return True
        except Exception:
            logger.exception("_expand_recurring: failed for task %s", task.get("id"))
            return False


# ── Private helpers ───────────────────────────────────────────────────────────

def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _parse_iso(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=VIETNAM_TZ)
    return dt


def _parse_hhmm(s: str) -> tuple[int, int]:
    parts = s.strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"Invalid HH:MM string: {s!r}")
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        raise ValueError(f"Invalid HH:MM string: {s!r}")


def _parse_offsets(offsets_csv: str) -> list[int]:
    return [int(x.strip()) for x in offsets_csv.split(",") if x.strip()]


def _age_in_years(birthdate: date, today: date) -> int:
    years = today.year - birthdate.year
    if (today.month, today.day) < (birthdate.month, birthdate.day):
        years -= 1
    return years


def _build_reminder_text(task: dict, reminder: dict) -> str:
    title = task.get("title", "Task")
    offset = reminder.get("offset_seconds", 0)
    kind = reminder.get("kind", "scheduled")

    if kind == "snoozed":
        return f"⏰ Nhắc lại: {title}"
    if offset == 0:
        return f"⏰ Đã đến giờ: {title}"
    if offset < 3600:
        return f"⏰ {title} — còn {offset // 60} phút"
    return f"⏰ {title} — còn {offset // 3600} giờ"
