"""Tests for FR-4 sub-4.2 — stealth-read audit emission in core_handler.

These exercise the two ACL filter helpers in `core_handler` that audit-emit
when stealth-revealed rows are admitted:

  - `_acl_filter_notes(notes, viewer, deps)` — search-result filtering
  - `_visible_notes_with_meta(files, user, deps)` — `xem`/`liet ke`/`tim` paths

The two single-item handlers (`_cmd_xem_scope` note/wiki branches,
`_cmd_xem_wiki_page`) follow the identical "check `is_stealth`, log if true"
pattern; coverage there is verified by the can_read matrix in test_acl.py plus
this file's helper-level audit assertions.
"""
from __future__ import annotations

from datetime import date

import pytest

from audit import SqliteAuditLog
from deps import CoreDeps
from cmd_utils import _acl_filter_notes, _visible_notes_with_meta
from note_index import SqliteNoteIndex


# ── Fixtures ──────────────────────────────────────────────────────────────────


class FakeChannel:
    async def send(self, chat_id, text, use_markdown=True): ...
    async def delete_message(self, chat_id, message_id): return True


@pytest.fixture()
def audit(db_conn):
    return SqliteAuditLog(conn=db_conn)


@pytest.fixture()
def idx(db_conn):
    return SqliteNoteIndex(conn=db_conn)


def _today_minus_years(years: int) -> date:
    today = date.today()
    try:
        return today.replace(year=today.year - years)
    except ValueError:
        return today.replace(year=today.year - years, day=28)


def _make_deps(store, idx, audit) -> CoreDeps:
    return CoreDeps(
        llm=None,  # type: ignore[arg-type]
        notes=None,  # type: ignore[arg-type]
        wiki=None,  # type: ignore[arg-type]
        channel=FakeChannel(),  # type: ignore[arg-type]
        user_store=store,
        note_index=idx,
        memory_store=None,  # type: ignore[arg-type]
        elevation_store=None,  # type: ignore[arg-type]
        audit=audit,
    )


@pytest.fixture()
def child_with_parent(store, sample_admin, member_user):
    """A 10-year-old child of `member_user`."""
    child = store.create_user(name="Kid", role="member", birthdate=_today_minus_years(10))
    store.set_parent(user_id=child.id, parent_id=member_user.id, set_by=sample_admin.id)
    return child


@pytest.fixture()
def adult_with_parent(store, sample_admin, member_user):
    """A 25-year-old adult who happens to still have a parent_link entry."""
    adult = store.create_user(name="Grown", role="member", birthdate=_today_minus_years(25))
    store.set_parent(user_id=adult.id, parent_id=member_user.id, set_by=sample_admin.id)
    return adult


# ─────────────────────────────────────────────────────────────────────────────
# _acl_filter_notes — search-result filtering
# ─────────────────────────────────────────────────────────────────────────────


