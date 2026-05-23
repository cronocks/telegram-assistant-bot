"""tests/test_backup_engine.py — Unit tests for BackupEngine (FR-6).

Covers:
  - export_cooldown_remaining
  - generate_export (rate limit, unknown user, success)
  - _collect_data (all data sections)
  - _build_manifest (stats fields)
  - parse_import (size check, bad ZIP, missing files, path traversal, name conflict, warnings)
  - apply_import (user/bindings/quota/memory/conversations/parent_links, rollback)
  - _import_* helpers
"""
from __future__ import annotations

import io
import json
import sqlite3
import zipfile
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from backup_engine import (
    AUDIT_LOG_CAP,
    EXPORT_COOLDOWN_SECONDS,
    FORMAT_VERSION,
    MAX_IMPORT_BYTES,
    BackupEngine,
    ExportError,
    ImportFormatError,
    ParsedImport,
)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_zip(
    manifest: dict | None = None,
    data: dict | None = None,
    extra_files: dict[str, bytes] | None = None,
) -> bytes:
    """Build a minimal valid ZIP for import tests."""
    if manifest is None:
        manifest = {
            "format_version": FORMAT_VERSION,
            "exported_at": "2025-01-01T00:00:00+00:00",
            "exporter": "test",
            "source_user": {"id": 99, "name": "TestUser", "role": "member"},
            "stats": {},
        }
    if data is None:
        data = {
            "user": {"id": 99, "name": "TestUser", "role": "member"},
            "channel_bindings": [],
            "quota": None,
            "parent_links_as_child": [],
            "notes": [],
            "wiki_pages": [],
            "user_memory": [],
            "web_conversations": [],
            "audit_entries": [],
        }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        zf.writestr("data.json", json.dumps(data))
        for name, content in (extra_files or {}).items():
            zf.writestr(name, content)
    return buf.getvalue()


def _make_engine(db_conn) -> BackupEngine:
    """BackupEngine with mock stores wired to the in-memory DB."""
    user_store = MagicMock()
    note_index = MagicMock()
    memory_store = MagicMock()
    web_conv_store = MagicMock()
    audit = MagicMock()
    notes = MagicMock()
    wiki = MagicMock()

    engine = BackupEngine.__new__(BackupEngine)
    engine._user_store = user_store
    engine._note_index = note_index
    engine._memory_store = memory_store
    engine._web_conv_store = web_conv_store
    engine._audit = audit
    engine._notes = notes
    engine._wiki = wiki
    engine._conn = db_conn
    engine._last_export_at = {}
    return engine


def _seed_user(db_conn, user_id: int = 1, name: str = "Alice", role: str = "member") -> None:
    db_conn.execute(
        "INSERT INTO users (id, name, role) VALUES (?, ?, ?)",
        (user_id, name, role),
    )
    db_conn.commit()


# ── Fixtures ───────────────────────────────────────────────────────────────────

@pytest.fixture()
def engine(db_conn):
    return _make_engine(db_conn)


# ══════════════════════════════════════════════════════════════════════════════
# export_cooldown_remaining
# ══════════════════════════════════════════════════════════════════════════════

class TestCooldownRemaining:
    def test_no_prior_export_returns_zero(self, engine):
        assert engine.export_cooldown_remaining(1) == 0

    def test_after_export_returns_positive(self, engine):
        engine._last_export_at[1] = datetime.now(timezone.utc)
        remaining = engine.export_cooldown_remaining(1)
        assert 0 < remaining <= EXPORT_COOLDOWN_SECONDS

    def test_after_full_cooldown_returns_zero(self, engine):
        from datetime import timedelta
        engine._last_export_at[1] = datetime.now(timezone.utc) - timedelta(seconds=EXPORT_COOLDOWN_SECONDS + 1)
        assert engine.export_cooldown_remaining(1) == 0

    def test_different_users_independent(self, engine):
        engine._last_export_at[1] = datetime.now(timezone.utc)
        assert engine.export_cooldown_remaining(2) == 0


# ══════════════════════════════════════════════════════════════════════════════
# generate_export
# ══════════════════════════════════════════════════════════════════════════════

