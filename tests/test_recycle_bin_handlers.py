"""Tests for FR-4 sub-4.3 — recycle bin handler-level behavior.

Exercises `_cmd_xem_thung_rac`, `_cmd_khoi_phuc`, `_cmd_xoa_han` with a
FakeChannel + real stores + a FakeDrive that records delete_file calls.
"""
from __future__ import annotations

import asyncio

import pytest

from audit import SqliteAuditLog
from deps import CoreDeps
from cmd_audit import (
    _cmd_khoi_phuc,
    _cmd_xem_thung_rac,
    _cmd_xoa_han,
    _parse_recycle_target,
)
from note_index import SqliteNoteIndex


class FakeChannel:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send(self, chat_id, text, use_markdown=True):
        self.sent.append((chat_id, text))

    async def delete_message(self, chat_id, message_id): return True

    @property
    def last_text(self) -> str:
        return self.sent[-1][1] if self.sent else ""


class FakeDriveAdapter:
    """Mock NoteStore / WikiStore for delete_file behavior."""

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


def _make_deps(store, idx, audit, notes_adapter=None, wiki_adapter=None) -> CoreDeps:
    return CoreDeps(
        llm=None,  # type: ignore[arg-type]
        notes=notes_adapter or FakeDriveAdapter(succeed=True),
        wiki=wiki_adapter or FakeDriveAdapter(succeed=True),
        channel=FakeChannel(),
        user_store=store,
        note_index=idx,
        memory_store=None,  # type: ignore[arg-type]
        elevation_store=None,  # type: ignore[arg-type]
        audit=audit,
    )


def _run(coro):
    return asyncio.run(coro)


# ─────────────────────────────────────────────────────────────────────────────
# Parser
# ─────────────────────────────────────────────────────────────────────────────


class TestParseRecycleTarget:

    def test_valid_user(self):
        assert _parse_recycle_target("user 3") == ("user", 3)

    def test_valid_note(self):
        assert _parse_recycle_target("note 12") == ("note", 12)

    def test_valid_wiki(self):
        assert _parse_recycle_target("wiki 5") == ("wiki", 5)

    def test_case_insensitive_kind(self):
        assert _parse_recycle_target("USER 3") == ("user", 3)

    def test_invalid_kind(self):
        assert _parse_recycle_target("foo 3") is None

    def test_missing_id(self):
        assert _parse_recycle_target("user") is None

    def test_non_numeric_id(self):
        assert _parse_recycle_target("user abc") is None

    def test_zero_id_rejected(self):
        assert _parse_recycle_target("user 0") is None

    def test_negative_rejected(self):
        # `-3` doesn't pass isdigit() → rejected.
        assert _parse_recycle_target("user -3") is None

    def test_empty(self):
        assert _parse_recycle_target("") is None


# ─────────────────────────────────────────────────────────────────────────────
# xem thung rac
# ─────────────────────────────────────────────────────────────────────────────


class TestXemThungRac:

    def test_admin_empty_recycle_bin(self, store, idx, audit, sample_admin):
        deps = _make_deps(store, idx, audit)
        _run(_cmd_xem_thung_rac("c1", sample_admin, deps))
        assert "trong" in deps.channel.last_text.lower()

    def test_non_admin_rejected_no_audit(self, store, idx, audit, member_user):
        deps = _make_deps(store, idx, audit)
        _run(_cmd_xem_thung_rac("c1", member_user, deps))
        assert "Chỉ admin" in deps.channel.last_text
        assert audit.list_recent(action="recycle_view") == []

    def test_admin_mixed_kinds_listed(self, store, idx, audit, sample_admin, member_user):
        # Seed: 1 deleted user, 2 deleted notes, 1 deleted wiki.
        u = store.create_user(name="Gone", role="readonly")
        store.soft_delete_user(u.id)
        nid1 = idx.add_note("n1", sample_admin.id, title="Note1")
        nid2 = idx.add_note("n2", sample_admin.id, title="Note2")
        idx.soft_delete_note(nid1)
        idx.soft_delete_note(nid2)
        wid = idx.add_wiki_page("w1", sample_admin.id, topic="W", slug="w")
        idx.soft_delete_wiki(wid)

        deps = _make_deps(store, idx, audit)
        _run(_cmd_xem_thung_rac("c1", sample_admin, deps))

        text = deps.channel.last_text
        assert "4 muc" in text
        assert "Note1" in text and "Note2" in text
        assert "Gone" in text
        assert f"user {u.id}" in text

    def test_audit_recycle_view_payload(self, store, idx, audit, sample_admin):
        u = store.create_user(name="Gone", role="readonly")
        store.soft_delete_user(u.id)
        nid = idx.add_note("n1", sample_admin.id)
        idx.soft_delete_note(nid)

        deps = _make_deps(store, idx, audit)
        _run(_cmd_xem_thung_rac("c1", sample_admin, deps))

        events = audit.list_recent(action="recycle_view")
        assert len(events) == 1
        assert events[0].actor_user_id == sample_admin.id
        assert events[0].payload == {"items": 2, "users": 1, "notes": 1, "wiki": 0}

    def test_empty_bin_still_emits_audit_view(self, store, idx, audit, sample_admin):
        deps = _make_deps(store, idx, audit)
        _run(_cmd_xem_thung_rac("c1", sample_admin, deps))
        events = audit.list_recent(action="recycle_view")
        assert len(events) == 1
        assert events[0].payload["items"] == 0


