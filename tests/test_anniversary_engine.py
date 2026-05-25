"""Tests for AnniversaryEngine — FR-8."""
from datetime import date, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from anniversary_engine import AnniversaryEngine
from anniversary_store import SqliteAnniversaryStore
from timeutils import VIETNAM_TZ


@pytest.fixture()
def anniv_store(db_conn):
    return SqliteAnniversaryStore(conn=db_conn)


@pytest.fixture()
def mock_notif():
    notif = MagicMock()
    notif.enqueue = MagicMock()
    return notif


@pytest.fixture()
def mock_audit():
    audit = MagicMock()
    audit.log = MagicMock()
    return audit


def _make_engine(db_conn, anniv_store, store, notif, audit, now):
    return AnniversaryEngine(
        anniv_store=anniv_store,
        user_store=store,
        notification_service=notif,
        audit=audit,
        conn=db_conn,
        now_fn=lambda: now,
    )


# ── compute_year ──────────────────────────────────────────────────────────────


def test_compute_year_inserts_row_per_offset(
    db_conn, anniv_store, store, member_user, mock_notif, mock_audit,
):
    """Default offsets '30,15,7,3,1,0' → 6 rows per anniversary."""
    anniv_store.create_anniversary(
        user_id=member_user.id, name="Giỗ ông", date_type="solar",
        month=6, day=15,
    )
    now = datetime(2025, 1, 1, 0, 1, tzinfo=VIETNAM_TZ)
    engine = _make_engine(db_conn, anniv_store, store, mock_notif, mock_audit, now)
    inserted = engine.compute_year(2025)
    assert inserted == 6
    rows = db_conn.execute(
        "SELECT * FROM anniversary_reminders WHERE year = 2025"
    ).fetchall()
    assert len(rows) == 6
    offsets = sorted(r["offset_days"] for r in rows)
    assert offsets == [0, 1, 3, 7, 15, 30]


def test_compute_year_is_idempotent(
    db_conn, anniv_store, store, member_user, mock_notif, mock_audit,
):
    anniv_store.create_anniversary(
        user_id=member_user.id, name="A", date_type="solar", month=6, day=15,
    )
    now = datetime(2025, 1, 1, 0, 1, tzinfo=VIETNAM_TZ)
    engine = _make_engine(db_conn, anniv_store, store, mock_notif, mock_audit, now)
    engine.compute_year(2025)
    second = engine.compute_year(2025)
    assert second == 0  # no new rows
    rows = db_conn.execute(
        "SELECT * FROM anniversary_reminders WHERE year = 2025"
    ).fetchall()
    assert len(rows) == 6


def test_compute_year_skips_disabled(
    db_conn, anniv_store, store, member_user, mock_notif, mock_audit,
):
    a = anniv_store.create_anniversary(
        user_id=member_user.id, name="A", date_type="solar", month=6, day=15,
    )
    anniv_store.update_anniversary(a["id"], enabled=0)
    now = datetime(2025, 1, 1, 0, 1, tzinfo=VIETNAM_TZ)
    engine = _make_engine(db_conn, anniv_store, store, mock_notif, mock_audit, now)
    assert engine.compute_year(2025) == 0


def test_compute_year_fire_at_solar_anniversary(
    db_conn, anniv_store, store, member_user, mock_notif, mock_audit,
):
    """For offset 0 (the day itself), fire_at should be 08:00 of the anniversary day."""
    anniv_store.create_anniversary(
        user_id=member_user.id, name="A", date_type="solar", month=6, day=15,
        reminder_offsets="0",
    )
    now = datetime(2025, 1, 1, 0, 1, tzinfo=VIETNAM_TZ)
    engine = _make_engine(db_conn, anniv_store, store, mock_notif, mock_audit, now)
    engine.compute_year(2025)
    row = db_conn.execute(
        "SELECT * FROM anniversary_reminders WHERE year = 2025"
    ).fetchone()
    fire_at = datetime.fromisoformat(row["fire_at"])
    assert fire_at.date() == date(2025, 6, 15)
    assert fire_at.hour == 8


def test_compute_year_fire_at_offset_3_days_before(
    db_conn, anniv_store, store, member_user, mock_notif, mock_audit,
):
    anniv_store.create_anniversary(
        user_id=member_user.id, name="A", date_type="solar", month=6, day=15,
        reminder_offsets="3",
    )
    now = datetime(2025, 1, 1, 0, 1, tzinfo=VIETNAM_TZ)
    engine = _make_engine(db_conn, anniv_store, store, mock_notif, mock_audit, now)
    engine.compute_year(2025)
    row = db_conn.execute(
        "SELECT * FROM anniversary_reminders WHERE year = 2025"
    ).fetchone()
    fire_at = datetime.fromisoformat(row["fire_at"])
    assert fire_at.date() == date(2025, 6, 12)  # 3 days before


