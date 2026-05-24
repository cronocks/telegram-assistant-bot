"""tests/test_parent_digest.py — RED tests for sub-task 7.6 Parent Digest.

All tests use lazy imports so existing tests still pass even before
send_parent_digest / list_active_parent_links are implemented.
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

import pytest

VN_TZ = timezone(timedelta(hours=7))

# ── lazy import ──────────────────────────────────────────────────────────────


def _import_send_parent_digest():
    from scheduled_jobs import send_parent_digest
    return send_parent_digest


# ── minimal fakes ────────────────────────────────────────────────────────────


@dataclass
class _FakeUser:
    id: int
    name: str = "user"
    birthdate: date | None = None


@dataclass
class _FakeLink:
    parent_id: int
    child_id: int
    digest_frequency: str = "daily"  # daily | weekly | monthly | off
    digest_time: str | None = "21:00"
    last_digest_at: str | None = None


@dataclass
class _FakeUserStore:
    links: list[_FakeLink] = field(default_factory=list)
    users: dict[int, _FakeUser] = field(default_factory=dict)
    last_digest_calls: list[tuple] = field(default_factory=list)

    def list_active_parent_links(self) -> list[dict]:
        return [
            {
                "parent_id": lk.parent_id,
                "child_id": lk.child_id,
                "digest_frequency": lk.digest_frequency,
                "digest_time": lk.digest_time,
                "last_digest_at": lk.last_digest_at,
            }
            for lk in self.links
        ]

    def get_user_by_id(self, user_id: int) -> _FakeUser | None:
        return self.users.get(user_id)

    def set_last_digest_at(self, parent_id: int, child_id: int, ts: str) -> None:
        self.last_digest_calls.append((parent_id, child_id, ts))


@dataclass
class _FakeTaskStore:
    completed: list[dict] = field(default_factory=list)
    pending: list[dict] = field(default_factory=list)

    def list_completed_on(self, user_id: int, date_str: str) -> list[dict]:
        return self.completed

    def list_pending_due(self, before_iso: str, user_id: int | None = None) -> list[dict]:
        return self.pending


@dataclass
class _FakeNotifService:
    enqueued: list[tuple] = field(default_factory=list)

    def enqueue(self, user_id: int, channel: str, payload: dict) -> None:
        self.enqueued.append((user_id, channel, payload))


@dataclass
class _FakeAudit:
    events: list[dict] = field(default_factory=list)

    def log(self, **kwargs: Any) -> None:
        self.events.append(kwargs)


@dataclass
class _FakeDeps:
    user_store: Any = None
    task_store: Any = None
    notification_service: Any = None
    audit: Any = None


def _make_deps(
    links: list[_FakeLink] | None = None,
    users: dict | None = None,
    completed: list | None = None,
    pending: list | None = None,
) -> _FakeDeps:
    ustore = _FakeUserStore(links=links or [], users=users or {})
    tstore = _FakeTaskStore(completed=completed or [], pending=pending or [])
    notif = _FakeNotifService()
    audit = _FakeAudit()
    return _FakeDeps(user_store=ustore, task_store=tstore,
                     notification_service=notif, audit=audit)


def _now_at(hour: int, minute: int, weekday: int = 6, day: int | None = None) -> datetime:
    """Return a fixed datetime in VN_TZ with specified hour/minute.
    weekday: 0=Mon … 6=Sun. day overrides the day-of-month.
    """
    base = datetime(2026, 6, 7, hour, minute, 0, tzinfo=VN_TZ)  # 2026-06-07 is a Sunday
    if weekday != 6:
        delta = (weekday - base.weekday()) % 7
        base = base + timedelta(days=delta)
    if day is not None:
        base = base.replace(day=day)
    return base


# ── 1. missing deps ──────────────────────────────────────────────────────────


def test_no_task_store_returns_empty():
    send_parent_digest = _import_send_parent_digest()
    deps = _FakeDeps(task_store=None, notification_service=_FakeNotifService(),
                     user_store=_FakeUserStore(), audit=_FakeAudit())
    result = send_parent_digest(deps, now=_now_at(21, 0))
    assert result == {"sent": 0, "skipped": 0}


def test_no_notification_service_returns_empty():
    send_parent_digest = _import_send_parent_digest()
    deps = _FakeDeps(task_store=_FakeTaskStore(), notification_service=None,
                     user_store=_FakeUserStore(), audit=_FakeAudit())
    result = send_parent_digest(deps, now=_now_at(21, 0))
    assert result == {"sent": 0, "skipped": 0}


# ── 2. no active links ───────────────────────────────────────────────────────


def test_no_active_links_returns_zero():
    send_parent_digest = _import_send_parent_digest()
    deps = _make_deps(links=[])
    result = send_parent_digest(deps, now=_now_at(21, 0))
    assert result == {"sent": 0, "skipped": 0}


# ── 3. daily matching ────────────────────────────────────────────────────────


def test_daily_time_match_enqueues():
    send_parent_digest = _import_send_parent_digest()
    child = _FakeUser(id=2, birthdate=date(2010, 1, 1))
    link = _FakeLink(parent_id=1, child_id=2, digest_frequency="daily", digest_time="21:00")
    deps = _make_deps(links=[link], users={2: child},
                      pending=[{"id": 1, "title": "Bài tập toán"}])
    result = send_parent_digest(deps, now=_now_at(21, 0))
    assert result["sent"] == 1
    assert len(deps.notification_service.enqueued) == 1


def test_daily_time_no_match_skips():
    send_parent_digest = _import_send_parent_digest()
    child = _FakeUser(id=2, birthdate=date(2010, 1, 1))
    link = _FakeLink(parent_id=1, child_id=2, digest_frequency="daily", digest_time="21:00")
    deps = _make_deps(links=[link], users={2: child},
                      pending=[{"id": 1, "title": "Bài tập toán"}])
    result = send_parent_digest(deps, now=_now_at(20, 0))
    assert result["sent"] == 0
    assert result["skipped"] == 1


# ── 4. frequency off ─────────────────────────────────────────────────────────


def test_frequency_off_skips():
    send_parent_digest = _import_send_parent_digest()
    child = _FakeUser(id=2, birthdate=date(2010, 1, 1))
    link = _FakeLink(parent_id=1, child_id=2, digest_frequency="off", digest_time="21:00")
    deps = _make_deps(links=[link], users={2: child},
                      pending=[{"id": 1, "title": "Task"}])
    result = send_parent_digest(deps, now=_now_at(21, 0))
    assert result["sent"] == 0
    assert result["skipped"] == 1


# ── 5. child >= 18 → skip ────────────────────────────────────────────────────


def test_child_18_skips_and_audits():
    send_parent_digest = _import_send_parent_digest()
    # Child born 2008-06-07 → turns 18 on 2026-06-07 (today)
    child = _FakeUser(id=2, birthdate=date(2008, 6, 7))
    link = _FakeLink(parent_id=1, child_id=2, digest_frequency="daily", digest_time="21:00")
    deps = _make_deps(links=[link], users={2: child},
                      pending=[{"id": 1, "title": "Task"}])
    result = send_parent_digest(deps, now=_now_at(21, 0))
    assert result["sent"] == 0
    assert result["skipped"] == 1
    actions = [e["action"] for e in deps.audit.events]
    assert "digest_disabled_at_18" in actions


# ── 6. weekly matching ───────────────────────────────────────────────────────


def test_weekly_day_match_enqueues():
    send_parent_digest = _import_send_parent_digest()
    child = _FakeUser(id=2, birthdate=date(2010, 1, 1))
    # "SUN 20:00" — now is Sunday 20:00
    link = _FakeLink(parent_id=1, child_id=2, digest_frequency="weekly", digest_time="SUN 20:00")
    deps = _make_deps(links=[link], users={2: child},
                      pending=[{"id": 1, "title": "Task"}])
    now = _now_at(20, 0, weekday=6)  # Sunday
    result = send_parent_digest(deps, now=now)
    assert result["sent"] == 1


def test_weekly_wrong_day_skips():
    send_parent_digest = _import_send_parent_digest()
    child = _FakeUser(id=2, birthdate=date(2010, 1, 1))
    link = _FakeLink(parent_id=1, child_id=2, digest_frequency="weekly", digest_time="SUN 20:00")
    deps = _make_deps(links=[link], users={2: child},
                      pending=[{"id": 1, "title": "Task"}])
    now = _now_at(20, 0, weekday=0)  # Monday
    result = send_parent_digest(deps, now=now)
    assert result["sent"] == 0
    assert result["skipped"] == 1


# ── 7. monthly matching ──────────────────────────────────────────────────────


def test_monthly_date_match_enqueues():
    send_parent_digest = _import_send_parent_digest()
    child = _FakeUser(id=2, birthdate=date(2010, 1, 1))
    # "1 20:00" → day 1 of month
    link = _FakeLink(parent_id=1, child_id=2, digest_frequency="monthly", digest_time="1 20:00")
    deps = _make_deps(links=[link], users={2: child},
                      pending=[{"id": 1, "title": "Task"}])
    now = _now_at(20, 0, day=1)
    result = send_parent_digest(deps, now=now)
    assert result["sent"] == 1


def test_monthly_last_day_match_enqueues():
    send_parent_digest = _import_send_parent_digest()
    child = _FakeUser(id=2, birthdate=date(2010, 1, 1))
    link = _FakeLink(parent_id=1, child_id=2, digest_frequency="monthly", digest_time="LAST 20:00")
    deps = _make_deps(links=[link], users={2: child},
                      pending=[{"id": 1, "title": "Task"}])
    # June 2026: last day = 30
    last_day = calendar.monthrange(2026, 6)[1]  # 30
    now = _now_at(20, 0, day=last_day)
    result = send_parent_digest(deps, now=now)
    assert result["sent"] == 1


def test_monthly_wrong_date_skips():
    send_parent_digest = _import_send_parent_digest()
    child = _FakeUser(id=2, birthdate=date(2010, 1, 1))
    link = _FakeLink(parent_id=1, child_id=2, digest_frequency="monthly", digest_time="1 20:00")
    deps = _make_deps(links=[link], users={2: child},
                      pending=[{"id": 1, "title": "Task"}])
    now = _now_at(20, 0, day=15)  # not day 1
    result = send_parent_digest(deps, now=now)
    assert result["sent"] == 0


# ── 8. no tasks → skip ───────────────────────────────────────────────────────


def test_no_tasks_skips():
    send_parent_digest = _import_send_parent_digest()
    child = _FakeUser(id=2, birthdate=date(2010, 1, 1))
    link = _FakeLink(parent_id=1, child_id=2, digest_frequency="daily", digest_time="21:00")
    deps = _make_deps(links=[link], users={2: child}, completed=[], pending=[])
    result = send_parent_digest(deps, now=_now_at(21, 0))
    assert result["sent"] == 0
    assert result["skipped"] == 1
    assert len(deps.notification_service.enqueued) == 0


# ── 9. message content ───────────────────────────────────────────────────────


def test_message_contains_top_3_pending():
    send_parent_digest = _import_send_parent_digest()
    child = _FakeUser(id=2, name="An", birthdate=date(2010, 1, 1))
    link = _FakeLink(parent_id=1, child_id=2, digest_frequency="daily", digest_time="21:00")
    tasks = [{"id": i, "title": f"Task {i}"} for i in range(1, 6)]  # 5 pending tasks
    deps = _make_deps(links=[link], users={2: child}, pending=tasks)
    send_parent_digest(deps, now=_now_at(21, 0))
    text = deps.notification_service.enqueued[0][2]["text"]
    assert "Task 1" in text
    assert "Task 2" in text
    assert "Task 3" in text
    assert "Task 4" not in text  # only top 3
    assert "Task 5" not in text


# ── 10. audit ────────────────────────────────────────────────────────────────


def test_audit_parent_digest_sent_logged():
    send_parent_digest = _import_send_parent_digest()
    child = _FakeUser(id=2, birthdate=date(2010, 1, 1))
    link = _FakeLink(parent_id=1, child_id=2, digest_frequency="daily", digest_time="21:00")
    deps = _make_deps(links=[link], users={2: child},
                      pending=[{"id": 1, "title": "Task"}])
    send_parent_digest(deps, now=_now_at(21, 0))
    actions = [e["action"] for e in deps.audit.events]
    assert "parent_digest_sent" in actions


# ── 11. null digest_time → default 21:00 ─────────────────────────────────────


def test_null_digest_time_uses_default():
    send_parent_digest = _import_send_parent_digest()
    child = _FakeUser(id=2, birthdate=date(2010, 1, 1))
    link = _FakeLink(parent_id=1, child_id=2, digest_frequency="daily", digest_time=None)
    deps = _make_deps(links=[link], users={2: child},
                      pending=[{"id": 1, "title": "Task"}])
    result = send_parent_digest(deps, now=_now_at(21, 0))
    assert result["sent"] == 1


# ── 12. set_last_digest_at called ────────────────────────────────────────────


def test_set_last_digest_at_called_after_send():
    send_parent_digest = _import_send_parent_digest()
    child = _FakeUser(id=2, birthdate=date(2010, 1, 1))
    link = _FakeLink(parent_id=1, child_id=2, digest_frequency="daily", digest_time="21:00")
    deps = _make_deps(links=[link], users={2: child},
                      pending=[{"id": 1, "title": "Task"}])
    send_parent_digest(deps, now=_now_at(21, 0))
    assert len(deps.user_store.last_digest_calls) == 1
    parent_id, child_id, _ = deps.user_store.last_digest_calls[0]
    assert parent_id == 1
    assert child_id == 2


# ── 13. sent/skipped counts ──────────────────────────────────────────────────


def test_mixed_links_returns_correct_counts():
    send_parent_digest = _import_send_parent_digest()
    child1 = _FakeUser(id=2, birthdate=date(2010, 1, 1))
    child2 = _FakeUser(id=3, birthdate=date(2010, 1, 1))
    links = [
        _FakeLink(parent_id=1, child_id=2, digest_frequency="daily", digest_time="21:00"),
        _FakeLink(parent_id=1, child_id=3, digest_frequency="off", digest_time="21:00"),
    ]
    ustore = _FakeUserStore(links=links, users={2: child1, 3: child2})
    tstore = _FakeTaskStore(pending=[{"id": 1, "title": "Task"}])
    deps = _FakeDeps(user_store=ustore, task_store=tstore,
                     notification_service=_FakeNotifService(), audit=_FakeAudit())
    result = send_parent_digest(deps, now=_now_at(21, 0))
    assert result == {"sent": 1, "skipped": 1}
