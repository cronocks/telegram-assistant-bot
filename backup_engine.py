"""backup_engine.py — Per-user data export / import engine (FR-6).

BackupEngine handles:
  - generate_export(): pack one user's data into a ZIP archive (bytes in memory)
  - parse_import() / apply_import(): validate and restore a ZIP (Sub 6.2)

ZIP layout:
  manifest.json       — metadata + stats
  data.json           — all SQLite rows belonging to the user
  notes/<file_id>.md  — raw note content downloaded from Drive
  wiki/<slug>.md      — raw wiki-page content downloaded from Drive
"""
from __future__ import annotations

import io
import json
import logging
import sqlite3
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone

from db.connection import get_connection
from interfaces import (
    AuditLog,
    MemoryStore,
    NoteIndex,
    NoteStore,
    UserStore,
    WebConversationStore,
    WikiStore,
)

logger = logging.getLogger(__name__)

FORMAT_VERSION = 1
EXPORT_COOLDOWN_SECONDS = 300   # 5 minutes per user
MAX_IMPORT_BYTES = 100 * 1024 * 1024  # 100 MB hard limit
AUDIT_LOG_CAP = 1000            # max audit rows exported per user (most recent)


# ── Exceptions ────────────────────────────────────────────────────────────────

class ExportError(Exception):
    """Raised when generate_export() cannot complete."""


class ImportFormatError(Exception):
    """Raised when parse_import() finds the ZIP structurally invalid."""


# ── Result dataclasses ────────────────────────────────────────────────────────

@dataclass
class ParsedImport:
    """Intermediate representation of a validated import ZIP (Sub 6.2)."""
    manifest: dict
    data: dict
    notes_content: dict[str, bytes]    # old_drive_file_id → raw markdown bytes
    wiki_content: dict[str, bytes]     # slug → raw markdown bytes
    warnings: list[str] = field(default_factory=list)


@dataclass
class ImportResult:
    """Summary returned by apply_import() (Sub 6.2)."""
    new_user_id: int
    counts: dict[str, int]
    id_map: dict[str, dict]            # {"notes": {old_file_id: new_file_id}, ...}
    warnings: list[str] = field(default_factory=list)


# ── Engine ────────────────────────────────────────────────────────────────────