# ─────────────────────────────────────────────────────────────────────────────
# khoi phuc
# ─────────────────────────────────────────────────────────────────────────────


class TestKhoiPhuc:

    def test_admin_restores_user(self, store, idx, audit, sample_admin, member_user):
        store.soft_delete_user(member_user.id)
        deps = _make_deps(store, idx, audit)
        _run(_cmd_khoi_phuc("c1", f"user {member_user.id}", sample_admin, deps))
        assert store.get_user_by_id(member_user.id).is_active is True
        events = audit.list_recent(action="recycle_restore")
        assert events[0].target_type == "user"
        assert events[0].target_id == str(member_user.id)

    def test_admin_restores_note(self, store, idx, audit, sample_admin):
        nid = idx.add_note("file-1", sample_admin.id, title="N")
        idx.soft_delete_note(nid)
        deps = _make_deps(store, idx, audit)
        _run(_cmd_khoi_phuc("c1", f"note {nid}", sample_admin, deps))
        assert idx.get_note_meta("file-1") is not None
        events = audit.list_recent(action="recycle_restore")
        assert events[0].target_type == "note"

    def test_admin_restores_wiki(self, store, idx, audit, sample_admin):
        wid = idx.add_wiki_page("w-1", sample_admin.id, topic="T", slug="t")
        idx.soft_delete_wiki(wid)
        deps = _make_deps(store, idx, audit)
        _run(_cmd_khoi_phuc("c1", f"wiki {wid}", sample_admin, deps))
        assert idx.get_wiki_meta("w-1") is not None

    def test_non_admin_rejected(self, store, idx, audit, member_user):
        deps = _make_deps(store, idx, audit)
        _run(_cmd_khoi_phuc("c1", "user 5", member_user, deps))
        assert "Chỉ admin" in deps.channel.last_text
        assert audit.list_recent(action="recycle_restore") == []

    def test_bad_syntax_returns_usage(self, store, idx, audit, sample_admin):
        deps = _make_deps(store, idx, audit)
        _run(_cmd_khoi_phuc("c1", "garbage", sample_admin, deps))
        assert "Cu phap" in deps.channel.last_text

    def test_restore_not_found_reports_error(self, store, idx, audit, sample_admin):
        deps = _make_deps(store, idx, audit)
        _run(_cmd_khoi_phuc("c1", "user 99999", sample_admin, deps))
        assert "Khong tim thay" in deps.channel.last_text
        assert audit.list_recent(action="recycle_restore") == []

    def test_restore_already_active_user_reports_error(self, store, idx, audit, sample_admin, member_user):
        """member_user is active; restore should report not in recycle bin."""
        deps = _make_deps(store, idx, audit)
        _run(_cmd_khoi_phuc("c1", f"user {member_user.id}", sample_admin, deps))
        assert "Khong tim thay" in deps.channel.last_text


# ─────────────────────────────────────────────────────────────────────────────
# xoa han
# ─────────────────────────────────────────────────────────────────────────────