class TestGenerateExport:
    def test_raises_export_error_on_cooldown(self, engine):
        engine._last_export_at[1] = datetime.now(timezone.utc)
        with pytest.raises(ExportError, match="Rate limit"):
            engine.generate_export(1)

    def test_raises_export_error_for_unknown_user(self, engine):
        engine._user_store.get_user_by_id.return_value = None
        with pytest.raises(ExportError, match="not found"):
            engine.generate_export(99)

    def test_success_returns_bytes_and_manifest(self, db_conn, engine):
        from interfaces import User
        _seed_user(db_conn, 1, "Alice")
        engine._user_store.get_user_by_id.return_value = User(id=1, name="Alice", role="member")
        engine._notes.read_file_by_id.return_value = {"content": ""}
        engine._wiki.save_page.return_value = ("wiki.md", "fid-wiki")

        zip_bytes, manifest = engine.generate_export(1)

        assert isinstance(zip_bytes, bytes) and len(zip_bytes) > 0
        assert manifest["format_version"] == FORMAT_VERSION
        assert manifest["source_user"]["id"] == 1
        engine._audit.log.assert_called()

    def test_cooldown_is_set_after_success(self, db_conn, engine):
        from interfaces import User
        _seed_user(db_conn, 1, "Alice")
        engine._user_store.get_user_by_id.return_value = User(id=1, name="Alice", role="member")
        engine._notes.read_file_by_id.return_value = {"content": ""}

        engine.generate_export(1)

        assert 1 in engine._last_export_at

    def test_build_zip_failure_emits_audit_and_raises(self, db_conn, engine):
        from interfaces import User
        _seed_user(db_conn, 1, "Alice")
        engine._user_store.get_user_by_id.return_value = User(id=1, name="Alice", role="member")

        with patch.object(engine, "_build_zip", side_effect=RuntimeError("DB crash")):
            with pytest.raises(ExportError, match="Export failed"):
                engine.generate_export(1)

        calls = [str(c) for c in engine._audit.log.call_args_list]
        assert any("data_export_failed" in c for c in calls)


# ══════════════════════════════════════════════════════════════════════════════
# _collect_data
# ══════════════════════════════════════════════════════════════════════════════

class TestCollectData:
    def test_returns_all_keys(self, db_conn, engine):
        _seed_user(db_conn, 1, "Alice")
        data = engine._collect_data(1)
        expected_keys = {
            "user", "channel_bindings", "quota",
            "parent_links_as_child", "parent_links_as_parent",
            "username_changes", "birthdate_changes",
            "notes", "wiki_pages", "user_memory",
            "web_conversations", "audit_entries",
        }
        assert expected_keys == set(data.keys())

    def test_user_section_contains_name(self, db_conn, engine):
        _seed_user(db_conn, 1, "Bob")
        data = engine._collect_data(1)
        assert data["user"]["name"] == "Bob"

    def test_empty_sections_for_fresh_user(self, db_conn, engine):
        _seed_user(db_conn, 1, "Alice")
        data = engine._collect_data(1)
        assert data["channel_bindings"] == []
        assert data["notes"] == []
        assert data["wiki_pages"] == []
        assert data["user_memory"] == []
        assert data["web_conversations"] == []

    def test_web_conversations_with_messages_are_denormalized(self, db_conn, engine):
        _seed_user(db_conn, 1, "Alice")
        db_conn.execute(
            "INSERT INTO web_conversations (id, user_id, title) VALUES (10, 1, 'Chat1')"
        )
        db_conn.execute(
            "INSERT INTO web_messages (conversation_id, role, text) VALUES (10, 'user', 'Hello')"
        )
        db_conn.commit()
        data = engine._collect_data(1)
        assert len(data["web_conversations"]) == 1
        assert data["web_conversations"][0]["messages"][0]["text"] == "Hello"

    def test_audit_log_is_capped(self, db_conn, engine):
        _seed_user(db_conn, 1, "Alice")
        for i in range(AUDIT_LOG_CAP + 50):
            db_conn.execute(
                "INSERT INTO audit_log (actor_user_id, action, target_type, target_id) VALUES (1, 'test', 'user', 1)"
            )
        db_conn.commit()
        data = engine._collect_data(1)
        assert len(data["audit_entries"]) == AUDIT_LOG_CAP