class BackupEngine:
    """Export and import engine for per-user data backup/restore.

    Injected via CoreDeps; wired in main.py (Sub 6.6).
    Direct DB access is required to read tables that have no store abstraction
    (channel_bindings, user_quotas, parent_links, audit_log, etc.).
    """

    def __init__(
        self,
        user_store: UserStore,
        note_index: NoteIndex,
        memory_store: MemoryStore,
        web_conversation_store: WebConversationStore,
        audit: AuditLog,
        notes: NoteStore,
        wiki: WikiStore,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        self._user_store = user_store
        self._note_index = note_index
        self._memory_store = memory_store
        self._web_conv_store = web_conversation_store
        self._audit = audit
        self._notes = notes
        self._wiki = wiki
        self._conn = conn or get_connection()
        # Rate-limit tracker: user_id → UTC datetime of last successful export
        self._last_export_at: dict[int, datetime] = {}

    # ── Rate-limit helpers ────────────────────────────────────────────────────

    def export_cooldown_remaining(self, user_id: int) -> int:
        """Return seconds until user_id may export again, or 0 if ready now."""
        last = self._last_export_at.get(user_id)
        if last is None:
            return 0
        elapsed = (datetime.now(timezone.utc) - last).total_seconds()
        remaining = EXPORT_COOLDOWN_SECONDS - elapsed
        return max(0, int(remaining))

    # ── Export (Sub 6.1) ──────────────────────────────────────────────────────

    def generate_export(self, user_id: int) -> tuple[bytes, dict]:
        """Generate an in-memory ZIP archive for one user.

        Returns (zip_bytes, manifest_dict).
        Raises ExportError on cooldown, unknown user, or Drive/DB failure.
        Emits audit events data_export / data_export_failed.
        """
        remaining = self.export_cooldown_remaining(user_id)
        if remaining > 0:
            raise ExportError(
                f"Rate limit: please wait {remaining} seconds before exporting again."
            )

        user = self._user_store.get_user_by_id(user_id)
        if user is None:
            raise ExportError(f"User {user_id} not found.")

        try:
            zip_bytes, manifest = self._build_zip(user_id)
        except ExportError:
            raise
        except Exception as exc:
            self._audit.log(
                actor_user_id=user_id,
                action="data_export_failed",
                target_type="user",
                target_id=user_id,
                payload={"error": str(exc)},
            )
            raise ExportError(f"Export failed: {exc}") from exc

        self._last_export_at[user_id] = datetime.now(timezone.utc)
        self._audit.log(
            actor_user_id=user_id,
            action="data_export",
            target_type="user",
            target_id=user_id,
            payload={
                "size_bytes": len(zip_bytes),
                "notes": manifest["stats"]["notes"],
                "wiki_pages": manifest["stats"]["wiki_pages"],
                "messages": manifest["stats"]["web_messages"],
                "delivery": "web",
            },
        )
        return zip_bytes, manifest

    def _build_zip(self, user_id: int) -> tuple[bytes, dict]:
        """Assemble the ZIP in a BytesIO buffer. No temporary files on disk."""
        buf = io.BytesIO()
        notes_raw_bytes = 0
        wiki_raw_bytes = 0

        with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
            # 1. Collect all SQLite rows for the user.
            data = self._collect_data(user_id)

            # 2. Download note content from Drive and add to ZIP.
            for note in data["notes"]:
                fid = note["drive_file_id"]
                content = self._download_drive_file(fid, label=f"note {fid}")
                path = f"notes/{fid}.md"
                note["content_path"] = path
                notes_raw_bytes += len(content)
                zf.writestr(path, content)

            # 3. Download wiki-page content from Drive and add to ZIP.
            for page in data["wiki_pages"]:
                fid = page["drive_file_id"]
                slug = page["slug"]
                content = self._download_drive_file(fid, label=f"wiki {slug}")
                path = f"wiki/{slug}.md"
                page["content_path"] = path
                wiki_raw_bytes += len(content)
                zf.writestr(path, content)

            # 4. Estimate total uncompressed size for manifest stats.
            data_json_bytes = len(json.dumps(data).encode("utf-8"))
            size_bytes_uncompressed = notes_raw_bytes + wiki_raw_bytes + data_json_bytes

            # 5. Build manifest.
            manifest = self._build_manifest(data, size_bytes_uncompressed)

            # 6. Write JSON entries last so content_path fields are populated.
            zf.writestr(
                "manifest.json",
                json.dumps(manifest, ensure_ascii=False, indent=2),
            )
            zf.writestr(
                "data.json",
                json.dumps(data, ensure_ascii=False, indent=2),
            )

        return buf.getvalue(), manifest

    def _download_drive_file(self, file_id: str, label: str) -> bytes:
        """Download a Drive file by ID. Returns raw bytes; empty on failure."""
        try:
            result = self._notes.read_file_by_id(file_id)
            return (result.get("content") or "").encode("utf-8")
        except Exception as exc:
            logger.warning("Export: cannot download %s: %s", label, exc)
            return b""

    def _collect_data(self, user_id: int) -> dict:
        """Query every relevant SQLite table and return a serializable dict."""
        conn = self._conn

        # users row (includes password_hash — exported intentionally per Decision D1)
        user_row = conn.execute(
            "SELECT * FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        user_data = dict(user_row) if user_row else {}

        # channel_bindings
        cb_rows = conn.execute(
            "SELECT channel, chat_id, bound_at FROM channel_bindings WHERE user_id = ?",
            (user_id,),
        ).fetchall()

        # user_quotas
        quota_row = conn.execute(
            "SELECT monthly_token_limit, used_tokens, month, updated_at"
            " FROM user_quotas WHERE user_id = ?",
            (user_id,),
        ).fetchone()

        # parent_links where user is child
        child_rows = conn.execute(
            """
            SELECT pl.active, pl.created_at, pl.removed_at,
                   u.name AS parent_name
            FROM parent_links pl
            JOIN users u ON u.id = pl.parent_id
            WHERE pl.user_id = ?
            ORDER BY pl.created_at
            """,
            (user_id,),
        ).fetchall()

        # parent_links where user is parent
        parent_rows = conn.execute(
            """
            SELECT pl.active, pl.created_at, pl.removed_at,
                   u.name AS child_name
            FROM parent_links pl
            JOIN users u ON u.id = pl.user_id
            WHERE pl.parent_id = ?
            ORDER BY pl.created_at
            """,
            (user_id,),
        ).fetchall()

        # username_changes — only approved/rejected (not pending ephemeral state)
        uc_rows = conn.execute(
            """
            SELECT old_username, new_username, requested_at, approved_at, rejected_at
            FROM username_changes
            WHERE user_id = ?
              AND (approved_at IS NOT NULL OR rejected_at IS NOT NULL)
            ORDER BY requested_at
            """,
            (user_id,),
        ).fetchall()

        # birthdate_changes — only approved/rejected
        bc_rows = conn.execute(
            """
            SELECT new_birthdate, requested_at, approved_at, rejected_at
            FROM birthdate_changes
            WHERE user_id = ?
              AND (approved_at IS NOT NULL OR rejected_at IS NOT NULL)
            ORDER BY requested_at
            """,
            (user_id,),
        ).fetchall()

        # notes (all, including soft-deleted — user owns the data)
        note_rows = conn.execute(
            """
            SELECT drive_file_id, scope, kind, title,
                   created_at, updated_at, deleted_at
            FROM notes
            WHERE owner_user_id = ?
            ORDER BY created_at
            """,
            (user_id,),
        ).fetchall()

        # wiki_pages (all, including soft-deleted)
        wiki_rows = conn.execute(
            """
            SELECT drive_file_id, scope, topic, slug,
                   created_at, updated_at, deleted_at
            FROM wiki_pages
            WHERE owner_user_id = ?
            ORDER BY created_at
            """,
            (user_id,),
        ).fetchall()

        # user_memory
        mem_rows = conn.execute(
            "SELECT kind, content, updated_at, curated_at"
            " FROM user_memory WHERE user_id = ?",
            (user_id,),
        ).fetchall()

        # web_conversations + messages (denormalized — easier to import)
        conv_rows = conn.execute(
            """
            SELECT id, title, created_at, updated_at
            FROM web_conversations
            WHERE user_id = ?
            ORDER BY created_at
            """,
            (user_id,),
        ).fetchall()
        web_conversations = []
        for conv in conv_rows:
            conv_dict = dict(conv)
            msg_rows = conn.execute(
                """
                SELECT role, text, created_at
                FROM web_messages
                WHERE conversation_id = ?
                ORDER BY created_at, id
                """,
                (conv_dict["id"],),
            ).fetchall()
            conv_dict["messages"] = [dict(m) for m in msg_rows]
            web_conversations.append(conv_dict)

        # audit_log — cap to most recent AUDIT_LOG_CAP rows (information only)
        audit_rows = conn.execute(
            """
            SELECT action, target_type, target_id, payload, created_at
            FROM audit_log
            WHERE actor_user_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (user_id, AUDIT_LOG_CAP),
        ).fetchall()

        return {
            "user": user_data,
            "channel_bindings": [dict(r) for r in cb_rows],
            "quota": dict(quota_row) if quota_row else None,
            "parent_links_as_child": [dict(r) for r in child_rows],
            "parent_links_as_parent": [dict(r) for r in parent_rows],
            "username_changes": [dict(r) for r in uc_rows],
            "birthdate_changes": [dict(r) for r in bc_rows],
            "notes": [dict(r) for r in note_rows],
            "wiki_pages": [dict(r) for r in wiki_rows],
            "user_memory": [dict(r) for r in mem_rows],
            "web_conversations": web_conversations,
            "audit_entries": [dict(r) for r in audit_rows],
        }

    def _build_manifest(self, data: dict, size_bytes_uncompressed: int) -> dict:
        """Build the manifest.json payload."""
        user = data["user"]
        total_messages = sum(
            len(c["messages"]) for c in data["web_conversations"]
        )
        return {
            "format_version": FORMAT_VERSION,
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "exporter": "telegram-bot-fr6",
            "source_user": {
                "id": user.get("id"),
                "name": user.get("name"),
                "username": user.get("username"),
                "role": user.get("role"),
            },
            "stats": {
                "notes": len(data["notes"]),
                "wiki_pages": len(data["wiki_pages"]),
                "memory_kinds": len(data["user_memory"]),
                "web_conversations": len(data["web_conversations"]),
                "web_messages": total_messages,
                "audit_entries": len(data["audit_entries"]),
                "size_bytes_uncompressed": size_bytes_uncompressed,
            },
        }

    # ── Import stubs (Sub 6.2) ────────────────────────────────────────────────

    def parse_import(self, zip_bytes: bytes) -> ParsedImport:
        """Validate ZIP structure and parse manifest/data into ParsedImport.

        Raises ImportFormatError on any structural problem.
        Implemented in Sub 6.2.
        """
        raise NotImplementedError("parse_import: implemented in Sub 6.2")

    def apply_import(self, parsed: ParsedImport, *, admin_user_id: int) -> ImportResult:
        """Apply a parsed import — create user + restore all data rows.

        Transactional: rolls back Drive uploads and DB on any failure.
        Implemented in Sub 6.2.
        """
        raise NotImplementedError("apply_import: implemented in Sub 6.2")