def test_compute_year_lunar_recomputes_solar(
    db_conn, anniv_store, store, member_user, mock_notif, mock_audit,
):
    """Lunar 1/1 maps to different solar dates each year."""
    anniv_store.create_anniversary(
        user_id=member_user.id, name="Tet", date_type="lunar", month=1, day=1,
        reminder_offsets="0",
    )
    now = datetime(2024, 1, 1, 0, 1, tzinfo=VIETNAM_TZ)
    engine = _make_engine(db_conn, anniv_store, store, mock_notif, mock_audit, now)
    engine.compute_year(2024)
    engine.compute_year(2025)
    rows = db_conn.execute(
        "SELECT * FROM anniversary_reminders ORDER BY year"
    ).fetchall()
    assert len(rows) == 2
    d1 = datetime.fromisoformat(rows[0]["fire_at"]).date()
    d2 = datetime.fromisoformat(rows[1]["fire_at"]).date()
    assert d1 == date(2024, 2, 10)  # Tet 2024
    assert d2 == date(2025, 1, 29)  # Tet 2025


# ── tick() ────────────────────────────────────────────────────────────────────


def test_tick_fires_due_reminder(
    db_conn, anniv_store, store, member_user, mock_notif, mock_audit,
):
    anniv_store.create_anniversary(
        user_id=member_user.id, name="A", date_type="solar", month=6, day=15,
        reminder_offsets="0",
    )
    compute_now = datetime(2025, 1, 1, 0, 1, tzinfo=VIETNAM_TZ)
    engine = _make_engine(db_conn, anniv_store, store, mock_notif, mock_audit, compute_now)
    engine.compute_year(2025)

    # Time travel to 08:01 of the anniversary day.
    fire_time = datetime(2025, 6, 15, 8, 1, tzinfo=VIETNAM_TZ)
    engine = _make_engine(db_conn, anniv_store, store, mock_notif, mock_audit, fire_time)
    stats = engine.tick()
    assert stats["fired"] == 1
    assert stats["missed"] == 0
    mock_notif.enqueue.assert_called_once()
    args = mock_notif.enqueue.call_args
    assert args.args[0] == member_user.id
    assert args.args[1] == "telegram"


def test_tick_marks_overdue_as_missed(
    db_conn, anniv_store, store, member_user, mock_notif, mock_audit,
):
    """Reminders overdue by > 12h are marked missed, not fired."""
    anniv_store.create_anniversary(
        user_id=member_user.id, name="A", date_type="solar", month=6, day=15,
        reminder_offsets="0",
    )
    engine = _make_engine(
        db_conn, anniv_store, store, mock_notif, mock_audit,
        datetime(2025, 1, 1, 0, 1, tzinfo=VIETNAM_TZ),
    )
    engine.compute_year(2025)
    # 24h late
    late = datetime(2025, 6, 16, 8, 1, tzinfo=VIETNAM_TZ)
    engine = _make_engine(db_conn, anniv_store, store, mock_notif, mock_audit, late)
    stats = engine.tick()
    assert stats["fired"] == 0
    assert stats["missed"] == 1
    mock_notif.enqueue.assert_not_called()


def test_tick_skips_future_reminders(
    db_conn, anniv_store, store, member_user, mock_notif, mock_audit,
):
    anniv_store.create_anniversary(
        user_id=member_user.id, name="A", date_type="solar", month=6, day=15,
        reminder_offsets="0",
    )
    engine = _make_engine(
        db_conn, anniv_store, store, mock_notif, mock_audit,
        datetime(2025, 1, 1, 0, 1, tzinfo=VIETNAM_TZ),
    )
    engine.compute_year(2025)
    # Before fire_at
    early = datetime(2025, 6, 14, 0, 0, tzinfo=VIETNAM_TZ)
    engine = _make_engine(db_conn, anniv_store, store, mock_notif, mock_audit, early)
    stats = engine.tick()
    assert stats == {"fired": 0, "missed": 0}


def test_tick_skips_disabled_anniversary(
    db_conn, anniv_store, store, member_user, mock_notif, mock_audit,
):
    a = anniv_store.create_anniversary(
        user_id=member_user.id, name="A", date_type="solar", month=6, day=15,
        reminder_offsets="0",
    )
    engine = _make_engine(
        db_conn, anniv_store, store, mock_notif, mock_audit,
        datetime(2025, 1, 1, 0, 1, tzinfo=VIETNAM_TZ),
    )
    engine.compute_year(2025)
    # Disable after compute — should not fire even if due.
    anniv_store.update_anniversary(a["id"], enabled=0)
    fire_time = datetime(2025, 6, 15, 8, 1, tzinfo=VIETNAM_TZ)
    engine = _make_engine(db_conn, anniv_store, store, mock_notif, mock_audit, fire_time)
    stats = engine.tick()
    assert stats["fired"] == 0
    mock_notif.enqueue.assert_not_called()


def test_tick_skips_deleted_anniversary(
    db_conn, anniv_store, store, member_user, mock_notif, mock_audit,
):
    a = anniv_store.create_anniversary(
        user_id=member_user.id, name="A", date_type="solar", month=6, day=15,
        reminder_offsets="0",
    )
    engine = _make_engine(
        db_conn, anniv_store, store, mock_notif, mock_audit,
        datetime(2025, 1, 1, 0, 1, tzinfo=VIETNAM_TZ),
    )
    engine.compute_year(2025)
    anniv_store.soft_delete_anniversary(a["id"])
    engine = _make_engine(
        db_conn, anniv_store, store, mock_notif, mock_audit,
        datetime(2025, 6, 15, 8, 1, tzinfo=VIETNAM_TZ),
    )
    stats = engine.tick()
    assert stats["fired"] == 0