class TestAclFilterNotesAudit:

    def test_admin_search_hit_on_child_private_emits_audit(
        self, store, idx, audit, sample_admin, child_with_parent,
    ):
        idx.add_note("file-kid", child_with_parent.id, scope="private", title="Diary")
        deps = _make_deps(store, idx, audit)

        notes_in = [{"id": "file-kid", "name": "Diary.md"}]
        result = _acl_filter_notes(notes_in, sample_admin, deps)

        assert len(result) == 1
        events = audit.list_recent(action="stealth_read_note")
        assert len(events) == 1
        assert events[0].actor_user_id == sample_admin.id
        assert events[0].target_id == "file-kid"
        assert events[0].payload["owner_user_id"] == child_with_parent.id

    def test_admin_search_multiple_child_files_emits_one_audit_per_file(
        self, store, idx, audit, sample_admin, child_with_parent,
    ):
        for fid in ["f1", "f2", "f3"]:
            idx.add_note(fid, child_with_parent.id, scope="private", title=fid)
        deps = _make_deps(store, idx, audit)

        notes_in = [{"id": fid, "name": fid} for fid in ["f1", "f2", "f3"]]
        result = _acl_filter_notes(notes_in, sample_admin, deps)

        assert {n["id"] for n in result} == {"f1", "f2", "f3"}
        events = audit.list_recent(action="stealth_read_note")
        assert {e.target_id for e in events} == {"f1", "f2", "f3"}

    def test_admin_search_mixed_child_and_adult_audits_only_child(
        self, store, idx, audit, sample_admin, child_with_parent, adult_with_parent,
    ):
        idx.add_note("kid-priv",   child_with_parent.id, scope="private", title="Kid")
        idx.add_note("adult-priv", adult_with_parent.id, scope="private", title="Adult")
        idx.add_note("pub",        adult_with_parent.id, scope="everyone", title="Pub")
        deps = _make_deps(store, idx, audit)

        notes_in = [{"id": fid, "name": fid} for fid in ["kid-priv", "adult-priv", "pub"]]
        result = _acl_filter_notes(notes_in, sample_admin, deps)

        # Adult's private is blocked; child's private + public pass.
        assert {n["id"] for n in result} == {"kid-priv", "pub"}
        events = audit.list_recent(action="stealth_read_note")
        assert len(events) == 1
        assert events[0].target_id == "kid-priv"

    def test_manager_search_on_child_private_no_audit(
        self, store, idx, audit, child_with_parent,
    ):
        idx.add_note("file-kid", child_with_parent.id, scope="private")
        manager = store.create_user(name="Mgr", role="manager")
        deps = _make_deps(store, idx, audit)

        notes_in = [{"id": "file-kid", "name": "Diary.md"}]
        result = _acl_filter_notes(notes_in, manager, deps)

        # Manager doesn't get stealth access → file is filtered out, no audit.
        assert result == []
        assert audit.list_recent(action="stealth_read_note") == []

    def test_member_search_on_child_private_no_audit(
        self, store, idx, audit, child_with_parent, member_user,
    ):
        idx.add_note("file-kid", child_with_parent.id, scope="private")
        deps = _make_deps(store, idx, audit)

        notes_in = [{"id": "file-kid", "name": "Diary.md"}]
        result = _acl_filter_notes(notes_in, member_user, deps)

        assert result == []
        assert audit.list_recent(action="stealth_read_note") == []

    def test_admin_reads_own_private_no_audit(
        self, store, idx, audit, sample_admin,
    ):
        idx.add_note("self", sample_admin.id, scope="private", title="Self")
        deps = _make_deps(store, idx, audit)

        notes_in = [{"id": "self", "name": "Self.md"}]
        result = _acl_filter_notes(notes_in, sample_admin, deps)

        assert len(result) == 1
        assert audit.list_recent(action="stealth_read_note") == []

    def test_admin_reads_child_everyone_scope_no_audit(
        self, store, idx, audit, sample_admin, child_with_parent,
    ):
        idx.add_note("pub", child_with_parent.id, scope="everyone", title="Public")
        deps = _make_deps(store, idx, audit)

        notes_in = [{"id": "pub", "name": "Public.md"}]
        result = _acl_filter_notes(notes_in, sample_admin, deps)

        assert len(result) == 1
        assert audit.list_recent(action="stealth_read_note") == []

    def test_child_reads_own_private_no_audit(
        self, store, idx, audit, child_with_parent,
    ):
        idx.add_note("self", child_with_parent.id, scope="private")
        deps = _make_deps(store, idx, audit)

        notes_in = [{"id": "self", "name": "Self.md"}]
        result = _acl_filter_notes(notes_in, child_with_parent, deps)

        assert len(result) == 1
        assert audit.list_recent(action="stealth_read_note") == []

    def test_repeated_search_emits_separate_audit_rows(
        self, store, idx, audit, sample_admin, child_with_parent,
    ):
        idx.add_note("file-kid", child_with_parent.id, scope="private")
        deps = _make_deps(store, idx, audit)

        notes_in = [{"id": "file-kid", "name": "x"}]
        _acl_filter_notes(notes_in, sample_admin, deps)
        _acl_filter_notes(notes_in, sample_admin, deps)

        events = audit.list_recent(action="stealth_read_note")
        assert len(events) == 2