class TestXoaHan:

    def test_admin_hard_deletes_user_no_refs(self, store, idx, audit, sample_admin):
        u = store.create_user(name="Lone", role="readonly")
        store.soft_delete_user(u.id)
        deps = _make_deps(store, idx, audit)

        _run(_cmd_xoa_han("c1", f"user {u.id}", sample_admin, deps))

        assert store.get_user_by_id(u.id) is None
        events = audit.list_recent(action="recycle_purge")
        assert events[0].target_type == "user"
        assert events[0].target_id == str(u.id)

    def test_user_fk_constraint_blocks_hard_delete(
        self, store, idx, audit, sample_admin, member_user,
    ):
        """Active channel_binding prevents DELETE; surface clear error."""
        store.bind_channel(member_user.id, "telegram", "999")
        store.soft_delete_user(member_user.id)
        deps = _make_deps(store, idx, audit)

        _run(_cmd_xoa_han("c1", f"user {member_user.id}", sample_admin, deps))

        # User row still in DB.
        assert store.get_user_by_id(member_user.id) is not None
        # Error message mentions referenced data.
        text = deps.channel.last_text
        assert "tham chieu" in text or "channel_bindings" in text
        # No audit row written for failed purge.
        assert audit.list_recent(action="recycle_purge") == []

    def test_hard_delete_note_calls_drive_delete(self, store, idx, audit, sample_admin):
        nid = idx.add_note("drive_abc", sample_admin.id, title="N")
        idx.soft_delete_note(nid)
        notes = FakeDriveAdapter(succeed=True)
        deps = _make_deps(store, idx, audit, notes_adapter=notes)

        _run(_cmd_xoa_han("c1", f"note {nid}", sample_admin, deps))

        assert notes.deleted_ids == ["drive_abc"]
        events = audit.list_recent(action="recycle_purge")
        assert events[0].payload["drive_deleted"] is True
        assert events[0].payload["drive_file_id"] == "drive_abc"

    def test_hard_delete_note_drive_failure_still_proceeds(
        self, store, idx, audit, sample_admin,
    ):
        """Drive delete failure should not block SQLite purge."""
        nid = idx.add_note("drive_xyz", sample_admin.id)
        idx.soft_delete_note(nid)
        notes = FakeDriveAdapter(succeed=False)
        deps = _make_deps(store, idx, audit, notes_adapter=notes)

        _run(_cmd_xoa_han("c1", f"note {nid}", sample_admin, deps))

        # SQLite row gone.
        assert idx.list_deleted_notes() == []
        events = audit.list_recent(action="recycle_purge")
        assert events[0].payload["drive_deleted"] is False
        assert "orphan" in deps.channel.last_text.lower()

    def test_hard_delete_wiki_calls_wiki_adapter(self, store, idx, audit, sample_admin):
        wid = idx.add_wiki_page("drive_w_1", sample_admin.id, topic="T", slug="t")
        idx.soft_delete_wiki(wid)
        wiki = FakeDriveAdapter(succeed=True)
        notes = FakeDriveAdapter(succeed=True)
        deps = _make_deps(store, idx, audit, notes_adapter=notes, wiki_adapter=wiki)

        _run(_cmd_xoa_han("c1", f"wiki {wid}", sample_admin, deps))

        # Only wiki adapter called.
        assert wiki.deleted_ids == ["drive_w_1"]
        assert notes.deleted_ids == []
        events = audit.list_recent(action="recycle_purge")
        assert events[0].target_type == "wiki"

    def test_hard_delete_not_found(self, store, idx, audit, sample_admin):
        deps = _make_deps(store, idx, audit)
        _run(_cmd_xoa_han("c1", "note 99999", sample_admin, deps))
        assert "Khong tim thay" in deps.channel.last_text
        assert audit.list_recent(action="recycle_purge") == []

    def test_bad_syntax_returns_usage(self, store, idx, audit, sample_admin):
        deps = _make_deps(store, idx, audit)
        _run(_cmd_xoa_han("c1", "garbage stuff here", sample_admin, deps))
        assert "Cu phap" in deps.channel.last_text

    def test_non_admin_rejected(self, store, idx, audit, member_user):
        deps = _make_deps(store, idx, audit)
        _run(_cmd_xoa_han("c1", "user 5", member_user, deps))
        assert "Chỉ admin" in deps.channel.last_text
        assert audit.list_recent(action="recycle_purge") == []

    def test_hard_delete_drive_adapter_raises_still_audits(
        self, store, idx, audit, sample_admin,
    ):
        """If adapter.delete_file raises (not just returns False), still proceed."""
        nid = idx.add_note("drive_boom", sample_admin.id)
        idx.soft_delete_note(nid)

        class RaisingAdapter:
            def delete_file(self, _file_id):
                raise RuntimeError("boom")

        deps = _make_deps(store, idx, audit, notes_adapter=RaisingAdapter())
        _run(_cmd_xoa_han("c1", f"note {nid}", sample_admin, deps))

        assert idx.list_deleted_notes() == []
        events = audit.list_recent(action="recycle_purge")
        assert events[0].payload["drive_deleted"] is False
