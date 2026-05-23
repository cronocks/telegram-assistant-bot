"""tests/test_local_migrate.py — Unit tests for tools/local_migrate.py (FR-6).

Covers:
  - _copy_db (success, dry-run, missing DB)
  - _list_drive_files (user filter, deleted filter)
  - _mirror_file (skip-if-exists, dry-run, download success, error handling)
  - main() integration (dry-run, --users filter, summary printed)
"""
from __future__ import annotations

import io
import json
import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Make project root importable so tools/ package is accessible.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from tools.local_migrate import (
    _copy_db,
    _download_drive_file,
    _list_drive_files,
    _mirror_file,
)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_db(tmp_path: Path) -> tuple[Path, sqlite3.Connection]:
    """Create a minimal SQLite DB at tmp_path/bot.db. Returns (path, conn)."""
    db_path = tmp_path / "bot.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE notes (
            id INTEGER PRIMARY KEY,
            drive_file_id TEXT,
            title TEXT,
            scope TEXT DEFAULT 'private',
            kind TEXT DEFAULT 'note',
            owner_user_id INTEGER,
            deleted_at DATETIME
        );
        CREATE TABLE wiki_pages (
            id INTEGER PRIMARY KEY,
            drive_file_id TEXT,
            slug TEXT,
            topic TEXT,
            scope TEXT DEFAULT 'everyone',
            owner_user_id INTEGER,
            deleted_at DATETIME
        );
    """)
    conn.commit()
    return db_path, conn


# ══════════════════════════════════════════════════════════════════════════════
# _copy_db
# ══════════════════════════════════════════════════════════════════════════════

class TestCopyDb:
    def test_copies_db_to_output_dir(self, tmp_path):
        db_path, conn = _make_db(tmp_path)
        conn.close()
        output_dir = tmp_path / "output"
        output_dir.mkdir()

        # Patch _PROJECT_ROOT inside the module so it finds our tmp db.
        with patch("tools.local_migrate._PROJECT_ROOT", tmp_path):
            dest = _copy_db(output_dir, dry_run=False)

        assert dest.exists()
        assert dest.stat().st_size > 0

    def test_dry_run_does_not_create_file(self, tmp_path):
        db_path, conn = _make_db(tmp_path)
        conn.close()
        output_dir = tmp_path / "output"
        output_dir.mkdir()

        with patch("tools.local_migrate._PROJECT_ROOT", tmp_path):
            dest = _copy_db(output_dir, dry_run=True)

        assert not dest.exists()

    def test_returns_destination_path(self, tmp_path):
        db_path, conn = _make_db(tmp_path)
        conn.close()
        output_dir = tmp_path / "output"
        output_dir.mkdir()

        with patch("tools.local_migrate._PROJECT_ROOT", tmp_path):
            dest = _copy_db(output_dir, dry_run=False)

        assert dest.name == "bot.db"
        assert dest.parent == output_dir

    def test_raises_if_db_missing(self, tmp_path):
        output_dir = tmp_path / "output"
        output_dir.mkdir()

        with patch("tools.local_migrate._PROJECT_ROOT", tmp_path):
            with pytest.raises(SystemExit):
                _copy_db(output_dir, dry_run=False)


# ══════════════════════════════════════════════════════════════════════════════
# _list_drive_files
# ══════════════════════════════════════════════════════════════════════════════

class TestListDriveFiles:
    def _make_memory_conn(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE notes (
                id INTEGER PRIMARY KEY,
                drive_file_id TEXT,
                title TEXT,
                scope TEXT DEFAULT 'private',
                kind TEXT DEFAULT 'note',
                owner_user_id INTEGER,
                deleted_at DATETIME
            );
            CREATE TABLE wiki_pages (
                id INTEGER PRIMARY KEY,
                drive_file_id TEXT,
                slug TEXT,
                topic TEXT,
                scope TEXT DEFAULT 'everyone',
                owner_user_id INTEGER,
                deleted_at DATETIME
            );
        """)
        return conn

    def test_returns_all_when_no_filter(self):
        conn = self._make_memory_conn()
        conn.execute("INSERT INTO notes VALUES (1,'fid1','N1','private','note',1,NULL)")
        conn.execute("INSERT INTO notes VALUES (2,'fid2','N2','private','note',2,NULL)")
        conn.execute("INSERT INTO wiki_pages VALUES (1,'wid1','slug1','T1','everyone',1,NULL)")
        conn.commit()
        notes, wiki = _list_drive_files(conn, user_ids=None, include_deleted=False)
        assert len(notes) == 2
        assert len(wiki) == 1

    def test_user_filter_limits_results(self):
        conn = self._make_memory_conn()
        conn.execute("INSERT INTO notes VALUES (1,'fid1','N1','private','note',1,NULL)")
        conn.execute("INSERT INTO notes VALUES (2,'fid2','N2','private','note',2,NULL)")
        conn.commit()
        notes, _ = _list_drive_files(conn, user_ids=[1], include_deleted=False)
        assert len(notes) == 1
        assert notes[0]["drive_file_id"] == "fid1"

    def test_excludes_deleted_by_default(self):
        conn = self._make_memory_conn()
        conn.execute("INSERT INTO notes VALUES (1,'fid1','N1','private','note',1,'2025-01-01')")
        conn.execute("INSERT INTO notes VALUES (2,'fid2','N2','private','note',1,NULL)")
        conn.commit()
        notes, _ = _list_drive_files(conn, user_ids=None, include_deleted=False)
        assert len(notes) == 1
        assert notes[0]["drive_file_id"] == "fid2"

    def test_include_deleted_returns_all(self):
        conn = self._make_memory_conn()
        conn.execute("INSERT INTO notes VALUES (1,'fid1','N1','private','note',1,'2025-01-01')")
        conn.execute("INSERT INTO notes VALUES (2,'fid2','N2','private','note',1,NULL)")
        conn.commit()
        notes, _ = _list_drive_files(conn, user_ids=None, include_deleted=True)
        assert len(notes) == 2

    def test_empty_db_returns_empty_lists(self):
        conn = self._make_memory_conn()
        notes, wiki = _list_drive_files(conn, user_ids=None, include_deleted=False)
        assert notes == []
        assert wiki == []