# ══════════════════════════════════════════════════════════════════════════════
# _build_manifest
# ══════════════════════════════════════════════════════════════════════════════

class TestBuildManifest:
    def test_format_version_and_exporter(self, engine):
        data = {
            "user": {"id": 1, "name": "Alice", "username": None, "role": "member"},
            "notes": [], "wiki_pages": [], "user_memory": [],
            "web_conversations": [],
            "audit_entries": [],
        }
        manifest = engine._build_manifest(data, size_bytes_uncompressed=1000)
        assert manifest["format_version"] == FORMAT_VERSION
        assert manifest["exporter"] == "telegram-bot-fr6"

    def test_stats_counts_are_correct(self, engine):
        data = {
            "user": {"id": 1, "name": "Alice", "username": None, "role": "member"},
            "notes": [{"drive_file_id": "n1"}, {"drive_file_id": "n2"}],
            "wiki_pages": [{"drive_file_id": "w1"}],
            "user_memory": [{"kind": "a"}, {"kind": "b"}],
            "web_conversations": [
                {"messages": [{"role": "user", "text": "hi"}, {"role": "assistant", "text": "hello"}]},
                {"messages": []},
            ],
            "audit_entries": [],
        }
        manifest = engine._build_manifest(data, size_bytes_uncompressed=2000)
        stats = manifest["stats"]
        assert stats["notes"] == 2
        assert stats["wiki_pages"] == 1
        assert stats["memory_kinds"] == 2
        assert stats["web_conversations"] == 2
        assert stats["web_messages"] == 2
        assert stats["size_bytes_uncompressed"] == 2000


# ══════════════════════════════════════════════════════════════════════════════
# parse_import
# ══════════════════════════════════════════════════════════════════════════════

class TestParseImport:
    def test_raises_if_too_large(self, engine):
        big = b"x" * (MAX_IMPORT_BYTES + 1)
        with pytest.raises(ImportFormatError, match="too large"):
            engine.parse_import(big)

    def test_raises_on_bad_zip(self, engine):
        with pytest.raises(ImportFormatError, match="valid ZIP"):
            engine.parse_import(b"not a zip")

    def test_raises_if_manifest_missing(self, engine):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("data.json", "{}")
        with pytest.raises(ImportFormatError, match="manifest.json"):
            engine.parse_import(buf.getvalue())

    def test_raises_if_data_missing(self, engine):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("manifest.json", json.dumps({"format_version": FORMAT_VERSION}))
        with pytest.raises(ImportFormatError, match="data.json"):
            engine.parse_import(buf.getvalue())

    def test_raises_on_wrong_format_version(self, engine):
        manifest = {"format_version": 999}
        zip_bytes = _make_zip(manifest=manifest)
        with pytest.raises(ImportFormatError, match="format_version"):
            engine.parse_import(zip_bytes)

    def test_raises_on_path_traversal(self, engine):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("manifest.json", json.dumps({"format_version": FORMAT_VERSION}))
            zf.writestr("data.json", "{}")
            zf.writestr("../evil.txt", "bad")
        with pytest.raises(ImportFormatError, match="unsafe path"):
            engine.parse_import(buf.getvalue())

    def test_valid_zip_returns_parsed_import(self, db_conn, engine):
        zip_bytes = _make_zip()
        result = engine.parse_import(zip_bytes)
        assert result.manifest["format_version"] == FORMAT_VERSION
        assert result.data["user"]["name"] == "TestUser"
        assert isinstance(result.warnings, list)

    def test_missing_note_content_adds_warning(self, db_conn, engine):
        data = {
            "user": {"id": 99, "name": "Orphan"},
            "notes": [{"drive_file_id": "fid1", "title": "MyNote", "content_path": "notes/fid1.md"}],
            "wiki_pages": [],
            "user_memory": [],
            "web_conversations": [],
            "audit_entries": [],
            "channel_bindings": [],
            "quota": None,
            "parent_links_as_child": [],
        }
        zip_bytes = _make_zip(data=data)  # no notes/fid1.md file
        result = engine.parse_import(zip_bytes)
        assert any("fid1" in w or "MyNote" in w for w in result.warnings)
        assert result.notes_content.get("fid1") == b""

    def test_name_conflict_adds_warning(self, db_conn, engine):
        _seed_user(db_conn, 1, "TestUser")
        zip_bytes = _make_zip()  # source_user name = "TestUser"
        result = engine.parse_import(zip_bytes)
        assert any("already exists" in w for w in result.warnings)

    def test_note_content_loaded_correctly(self, db_conn, engine):
        data = {
            "user": {"id": 99, "name": "X"},
            "notes": [{"drive_file_id": "nid1", "title": "T", "content_path": "notes/nid1.md"}],
            "wiki_pages": [],
            "user_memory": [],
            "web_conversations": [],
            "audit_entries": [],
            "channel_bindings": [],
            "quota": None,
            "parent_links_as_child": [],
        }
        zip_bytes = _make_zip(data=data, extra_files={"notes/nid1.md": b"Hello!"})
        result = engine.parse_import(zip_bytes)
        assert result.notes_content["nid1"] == b"Hello!"