# ─────────────────────────────────────────────────────────────────────────────
# _visible_notes_with_meta — xem/liet ke/tim filter
# ─────────────────────────────────────────────────────────────────────────────


class TestVisibleNotesWithMetaAudit:

    def test_admin_liet_ke_marks_audit_for_each_child_file(
        self, store, idx, audit, sample_admin, child_with_parent,
    ):
        idx.add_note("k1", child_with_parent.id, scope="private", title="K1")
        idx.add_note("k2", child_with_parent.id, scope="private", title="K2")
        deps = _make_deps(store, idx, audit)

        files_in = [{"id": "k1", "name": "K1.md"}, {"id": "k2", "name": "K2.md"}]
        visible, metas = _visible_notes_with_meta(files_in, sample_admin, deps)

        assert {f["id"] for f in visible} == {"k1", "k2"}
        events = audit.list_recent(action="stealth_read_note")
        assert {e.target_id for e in events} == {"k1", "k2"}

    def test_admin_liet_ke_skips_orphan_files(
        self, store, idx, audit, sample_admin, child_with_parent,
    ):
        # Only k1 is indexed; k2 is a Drive orphan.
        idx.add_note("k1", child_with_parent.id, scope="private")
        deps = _make_deps(store, idx, audit)

        files_in = [{"id": "k1", "name": "K1"}, {"id": "k2", "name": "K2"}]
        visible, _ = _visible_notes_with_meta(files_in, sample_admin, deps)

        assert {f["id"] for f in visible} == {"k1"}
        events = audit.list_recent(action="stealth_read_note")
        assert {e.target_id for e in events} == {"k1"}

    def test_admin_liet_ke_does_not_audit_adult_or_self(
        self, store, idx, audit, sample_admin, child_with_parent, adult_with_parent,
    ):
        idx.add_note("kid",   child_with_parent.id, scope="private")
        idx.add_note("adult", adult_with_parent.id, scope="private")
        idx.add_note("self",  sample_admin.id,      scope="private")
        idx.add_note("pub",   adult_with_parent.id, scope="everyone")
        deps = _make_deps(store, idx, audit)

        files_in = [{"id": fid, "name": fid} for fid in ["kid", "adult", "self", "pub"]]
        visible, _ = _visible_notes_with_meta(files_in, sample_admin, deps)

        # adult's private blocked; others visible.
        assert {f["id"] for f in visible} == {"kid", "self", "pub"}
        events = audit.list_recent(action="stealth_read_note")
        # Only the child's private file is audited.
        assert {e.target_id for e in events} == {"kid"}

    def test_no_audit_when_visible_set_empty(
        self, store, idx, audit, sample_admin,
    ):
        deps = _make_deps(store, idx, audit)
        visible, _ = _visible_notes_with_meta([], sample_admin, deps)
        assert visible == []
        assert audit.list_recent() == []


# ─────────────────────────────────────────────────────────────────────────────
# Audit payload shape
# ─────────────────────────────────────────────────────────────────────────────


class TestAuditPayloadShape:

    def test_payload_contains_owner_user_id(
        self, store, idx, audit, sample_admin, child_with_parent,
    ):
        idx.add_note("file-kid", child_with_parent.id, scope="private")
        deps = _make_deps(store, idx, audit)

        _acl_filter_notes([{"id": "file-kid", "name": "x"}], sample_admin, deps)

        events = audit.list_recent(action="stealth_read_note")
        assert events[0].payload == {"owner_user_id": child_with_parent.id}

    def test_target_id_uses_drive_file_id(
        self, store, idx, audit, sample_admin, child_with_parent,
    ):
        idx.add_note("drive_abc_123", child_with_parent.id, scope="private")
        deps = _make_deps(store, idx, audit)

        _acl_filter_notes([{"id": "drive_abc_123", "name": "x"}], sample_admin, deps)

        events = audit.list_recent(action="stealth_read_note")
        assert events[0].target_id == "drive_abc_123"
        assert events[0].target_type == "note"