# ══════════════════════════════════════════════════════════════════════════════
# _mirror_file
# ══════════════════════════════════════════════════════════════════════════════

class TestMirrorFile:
    def test_skips_if_file_exists(self, tmp_path):
        dest = tmp_path / "existing.md"
        dest.write_bytes(b"already here")
        service = MagicMock()
        ok, label = _mirror_file(service, "fid1", dest, dry_run=False)
        assert ok is False
        assert label == "skip"
        service.files.assert_not_called()

    def test_dry_run_does_not_download(self, tmp_path):
        dest = tmp_path / "new.md"
        service = MagicMock()
        ok, label = _mirror_file(service, "fid1", dest, dry_run=True)
        assert ok is False
        assert label == "dry-run"
        assert not dest.exists()

    def test_downloads_and_writes_file(self, tmp_path):
        dest = tmp_path / "notes" / "fid1.md"
        service = MagicMock()

        with patch("tools.local_migrate._download_drive_file", return_value=b"# Note content"):
            ok, label = _mirror_file(service, "fid1", dest, dry_run=False)

        assert ok is True
        assert label == "ok"
        assert dest.read_bytes() == b"# Note content"

    def test_returns_error_label_on_exception(self, tmp_path):
        dest = tmp_path / "notes" / "fid1.md"
        service = MagicMock()

        with patch("tools.local_migrate._download_drive_file", side_effect=RuntimeError("Drive 500")):
            ok, label = _mirror_file(service, "fid1", dest, dry_run=False)

        assert ok is False
        assert "error" in label

    def test_creates_parent_dirs(self, tmp_path):
        dest = tmp_path / "a" / "b" / "c" / "file.md"
        service = MagicMock()

        with patch("tools.local_migrate._download_drive_file", return_value=b"content"):
            _mirror_file(service, "fid1", dest, dry_run=False)

        assert dest.exists()


# ══════════════════════════════════════════════════════════════════════════════
# main() integration — dry-run path
# ══════════════════════════════════════════════════════════════════════════════

class TestMainDryRun:
    def test_dry_run_prints_summary_and_exits_clean(self, tmp_path, capsys):
        db_path, conn = _make_db(tmp_path)
        conn.close()

        with patch("tools.local_migrate._PROJECT_ROOT", tmp_path), \
             patch("tools.local_migrate._check_env"), \
             patch("sys.argv", ["local_migrate.py", "--dry-run", f"--output={tmp_path / 'out'}"]):
            from tools import local_migrate
            local_migrate.main()

        captured = capsys.readouterr()
        assert "dry-run" in captured.out.lower()
        assert "Summary" in captured.out

    def test_dry_run_writes_no_files(self, tmp_path):
        db_path, conn = _make_db(tmp_path)
        conn.close()
        out_dir = tmp_path / "out"

        with patch("tools.local_migrate._PROJECT_ROOT", tmp_path), \
             patch("tools.local_migrate._check_env"), \
             patch("sys.argv", ["local_migrate.py", "--dry-run", f"--output={out_dir}"]):
            from tools import local_migrate
            local_migrate.main()

        assert not out_dir.exists()

    def test_users_filter_is_applied(self, tmp_path, capsys):
        db_path, conn = _make_db(tmp_path)
        conn.execute("INSERT INTO notes VALUES (1,'fid1','N1','private','note',1,NULL)")
        conn.execute("INSERT INTO notes VALUES (2,'fid2','N2','private','note',2,NULL)")
        conn.commit()
        conn.close()

        with patch("tools.local_migrate._PROJECT_ROOT", tmp_path), \
             patch("tools.local_migrate._check_env"), \
             patch("sys.argv", ["local_migrate.py", "--dry-run",
                                f"--output={tmp_path / 'out'}", "--users=1"]):
            from tools import local_migrate
            local_migrate.main()

        captured = capsys.readouterr()
        assert "1 notes" in captured.out or "filtered" in captured.out.lower()