# ══════════════════════════════════════════════════════════════════════════════
# apply_import — helpers
# ══════════════════════════════════════════════════════════════════════════════

class TestImportUser:
    def test_inserts_new_user_and_returns_id(self, db_conn, engine):
        user_data = {"name": "Imported", "role": "member", "username": None,
                     "birthdate": None, "password_hash": None, "must_change_password": 0}
        new_id = engine._import_user(user_data)
        row = db_conn.execute("SELECT * FROM users WHERE id = ?", (new_id,)).fetchone()
        assert row["name"] == "Imported"
        assert row["role"] == "member"

    def test_defaults_role_to_member(self, db_conn, engine):
        new_id = engine._import_user({"name": "NoRole"})
        row = db_conn.execute("SELECT role FROM users WHERE id = ?", (new_id,)).fetchone()
        assert row["role"] == "member"


class TestImportChannelBindings:
    def test_inserts_binding(self, db_conn, engine):
        _seed_user(db_conn, 5, "Tester")
        warnings = engine._import_channel_bindings(
            [{"channel": "telegram", "chat_id": "12345"}], new_user_id=5
        )
        assert warnings == []
        row = db_conn.execute(
            "SELECT * FROM channel_bindings WHERE user_id = 5"
        ).fetchone()
        assert row["chat_id"] == "12345"

    def test_skips_conflict_and_warns(self, db_conn, engine):
        _seed_user(db_conn, 5, "User5")
        _seed_user(db_conn, 6, "User6")
        db_conn.execute(
            "INSERT INTO channel_bindings (user_id, channel, chat_id) VALUES (5, 'telegram', '111')"
        )
        db_conn.commit()
        warnings = engine._import_channel_bindings(
            [{"channel": "telegram", "chat_id": "111"}], new_user_id=6
        )
        assert len(warnings) == 1
        assert "already bound" in warnings[0]


class TestImportQuota:
    def test_inserts_quota_row(self, db_conn, engine):
        _seed_user(db_conn, 5, "QUser")
        engine._import_quota({"monthly_token_limit": 50000, "month": "2025-01"}, 5)
        row = db_conn.execute("SELECT * FROM user_quotas WHERE user_id = 5").fetchone()
        assert row["monthly_token_limit"] == 50000

    def test_noop_when_quota_is_none(self, db_conn, engine):
        _seed_user(db_conn, 5, "QUser")
        engine._import_quota(None, 5)
        row = db_conn.execute("SELECT * FROM user_quotas WHERE user_id = 5").fetchone()
        assert row is None


class TestImportMemory:
    def test_calls_memory_store_set(self, engine):
        engine._import_memory(
            [{"kind": "summary", "content": "Some text"}], new_user_id=3
        )
        engine._memory_store.set.assert_called_once_with(3, "summary", "Some text")


