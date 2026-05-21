"""Tests for FR-4 sub-4.4 — scheduled purge jobs.

Covers:
  - UserStore.find_users_turning_18 boundary + edge cases
  - UserStore.list_deleted_users(older_than=...) filter
  - scheduled_jobs.purge_recycle_bin_180d (notes, wiki, users)
  - scheduled_jobs.purge_children_turning_18
  - scheduled_jobs.register_jobs

Drive adapters are stubbed with a recording fake. All "now" parameters are
injected explicitly so tests don't depend on real clock.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

import scheduled_jobs
from audit import SqliteAuditLog
from core_handler import CoreDeps
from note_index import SqliteNoteIndex


VN_TZ = scheduled_jobs.VN_TZ


# ── Fixtures ──────────────────────────────────────────────────────────────────


class FakeDrive:
    """Records delete_file calls. `succeed` controls all returns."""

    def __init__(self, succeed: bool = True) -> None:
        self.succeed = succeed
        self.deleted_ids: list[str] = []

    def delete_file(self, file_id: str) -> bool:
        self.deleted_ids.append(file_id)
        return self.succeed


@pytest.fixture()
def audit(db_conn):
    return SqliteAuditLog(conn=db_conn)


@pytest.fixture()
def idx(db_conn):
    return SqliteNoteIndex(conn=db_conn)


def _make_deps(store, idx, audit, notes=None, wiki=None) -> CoreDeps:
    return CoreDeps(
        llm=None,  # type: ignore[arg-type]
        notes=notes or FakeDrive(succeed=True),
        wiki=wiki or FakeDrive(succeed=True),
        channel=None,  # type: ignore[arg-type]
        user_store=store,
        note_index=idx,
        memory_store=None,  # type: ignore[arg-type]
        elevation_store=None,  # type: ignore[arg-type]
        audit=audit,
    )


def _bd_today_minus_years(years: int, today: date | None = None) -> date:
    today = today or date.today()
    try:
        return today.replace(year=today.year - years)
    except ValueError:
        return today.replace(year=today.year - years, day=28)


def _set_user_deleted_at(store, user_id: int, iso_ts: str) -> None:
    """Override `users.deleted_at` to a specific timestamp."""
    store._conn.execute(
        "UPDATE users SET deleted_at = ? WHERE id = ?", (iso_ts, user_id),
    )
    store._conn.commit()


def _set_note_deleted_at(idx, note_id: int, iso_ts: str) -> None:
    idx._conn.execute(
        "UPDATE notes SET deleted_at = ? WHERE id = ?", (iso_ts, note_id),
    )
    idx._conn.commit()


def _set_wiki_deleted_at(idx, wiki_id: int, iso_ts: str) -> None:
    idx._conn.execute(
        "UPDATE wiki_pages SET deleted_at = ? WHERE id = ?", (iso_ts, wiki_id),
    )
    idx._conn.commit()


# ═════════════════════════════════════════════════════════════════════════════
# find_users_turning_18
# ═════════════════════════════════════════════════════════════════════════════


class TestFindUsersTurning18:

    def test_returns_empty_when_no_birthdates(self, store, sample_admin):
        assert store.find_users_turning_18(date(2026, 5, 21)) == []

    def test_finds_user_whose_18th_birthday_is_on_date(self, store):
        # Born 2008-05-21 → turns 18 on 2026-05-21.
        u = store.create_user(name="Kid", role="member", birthdate=date(2008, 5, 21))
        found = store.find_users_turning_18(date(2026, 5, 21))
        assert [x.id for x in found] == [u.id]

    def test_excludes_day_before_18th_birthday(self, store):
        store.create_user(name="Kid", role="member", birthdate=date(2008, 5, 21))
        # On May 20, they're still 17 — not turning 18 today.
        assert store.find_users_turning_18(date(2026, 5, 20)) == []

    def test_excludes_day_after_18th_birthday(self, store):
        store.create_user(name="Kid", role="member", birthdate=date(2008, 5, 21))
        # On May 22, they turned 18 yesterday → not today.
        assert store.find_users_turning_18(date(2026, 5, 22)) == []

    def test_excludes_users_already_19(self, store):
        store.create_user(name="Older", role="member", birthdate=date(2007, 5, 21))
        assert store.find_users_turning_18(date(2026, 5, 21)) == []

    def test_excludes_users_with_no_birthdate(self, store):
        store.create_user(name="Unknown", role="member", birthdate=None)
        assert store.find_users_turning_18(date(2026, 5, 21)) == []

    def test_excludes_soft_deleted_users(self, store):
        u = store.create_user(name="Gone", role="member", birthdate=date(2008, 5, 21))
        store.soft_delete_user(u.id)
        assert store.find_users_turning_18(date(2026, 5, 21)) == []

    def test_multiple_matches_same_day(self, store):
        a = store.create_user(name="A", role="member", birthdate=date(2008, 5, 21))
        b = store.create_user(name="B", role="member", birthdate=date(2008, 5, 21))
        store.create_user(name="C", role="member", birthdate=date(2008, 5, 22))  # too young
        found = store.find_users_turning_18(date(2026, 5, 21))
        assert {x.id for x in found} == {a.id, b.id}

    def test_feb29_birthday_lands_on_mar1_in_non_leap_year(self, store):
        """Feb-29 child turns 18 on Mar 1 in a non-leap year (consistent with _age_in_years)."""
        store.create_user(name="LeapKid", role="member", birthdate=date(2008, 2, 29))
        # 2026 is not a leap year; turning 18 is detected on Mar 1, 2026.
        found = store.find_users_turning_18(date(2026, 3, 1))
        assert len(found) == 1
        # And NOT on Feb 28.
        assert store.find_users_turning_18(date(2026, 2, 28)) == []


# ═════════════════════════════════════════════════════════════════════════════
# list_deleted_users(older_than=...)
# ═════════════════════════════════════════════════════════════════════════════


class TestListDeletedUsersOlderThan:

    def test_no_filter_returns_all_deleted(self, store):
        u = store.create_user(name="A", role="member")
        store.soft_delete_user(u.id)
        assert len(store.list_deleted_users()) == 1

    def test_older_than_excludes_recent(self, store):
        u = store.create_user(name="Recent", role="member")
        store.soft_delete_user(u.id)
        # Threshold = 200 days ago → no rows older than that.
        threshold = (datetime.now(timezone.utc) - timedelta(days=200)).strftime("%Y-%m-%d %H:%M:%S")
        assert store.list_deleted_users(older_than=threshold) == []

    def test_older_than_includes_ancient(self, store):
        u = store.create_user(name="Ancient", role="member")
        store.soft_delete_user(u.id)
        _set_user_deleted_at(store, u.id, "2000-01-01 00:00:00")
        threshold = (datetime.now(timezone.utc) - timedelta(days=200)).strftime("%Y-%m-%d %H:%M:%S")
        result = store.list_deleted_users(older_than=threshold)
        assert [x.id for x in result] == [u.id]


# ═════════════════════════════════════════════════════════════════════════════
# purge_recycle_bin_180d — notes
# ═════════════════════════════════════════════════════════════════════════════


class TestPurgeRecycleBin180dNotes:

    def test_purges_old_note(self, store, idx, audit, sample_admin):
        nid = idx.add_note("file-old", sample_admin.id, title="Old")
        idx.soft_delete_note(nid)
        _set_note_deleted_at(idx, nid, "2000-01-01T00:00:00Z")
        deps = _make_deps(store, idx, audit)

        summary = scheduled_jobs.purge_recycle_bin_180d(deps)

        assert summary["notes_purged"] == 1
        # Row gone from DB.
        assert idx.list_deleted_notes() == []
        # Audit row written with system actor.
        events = audit.list_recent(action="recycle_purge", target_type="note")
        assert len(events) == 1
        assert events[0].actor_user_id is None
        assert events[0].payload["reason"] == "180d"

    def test_skips_recently_deleted_note(self, store, idx, audit, sample_admin):
        nid = idx.add_note("file-recent", sample_admin.id)
        idx.soft_delete_note(nid)  # deleted_at = now
        deps = _make_deps(store, idx, audit)

        summary = scheduled_jobs.purge_recycle_bin_180d(deps)

        assert summary["notes_purged"] == 0
        # Row still there.
        assert len(idx.list_deleted_notes()) == 1

    def test_drive_success_recorded_in_audit(self, store, idx, audit, sample_admin):
        nid = idx.add_note("drive_x", sample_admin.id)
        idx.soft_delete_note(nid)
        _set_note_deleted_at(idx, nid, "2000-01-01T00:00:00Z")
        notes = FakeDrive(succeed=True)
        deps = _make_deps(store, idx, audit, notes=notes)

        summary = scheduled_jobs.purge_recycle_bin_180d(deps)

        assert notes.deleted_ids == ["drive_x"]
        assert summary["drive_delete_failures"] == 0
        events = audit.list_recent(action="recycle_purge")
        assert events[0].payload["drive_deleted"] is True

    def test_drive_failure_counted_but_sqlite_still_purged(
        self, store, idx, audit, sample_admin,
    ):
        nid = idx.add_note("drive_y", sample_admin.id)
        idx.soft_delete_note(nid)
        _set_note_deleted_at(idx, nid, "2000-01-01T00:00:00Z")
        notes = FakeDrive(succeed=False)
        deps = _make_deps(store, idx, audit, notes=notes)

        summary = scheduled_jobs.purge_recycle_bin_180d(deps)

        assert summary["notes_purged"] == 1
        assert summary["drive_delete_failures"] == 1
        assert idx.list_deleted_notes() == []
        events = audit.list_recent(action="recycle_purge")
        assert events[0].payload["drive_deleted"] is False

    def test_empty_bin_returns_zero_summary(self, store, idx, audit):
        deps = _make_deps(store, idx, audit)
        summary = scheduled_jobs.purge_recycle_bin_180d(deps)
        assert summary == {
            "users_purged": 0,
            "users_skipped_fk": 0,
            "notes_purged": 0,
            "wiki_purged": 0,
            "drive_delete_failures": 0,
        }
        assert audit.list_recent() == []


# ═════════════════════════════════════════════════════════════════════════════
# purge_recycle_bin_180d — wiki
# ═════════════════════════════════════════════════════════════════════════════


class TestPurgeRecycleBin180dWiki:

    def test_purges_old_wiki(self, store, idx, audit, sample_admin):
        wid = idx.add_wiki_page("drive_w", sample_admin.id, topic="T", slug="t")
        idx.soft_delete_wiki(wid)
        _set_wiki_deleted_at(idx, wid, "2000-01-01T00:00:00Z")
        wiki = FakeDrive(succeed=True)
        deps = _make_deps(store, idx, audit, wiki=wiki)

        summary = scheduled_jobs.purge_recycle_bin_180d(deps)

        assert summary["wiki_purged"] == 1
        assert wiki.deleted_ids == ["drive_w"]
        events = audit.list_recent(action="recycle_purge", target_type="wiki")
        assert events[0].payload["drive_deleted"] is True

    def test_skips_recent_wiki(self, store, idx, audit, sample_admin):
        wid = idx.add_wiki_page("recent_w", sample_admin.id, topic="T", slug="t")
        idx.soft_delete_wiki(wid)
        deps = _make_deps(store, idx, audit)

        summary = scheduled_jobs.purge_recycle_bin_180d(deps)
        assert summary["wiki_purged"] == 0

    def test_wiki_drive_failure(self, store, idx, audit, sample_admin):
        wid = idx.add_wiki_page("drive_wf", sample_admin.id, topic="T", slug="t")
        idx.soft_delete_wiki(wid)
        _set_wiki_deleted_at(idx, wid, "2000-01-01T00:00:00Z")
        wiki = FakeDrive(succeed=False)
        deps = _make_deps(store, idx, audit, wiki=wiki)

        summary = scheduled_jobs.purge_recycle_bin_180d(deps)
        assert summary["wiki_purged"] == 1
        assert summary["drive_delete_failures"] == 1


# ═════════════════════════════════════════════════════════════════════════════
# purge_recycle_bin_180d — users
# ═════════════════════════════════════════════════════════════════════════════


class TestPurgeRecycleBin180dUsers:

    def test_purges_old_user_with_no_refs(self, store, idx, audit):
        u = store.create_user(name="Lone", role="readonly")
        store.soft_delete_user(u.id)
        _set_user_deleted_at(store, u.id, "2000-01-01 00:00:00")
        deps = _make_deps(store, idx, audit)

        summary = scheduled_jobs.purge_recycle_bin_180d(deps)

        assert summary["users_purged"] == 1
        assert summary["users_skipped_fk"] == 0
        assert store.get_user_by_id(u.id) is None
        events = audit.list_recent(action="recycle_purge", target_type="user")
        assert events[0].payload["name"] == "Lone"

    def test_skips_fk_constrained_user(self, store, idx, audit, sample_admin, member_user):
        store.bind_channel(member_user.id, "telegram", "999")
        store.soft_delete_user(member_user.id)
        _set_user_deleted_at(store, member_user.id, "2000-01-01 00:00:00")
        deps = _make_deps(store, idx, audit)

        summary = scheduled_jobs.purge_recycle_bin_180d(deps)

        assert summary["users_purged"] == 0
        assert summary["users_skipped_fk"] == 1
        # User row still present.
        assert store.get_user_by_id(member_user.id) is not None
        # Audit row: purge_skipped, not recycle_purge.
        purge_events = audit.list_recent(action="recycle_purge", target_type="user")
        assert purge_events == []
        skip_events = audit.list_recent(action="purge_skipped", target_type="user")
        assert len(skip_events) == 1
        assert skip_events[0].payload["reason"] == "fk_constraint"

    def test_skips_recent_user(self, store, idx, audit):
        u = store.create_user(name="Recent", role="readonly")
        store.soft_delete_user(u.id)  # deleted_at = now
        deps = _make_deps(store, idx, audit)

        summary = scheduled_jobs.purge_recycle_bin_180d(deps)
        assert summary["users_purged"] == 0

    def test_mixed_batch_summary(self, store, idx, audit, sample_admin):
        # 1 old note, 1 recent wiki, 1 old user (no refs)
        nid = idx.add_note("old_note", sample_admin.id)
        idx.soft_delete_note(nid)
        _set_note_deleted_at(idx, nid, "2000-01-01T00:00:00Z")

        wid = idx.add_wiki_page("recent_w", sample_admin.id, topic="T", slug="t")
        idx.soft_delete_wiki(wid)  # recent

        u = store.create_user(name="OldUser", role="readonly")
        store.soft_delete_user(u.id)
        _set_user_deleted_at(store, u.id, "2000-01-01 00:00:00")

        deps = _make_deps(store, idx, audit)
        summary = scheduled_jobs.purge_recycle_bin_180d(deps)

        assert summary["notes_purged"] == 1
        assert summary["wiki_purged"] == 0
        assert summary["users_purged"] == 1


# ═════════════════════════════════════════════════════════════════════════════
# purge_children_turning_18
# ═════════════════════════════════════════════════════════════════════════════


class TestPurgeChildrenTurning18:

    def _make_child_who_turned_18_yesterday(self, store, today_utc):
        """Create a member whose 18th birthday was today_utc - 1 day (in VN_TZ)."""
        today_vn = today_utc.astimezone(VN_TZ).date()
        yesterday_vn = today_vn - timedelta(days=1)
        bd = yesterday_vn.replace(year=yesterday_vn.year - 18)
        return store.create_user(name="Just18", role="member", birthdate=bd)

    def test_purges_soft_deleted_notes_of_just_18_user(self, store, idx, audit):
        now_utc = datetime(2026, 5, 22, 3, 0, tzinfo=timezone.utc)  # 10:00 VN
        child = self._make_child_who_turned_18_yesterday(store, now_utc)

        nid = idx.add_note("file-1", child.id, title="Diary")
        idx.soft_delete_note(nid)
        deps = _make_deps(store, idx, audit)

        summary = scheduled_jobs.purge_children_turning_18(deps, now=now_utc)

        assert child.id in summary
        assert summary[child.id]["notes_purged"] == 1
        assert idx.list_soft_deleted_notes_by_owner(child.id) == []
        events = audit.list_recent(action="auto_purge_18")
        assert len(events) == 1
        assert events[0].target_id == str(child.id)

    def test_purges_soft_deleted_wiki_of_just_18_user(self, store, idx, audit):
        now_utc = datetime(2026, 5, 22, 3, 0, tzinfo=timezone.utc)
        child = self._make_child_who_turned_18_yesterday(store, now_utc)

        wid = idx.add_wiki_page("wiki-1", child.id, topic="T", slug="t")
        idx.soft_delete_wiki(wid)
        deps = _make_deps(store, idx, audit)

        summary = scheduled_jobs.purge_children_turning_18(deps, now=now_utc)

        assert summary[child.id]["wiki_purged"] == 1

    def test_does_not_touch_live_data_of_just_18_user(self, store, idx, audit):
        """Decision D5: only soft-deleted items are purged; live data is left alone."""
        now_utc = datetime(2026, 5, 22, 3, 0, tzinfo=timezone.utc)
        child = self._make_child_who_turned_18_yesterday(store, now_utc)

        nid_live = idx.add_note("live", child.id, title="Live")  # NOT soft-deleted
        deps = _make_deps(store, idx, audit)

        scheduled_jobs.purge_children_turning_18(deps, now=now_utc)

        # Live note untouched.
        assert idx.get_note_meta("live") is not None

    def test_no_matching_users_does_nothing(self, store, idx, audit):
        now_utc = datetime(2026, 5, 22, 3, 0, tzinfo=timezone.utc)
        # User who turned 19 yesterday — not matched.
        bd = date(2007, 5, 21)
        u = store.create_user(name="Older", role="member", birthdate=bd)
        nid = idx.add_note("x", u.id)
        idx.soft_delete_note(nid)
        deps = _make_deps(store, idx, audit)

        summary = scheduled_jobs.purge_children_turning_18(deps, now=now_utc)
        assert summary == {}
        # Note still in recycle bin.
        assert len(idx.list_soft_deleted_notes_by_owner(u.id)) == 1
        # No audit row.
        assert audit.list_recent(action="auto_purge_18") == []

    def test_user_with_no_soft_deleted_items_still_gets_audit(self, store, idx, audit):
        """Audit row records the event even if nothing was purged (proof of run)."""
        now_utc = datetime(2026, 5, 22, 3, 0, tzinfo=timezone.utc)
        child = self._make_child_who_turned_18_yesterday(store, now_utc)
        # No notes / wiki at all.
        deps = _make_deps(store, idx, audit)

        scheduled_jobs.purge_children_turning_18(deps, now=now_utc)

        events = audit.list_recent(action="auto_purge_18")
        assert len(events) == 1
        assert events[0].payload["notes_purged"] == 0
        assert events[0].payload["wiki_purged"] == 0

    def test_drive_failure_counted_in_payload(self, store, idx, audit):
        now_utc = datetime(2026, 5, 22, 3, 0, tzinfo=timezone.utc)
        child = self._make_child_who_turned_18_yesterday(store, now_utc)
        nid = idx.add_note("drive_xx", child.id)
        idx.soft_delete_note(nid)
        notes = FakeDrive(succeed=False)
        deps = _make_deps(store, idx, audit, notes=notes)

        scheduled_jobs.purge_children_turning_18(deps, now=now_utc)

        events = audit.list_recent(action="auto_purge_18")
        assert events[0].payload["drive_delete_failures"] == 1

    def test_multiple_children_each_get_own_audit_row(self, store, idx, audit):
        now_utc = datetime(2026, 5, 22, 3, 0, tzinfo=timezone.utc)
        a = self._make_child_who_turned_18_yesterday(store, now_utc)
        # Adjust name to avoid unique-name conflict
        store._conn.execute("UPDATE users SET name = ? WHERE id = ?", ("KidA", a.id))
        store._conn.commit()
        b = self._make_child_who_turned_18_yesterday(store, now_utc)
        store._conn.execute("UPDATE users SET name = ? WHERE id = ?", ("KidB", b.id))
        store._conn.commit()

        deps = _make_deps(store, idx, audit)
        summary = scheduled_jobs.purge_children_turning_18(deps, now=now_utc)

        assert set(summary.keys()) == {a.id, b.id}
        events = audit.list_recent(action="auto_purge_18")
        targets = {e.target_id for e in events}
        assert targets == {str(a.id), str(b.id)}


# ═════════════════════════════════════════════════════════════════════════════
# register_jobs
# ═════════════════════════════════════════════════════════════════════════════


class TestRegisterJobs:

    def test_all_jobs_registered_with_expected_ids(self, store, idx, audit):
        from apscheduler.schedulers.background import BackgroundScheduler

        scheduler = BackgroundScheduler()
        scheduler.start(paused=True)
        try:
            deps = _make_deps(store, idx, audit)
            scheduled_jobs.register_jobs(scheduler, deps)
            ids = {j.id for j in scheduler.get_jobs()}
            assert scheduled_jobs.JOB_ID_180D in ids
            assert scheduled_jobs.JOB_ID_TURN_18 in ids
            assert scheduled_jobs.JOB_ID_NOTIF_FLUSH in ids
        finally:
            scheduler.shutdown(wait=False)

    def test_register_jobs_idempotent_via_replace_existing(self, store, idx, audit):
        from apscheduler.schedulers.background import BackgroundScheduler

        scheduler = BackgroundScheduler()
        scheduler.start(paused=True)
        try:
            deps = _make_deps(store, idx, audit)
            scheduled_jobs.register_jobs(scheduler, deps)
            # Second call should not raise (replace_existing=True replaces
            # rather than duplicating).
            scheduled_jobs.register_jobs(scheduler, deps)
            assert len(scheduler.get_jobs()) == 3
        finally:
            scheduler.shutdown(wait=False)