# ── cancel_all_for_anniversary ────────────────────────────────────────────────


def test_cancel_all_for_anniversary(
    db_conn, anniv_store, store, member_user, mock_notif, mock_audit,
):
    a = anniv_store.create_anniversary(
        user_id=member_user.id, name="A", date_type="solar", month=6, day=15,
    )
    engine = _make_engine(
        db_conn, anniv_store, store, mock_notif, mock_audit,
        datetime(2025, 1, 1, 0, 1, tzinfo=VIETNAM_TZ),
    )
    engine.compute_year(2025)
    cancelled = engine.cancel_all_for_anniversary(a["id"])
    assert cancelled == 6
    pending = db_conn.execute(
        "SELECT COUNT(*) FROM anniversary_reminders "
        "WHERE anniversary_id = ? AND status = 'pending'",
        (a["id"],),
    ).fetchone()[0]
    assert pending == 0


# ── parent mirror (under-18) ──────────────────────────────────────────────────


def test_compute_year_passes_is_leap_month(
    db_conn, anniv_store, store, member_user, mock_notif, mock_audit,
):
    """is_leap_month=1 stored on anniversary must be passed to compute_anniversary_solar_date.
    We verify indirectly: a leap-month anniversary in a year WITH that leap month
    produces a fire_at different from is_leap_month=0 on the same date.
    """
    # 2023 has lunar leap month 2. Create two anniversaries — one leap, one regular.
    a_regular = anniv_store.create_anniversary(
        user_id=member_user.id, name="Regular", date_type="lunar",
        month=2, day=15, reminder_offsets="0", is_leap_month=0,
    )
    a_leap = anniv_store.create_anniversary(
        user_id=member_user.id, name="Leap", date_type="lunar",
        month=2, day=15, reminder_offsets="0", is_leap_month=1,
    )
    now = datetime(2023, 1, 1, 0, 1, tzinfo=VIETNAM_TZ)
    engine = _make_engine(db_conn, anniv_store, store, mock_notif, mock_audit, now)
    engine.compute_year(2023)

    rows = db_conn.execute(
        "SELECT ar.fire_at, a.name FROM anniversary_reminders ar "
        "JOIN anniversaries a ON a.id = ar.anniversary_id WHERE ar.year = 2023"
    ).fetchall()
    fire_dates = {r["name"]: r["fire_at"] for r in rows}
    assert fire_dates["Regular"] != fire_dates["Leap"]


def test_tick_payload_includes_day_of_week(
    db_conn, anniv_store, store, member_user, mock_notif, mock_audit,
):
    """Notification payload text must include Vietnamese day-of-week and solar date."""
    anniv_store.create_anniversary(
        user_id=member_user.id, name="Giỗ ông", date_type="solar",
        month=6, day=15, reminder_offsets="0",
    )
    now = datetime(2025, 1, 1, 0, 1, tzinfo=VIETNAM_TZ)
    engine = _make_engine(db_conn, anniv_store, store, mock_notif, mock_audit, now)
    engine.compute_year(2025)

    fire_time = datetime(2025, 6, 15, 8, 1, tzinfo=VIETNAM_TZ)
    engine = _make_engine(db_conn, anniv_store, store, mock_notif, mock_audit, fire_time)
    engine.tick()

    payload = mock_notif.enqueue.call_args.args[2]
    # Text must mention the day name — 2025-06-15 is Chủ nhật.
    assert "Chủ nhật" in payload["text"]
    # Text must mention the solar date in DD/MM/YYYY format.
    assert "15/06/2025" in payload["text"]


def test_tick_mirrors_to_parent_when_owner_under_18(
    db_conn, anniv_store, store, mock_notif, mock_audit,
):
    today = date(2025, 6, 15)
    parent = store.create_user(name="Parent", role="member")
    child = store.create_user(
        name="Child", role="member",
        birthdate=date(today.year - 10, 1, 1),  # 10 years old
    )
    store.set_parent(user_id=child.id, parent_id=parent.id, set_by=parent.id)

    anniv_store.create_anniversary(
        user_id=child.id, name="A", date_type="solar", month=6, day=15,
        reminder_offsets="0",
    )
    engine = _make_engine(
        db_conn, anniv_store, store, mock_notif, mock_audit,
        datetime(2025, 1, 1, 0, 1, tzinfo=VIETNAM_TZ),
    )
    engine.compute_year(2025)
    engine = _make_engine(
        db_conn, anniv_store, store, mock_notif, mock_audit,
        datetime(2025, 6, 15, 8, 1, tzinfo=VIETNAM_TZ),
    )
    engine.tick()
    # Two enqueue calls: child + parent
    assert mock_notif.enqueue.call_count == 2
    target_ids = [c.args[0] for c in mock_notif.enqueue.call_args_list]
    assert child.id in target_ids
    assert parent.id in target_ids