class TestImportConversations:
    def test_creates_convs_and_messages(self, engine):
        engine._web_conv_store.create.return_value = 10
        conversations = [
            {"title": "Chat A", "messages": [
                {"role": "user", "text": "Hi"},
                {"role": "assistant", "text": "Hello"},
            ]},
            {"title": None, "messages": []},
        ]
        conv_count, msg_count = engine._import_conversations(conversations, new_user_id=1)
        assert conv_count == 2
        assert msg_count == 2

    def test_rename_called_only_when_title_present(self, engine):
        engine._web_conv_store.create.return_value = 10
        engine._import_conversations(
            [{"title": "Named", "messages": []}, {"title": None, "messages": []}],
            new_user_id=1,
        )
        engine._web_conv_store.rename.assert_called_once_with(10, "Named")


class TestImportParentLinks:
    def test_skips_inactive_links(self, db_conn, engine):
        _seed_user(db_conn, 5, "Child")
        _seed_user(db_conn, 6, "Parent")
        warnings = engine._import_parent_links(
            [{"active": 0, "parent_name": "Parent"}], new_user_id=5
        )
        row = db_conn.execute("SELECT * FROM parent_links WHERE user_id = 5").fetchone()
        assert row is None
        assert warnings == []

    def test_warns_when_parent_not_found(self, db_conn, engine):
        _seed_user(db_conn, 5, "Child")
        warnings = engine._import_parent_links(
            [{"active": 1, "parent_name": "Ghost"}], new_user_id=5
        )
        assert any("Ghost" in w for w in warnings)

    def test_inserts_active_link_when_parent_exists(self, db_conn, engine):
        _seed_user(db_conn, 5, "Child")
        _seed_user(db_conn, 6, "Parent")
        warnings = engine._import_parent_links(
            [{"active": 1, "parent_name": "Parent"}], new_user_id=5
        )
        row = db_conn.execute("SELECT * FROM parent_links WHERE user_id = 5").fetchone()
        assert row is not None
        assert row["parent_id"] == 6


# ══════════════════════════════════════════════════════════════════════════════
# apply_import — full flow
# ══════════════════════════════════════════════════════════════════════════════

class TestApplyImport:
    def _make_parsed(self, name: str = "ImportedUser") -> ParsedImport:
        data = {
            "user": {"name": name, "role": "member", "username": None,
                     "birthdate": None, "password_hash": None, "must_change_password": 0},
            "channel_bindings": [],
            "quota": None,
            "notes": [],
            "wiki_pages": [],
            "user_memory": [],
            "web_conversations": [],
            "parent_links_as_child": [],
            "audit_entries": [],
        }
        return ParsedImport(
            manifest={"source_user": {"name": name}},
            data=data,
            notes_content={},
            wiki_content={},
        )

    def test_creates_new_user_in_db(self, db_conn, engine):
        parsed = self._make_parsed("ImportedUser")
        result = engine.apply_import(parsed, admin_user_id=0)
        row = db_conn.execute("SELECT name FROM users WHERE id = ?", (result.new_user_id,)).fetchone()
        assert row["name"] == "ImportedUser"

    def test_result_counts_are_zero_for_empty_import(self, db_conn, engine):
        parsed = self._make_parsed()
        result = engine.apply_import(parsed, admin_user_id=0)
        assert result.counts["notes"] == 0
        assert result.counts["web_conversations"] == 0

    def test_emits_data_import_audit(self, db_conn, engine):
        parsed = self._make_parsed()
        engine.apply_import(parsed, admin_user_id=0)
        calls = [str(c) for c in engine._audit.log.call_args_list]
        assert any("data_import" in c for c in calls)

    def test_rollback_on_failure(self, db_conn, engine):
        parsed = self._make_parsed()
        parsed.data["notes"] = [{"drive_file_id": "nid", "title": "T", "content_path": "notes/nid.md"}]
        parsed.notes_content = {"nid": b"Hello"}

        engine._notes.save_note.side_effect = RuntimeError("Drive error")
        engine._notes.delete_file = MagicMock()

        with pytest.raises(Exception):
            engine.apply_import(parsed, admin_user_id=0)

        calls = [str(c) for c in engine._audit.log.call_args_list]
        assert any("data_import_failed" in c for c in calls)
