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

    # ── Import — parse (Sub 6.2) ──────────────────────────────────────────────

    def parse_import(self, zip_bytes: bytes) -> ParsedImport:
        """Validate ZIP structure and parse manifest/data.

        Checks performed:
          - Size <= MAX_IMPORT_BYTES
          - ZIP is not corrupted
          - manifest.json present and format_version == 1
          - data.json present and parseable
          - No path-traversal filenames
          - All content files referenced in data.notes / data.wiki_pages exist in ZIP
          - Warns if the source user name conflicts with an existing active user

        Returns ParsedImport with warnings list.
        Raises ImportFormatError on any structural problem.
        """
        if len(zip_bytes) > MAX_IMPORT_BYTES:
            raise ImportFormatError(
                f"ZIP is too large ({len(zip_bytes) // (1024*1024)} MB). "
                f"Maximum allowed is {MAX_IMPORT_BYTES // (1024*1024)} MB."
            )

        try:
            zf = zipfile.ZipFile(io.BytesIO(zip_bytes), "r")
        except zipfile.BadZipFile as exc:
            raise ImportFormatError(f"Not a valid ZIP file: {exc}") from exc

        with zf:
            names = set(zf.namelist())

            # Check for path traversal.
            for name in names:
                if name.startswith("/") or ".." in name.split("/"):
                    raise ImportFormatError(
                        f"ZIP contains unsafe path: '{name}'"
                    )

            # Read manifest.
            if "manifest.json" not in names:
                raise ImportFormatError("manifest.json not found in ZIP.")
            try:
                manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
            except Exception as exc:
                raise ImportFormatError(f"Cannot parse manifest.json: {exc}") from exc

            if manifest.get("format_version") != FORMAT_VERSION:
                raise ImportFormatError(
                    f"Unsupported format_version={manifest.get('format_version')}. "
                    f"Expected {FORMAT_VERSION}."
                )

            # Read data.
            if "data.json" not in names:
                raise ImportFormatError("data.json not found in ZIP.")
            try:
                data = json.loads(zf.read("data.json").decode("utf-8"))
            except Exception as exc:
                raise ImportFormatError(f"Cannot parse data.json: {exc}") from exc

            # Validate that all Drive content files referenced in data exist in ZIP.
            warnings: list[str] = []
            notes_content: dict[str, bytes] = {}
            wiki_content: dict[str, bytes] = {}

            for note in data.get("notes", []):
                fid = note.get("drive_file_id", "")
                path = note.get("content_path") or f"notes/{fid}.md"
                if path not in names:
                    warnings.append(
                        f"Content file missing for note '{note.get('title', fid)}' "
                        f"(expected '{path}'). Note will be imported with empty content."
                    )
                    notes_content[fid] = b""
                else:
                    notes_content[fid] = zf.read(path)

            for page in data.get("wiki_pages", []):
                fid = page.get("drive_file_id", "")
                slug = page.get("slug", "")
                path = page.get("content_path") or f"wiki/{slug}.md"
                if path not in names:
                    warnings.append(
                        f"Content file missing for wiki page '{page.get('topic', slug)}' "
                        f"(expected '{path}'). Page will be imported with empty content."
                    )
                    wiki_content[slug] = b""
                else:
                    wiki_content[slug] = zf.read(path)

        # Check for name conflict with existing active user.
        source_name = (data.get("user") or {}).get("name", "")
        if source_name:
            conflict = self._conn.execute(
                "SELECT id FROM users WHERE name = ? AND deleted_at IS NULL",
                (source_name,),
            ).fetchone()
            if conflict:
                warnings.append(
                    f"User name '{source_name}' already exists in this system "
                    f"(id={conflict['id']}). Import will still create a new user row; "
                    "rename the existing user first if this causes a UNIQUE constraint error."
                )

        return ParsedImport(
            manifest=manifest,
            data=data,
            notes_content=notes_content,
            wiki_content=wiki_content,
            warnings=warnings,
        )

    # ── Import — apply (Sub 6.2) ──────────────────────────────────────────────

    def apply_import(self, parsed: ParsedImport, *, admin_user_id: int) -> ImportResult:
        """Apply a parsed import transactionally.

        Steps (in order):
          a. INSERT user row (new id assigned by SQLite AUTOINCREMENT)
          b. INSERT channel_bindings (skip conflicting (channel, chat_id) pairs)
          c. INSERT user_quotas
          d. Upload each note content to Drive → INSERT notes index row
          e. Upload each wiki page content to Drive → INSERT wiki_pages index row
             + append to wiki index (_index.md)
          f. INSERT user_memory rows
          g. INSERT web_conversations + web_messages
          h. Resolve parent_links by name → INSERT parent_links
          i. Emit audit data_import

        On any failure: best-effort delete uploaded Drive files, rollback DB
        transaction, emit data_import_failed.
        """
        data = parsed.data
        uploaded_drive_ids: list[str] = []   # track for rollback

        try:
            with self._conn:
                new_user_id = self._import_user(data["user"])
                cb_warnings = self._import_channel_bindings(
                    data.get("channel_bindings", []), new_user_id
                )
                self._import_quota(data.get("quota"), new_user_id)

                note_id_map = self._import_notes(
                    data.get("notes", []),
                    parsed.notes_content,
                    new_user_id,
                    uploaded_drive_ids,
                )
                wiki_id_map = self._import_wiki_pages(
                    data.get("wiki_pages", []),
                    parsed.wiki_content,
                    new_user_id,
                    uploaded_drive_ids,
                )
                self._import_memory(data.get("user_memory", []), new_user_id)
                conv_count, msg_count = self._import_conversations(
                    data.get("web_conversations", []), new_user_id
                )
                pl_warnings = self._import_parent_links(
                    data.get("parent_links_as_child", []), new_user_id
                )

                all_warnings = parsed.warnings + cb_warnings + pl_warnings
                counts = {
                    "notes": len(note_id_map),
                    "wiki_pages": len(wiki_id_map),
                    "web_conversations": conv_count,
                    "web_messages": msg_count,
                }
                id_map: dict[str, dict] = {
                    "notes": note_id_map,
                    "wiki_pages": wiki_id_map,
                }

                self._audit.log(
                    actor_user_id=admin_user_id,
                    action="data_import",
                    target_type="user",
                    target_id=new_user_id,
                    payload={
                        "source_name": parsed.manifest.get("source_user", {}).get("name"),
                        "id_map": id_map,
                        "items_imported": counts,
                    },
                )

        except Exception as exc:
            # Best-effort rollback of uploaded Drive files.
            for fid in uploaded_drive_ids:
                try:
                    self._notes.delete_file(fid)
                except Exception as del_exc:
                    logger.warning("Import rollback: cannot delete Drive file %s: %s", fid, del_exc)

            self._audit.log(
                actor_user_id=admin_user_id,
                action="data_import_failed",
                payload={"error": str(exc), "stage": "apply"},
            )
            raise

        return ImportResult(
            new_user_id=new_user_id,
            counts=counts,
            id_map=id_map,
            warnings=all_warnings,
        )

    # ── Import helpers ────────────────────────────────────────────────────────

    def _import_user(self, user_data: dict) -> int:
        """Insert a new user row. Returns the new user_id (SQLite AUTOINCREMENT)."""
        self._conn.execute(
            """
            INSERT INTO users (name, role, username, birthdate,
                               password_hash, must_change_password)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                user_data.get("name"),
                user_data.get("role", "member"),
                user_data.get("username"),
                user_data.get("birthdate"),
                user_data.get("password_hash"),
                user_data.get("must_change_password", 0),
            ),
        )
        return self._conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    def _import_channel_bindings(
        self, bindings: list[dict], new_user_id: int
    ) -> list[str]:
        """Insert channel bindings; skip conflicting (channel, chat_id) pairs."""
        warnings: list[str] = []
        for cb in bindings:
            try:
                self._conn.execute(
                    "INSERT INTO channel_bindings (user_id, channel, chat_id)"
                    " VALUES (?, ?, ?)",
                    (new_user_id, cb.get("channel"), cb.get("chat_id")),
                )
            except sqlite3.IntegrityError:
                warnings.append(
                    f"Channel binding ({cb.get('channel')}, {cb.get('chat_id')}) "
                    "already bound to another user — skipped."
                )
        return warnings

    def _import_quota(self, quota: dict | None, new_user_id: int) -> None:
        """Insert user_quotas row if present in the export."""
        if quota is None:
            return
        self._conn.execute(
            """
            INSERT OR IGNORE INTO user_quotas
                (user_id, monthly_token_limit, used_tokens, month)
            VALUES (?, ?, 0, ?)
            """,
            (
                new_user_id,
                quota.get("monthly_token_limit", 0),
                quota.get("month") or datetime.now(timezone.utc).strftime("%Y-%m"),
            ),
        )

    def _import_notes(
        self,
        notes: list[dict],
        notes_content: dict[str, bytes],
        new_user_id: int,
        uploaded_ids: list[str],
    ) -> dict[str, str]:
        """Upload each note to Drive and insert a notes index row.

        Returns {old_drive_file_id: new_drive_file_id}.
        """
        id_map: dict[str, str] = {}
        for note in notes:
            old_fid = note.get("drive_file_id", "")
            content_bytes = notes_content.get(old_fid, b"")
            content_str = content_bytes.decode("utf-8", errors="replace")
            title = note.get("title") or "Imported note"

            _filename, new_fid = self._notes.save_note(title, content_str)
            uploaded_ids.append(new_fid)

            self._note_index.add_note(
                drive_file_id=new_fid,
                owner_user_id=new_user_id,
                kind=note.get("kind", "note"),
                title=note.get("title"),
                scope=note.get("scope", "private"),
            )
            id_map[old_fid] = new_fid
        return id_map

    def _import_wiki_pages(
        self,
        pages: list[dict],
        wiki_content: dict[str, bytes],
        new_user_id: int,
        uploaded_ids: list[str],
    ) -> dict[str, str]:
        """Upload each wiki page to Drive, insert wiki_pages index row, and update index.

        Returns {old_drive_file_id: new_drive_file_id}.
        """
        id_map: dict[str, str] = {}
        for page in pages:
            old_fid = page.get("drive_file_id", "")
            slug = page.get("slug", "")
            topic = page.get("topic", slug)
            content_bytes = wiki_content.get(slug, b"")
            content_str = content_bytes.decode("utf-8", errors="replace")

            _filename, new_fid = self._wiki.save_page(topic, content_str)
            uploaded_ids.append(new_fid)

            self._note_index.add_wiki_page(
                drive_file_id=new_fid,
                owner_user_id=new_user_id,
                topic=topic,
                slug=slug,
                scope=page.get("scope", "everyone"),
            )
            # Register in wiki _index.md with empty TLDR (admin can regenerate).
            try:
                self._wiki.add_to_index(topic, slug, "other", "")
            except Exception as exc:
                logger.warning("Import: cannot update wiki index for '%s': %s", slug, exc)

            id_map[old_fid] = new_fid
        return id_map

    def _import_memory(self, memory_rows: list[dict], new_user_id: int) -> None:
        """Upsert user_memory rows for the new user."""
        for row in memory_rows:
            kind = row.get("kind", "memory")
            content = row.get("content", "")
            self._memory_store.set(new_user_id, kind, content)

    def _import_conversations(
        self, conversations: list[dict], new_user_id: int
    ) -> tuple[int, int]:
        """Insert web_conversations + web_messages. Returns (conv_count, msg_count)."""
        conv_count = 0
        msg_count = 0
        for conv in conversations:
            new_conv_id = self._web_conv_store.create(new_user_id)
            title = conv.get("title")
            if title:
                self._web_conv_store.rename(new_conv_id, title)
            for msg in conv.get("messages", []):
                self._web_conv_store.add_message(
                    new_conv_id,
                    role=msg.get("role", "user"),
                    text=msg.get("text", ""),
                )
                msg_count += 1
            conv_count += 1
        return conv_count, msg_count

    def _import_parent_links(
        self, links_as_child: list[dict], new_user_id: int
    ) -> list[str]:
        """Resolve parent links by name and insert active ones.

        Only imports currently-active links (active=1) to avoid cluttering history.
        Warns and skips if the named parent cannot be found.
        """
        warnings: list[str] = []
        for link in links_as_child:
            if not link.get("active"):
                continue  # skip historical inactive links
            parent_name = link.get("parent_name", "")
            if not parent_name:
                continue
            parent_row = self._conn.execute(
                "SELECT id FROM users WHERE name = ? AND deleted_at IS NULL",
                (parent_name,),
            ).fetchone()
            if parent_row is None:
                warnings.append(
                    f"Parent '{parent_name}' not found in this system — "
                    "parent link skipped."
                )
                continue
            parent_id = parent_row["id"]
            try:
                self._conn.execute(
                    "INSERT INTO parent_links (user_id, parent_id, set_by) VALUES (?, ?, ?)",
                    (new_user_id, parent_id, new_user_id),
                )
            except sqlite3.IntegrityError as exc:
                warnings.append(
                    f"Cannot insert parent link to '{parent_name}': {exc}"
                )
        return warnings
