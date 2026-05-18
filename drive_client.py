"""drive_client.py — Google Drive backed implementation of NoteStore.

Folder resolution:
1. If GDRIVE_FOLDER_ID is configured and the bot can access it, use that folder.
2. Otherwise search for a folder previously created by the bot (by name).
3. Otherwise create a new folder and initiate an ownership transfer.

Every file created automatically initiates an ownership transfer to OWNER_EMAIL
(when ENABLE_OWNERSHIP_TRANSFER is on).

Module-level helpers (`_get_service`, `_get_or_create_notes_folder`, `_read_file`,
`MIME_MARKDOWN`) are kept importable so the wiki adapter can share infrastructure.
"""
import base64
import io
import json
import os
from datetime import timedelta, timezone

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaInMemoryUpload, MediaIoBaseDownload

from config import (
    CLAUDE_NOTES_FOLDER,
    ENABLE_OWNERSHIP_TRANSFER,
    FUZZY_SCAN_LIMIT,
    GDRIVE_FOLDER_ID,
    LIST_RECENT_LIMIT,
    OWNER_EMAIL,
)
from security import (
    ALLOWED_SCOPES,
    audit_log,
    check_rate_limit,
    register_trusted_folder,
    validate_file_creation,
    validate_folder,
    validate_scope,
    validate_transfer_target,
)
from timeutils import (
    current_week_end,
    current_week_start,
    daily_journal_filename,
    datetime_str,
    filename_timestamp,
    now_local,
    time_str,
    today_str,
)

TOKEN_FILE = "token.json"
MIME_MARKDOWN = "text/markdown"

# Cached resolved folder id (one resolution per process).
_cached_folder_id: str = ""


# ═══════════════════════════════════════════════════════════════════════════════
# CREDENTIALS & SERVICE (module-level, shared with wiki adapter)
# ═══════════════════════════════════════════════════════════════════════════════

def _get_credentials() -> Credentials:
    """Load OAuth credentials from env (Render-style b64) or a local token file."""
    raw_b64 = os.environ.get("GOOGLE_OAUTH_TOKEN_B64", "").strip()

    if raw_b64:
        try:
            decoded = base64.b64decode(raw_b64).decode("utf-8")
            info = json.loads(decoded)
            creds = Credentials.from_authorized_user_info(info, list(ALLOWED_SCOPES))
        except Exception as e:
            raise RuntimeError(f"Cannot decode OAuth token: {e}")
    elif os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, list(ALLOWED_SCOPES))
    else:
        raise RuntimeError(
            "OAuth token not found! "
            "Run 'python oauth_setup.py' first or set GOOGLE_OAUTH_TOKEN_B64."
        )

    validate_scope(creds.scopes)

    if creds.expired and creds.refresh_token:
        print("[drive] Token expired, refreshing...")
        creds.refresh(Request())
        audit_log("token_refreshed")

    return creds


def _get_service():
    """Build a Drive API v3 service from credentials."""
    creds = _get_credentials()
    return build("drive", "v3", credentials=creds, cache_discovery=False)


# ═══════════════════════════════════════════════════════════════════════════════
# FOLDER MANAGEMENT (module-level, shared with wiki adapter)
# ═══════════════════════════════════════════════════════════════════════════════

def _get_or_create_notes_folder() -> str:
    """Resolve the bot's notes folder id, caching the result for the process.

    Priority:
      1. GDRIVE_FOLDER_ID if configured and accessible
      2. Folder previously created by the bot (search by name)
      3. Create a new folder + initiate ownership transfer (if enabled)
    """
    global _cached_folder_id

    if _cached_folder_id:
        return _cached_folder_id

    service = _get_service()

    # Priority 1: configured folder id.
    if GDRIVE_FOLDER_ID:
        try:
            folder = service.files().get(
                fileId=GDRIVE_FOLDER_ID, fields="id, name"
            ).execute()
            _cached_folder_id = GDRIVE_FOLDER_ID
            register_trusted_folder(_cached_folder_id)
            print(f"[drive] Using configured folder: {folder.get('name')} ({_cached_folder_id})")
            return _cached_folder_id
        except Exception as e:
            print(f"[drive] Cannot access GDRIVE_FOLDER_ID: {e}")

    # Priority 2: folder previously created by the bot.
    query = (
        f"name='{CLAUDE_NOTES_FOLDER}' "
        f"and mimeType='application/vnd.google-apps.folder' "
        f"and trashed=false"
    )
    results = service.files().list(q=query, fields="files(id, name)").execute()
    files = results.get("files", [])

    if files:
        _cached_folder_id = files[0]["id"]
        register_trusted_folder(_cached_folder_id)
        print(f"[drive] Found existing bot folder: {_cached_folder_id}")
        return _cached_folder_id

    # Priority 3: create a new folder.
    folder_meta = {
        "name": CLAUDE_NOTES_FOLDER,
        "mimeType": "application/vnd.google-apps.folder",
    }
    folder = service.files().create(body=folder_meta, fields="id").execute()
    _cached_folder_id = folder["id"]
    register_trusted_folder(_cached_folder_id)
    print(f"[drive] Created new folder: {_cached_folder_id}")
    audit_log("folder_created", file_id=_cached_folder_id, filename=CLAUDE_NOTES_FOLDER)

    if ENABLE_OWNERSHIP_TRANSFER:
        try:
            _initiate_ownership_transfer(service, _cached_folder_id, OWNER_EMAIL)
            print(f"[drive] Sent folder ownership transfer email to {OWNER_EMAIL}")
        except Exception as e:
            print(f"[drive] Folder transfer warning: {e}")
            audit_log("folder_transfer_failed", file_id=_cached_folder_id, details=str(e)[:200])

    return _cached_folder_id


# ═══════════════════════════════════════════════════════════════════════════════
# OWNERSHIP TRANSFER (module-level)
# ═══════════════════════════════════════════════════════════════════════════════

def _initiate_ownership_transfer(service, file_id: str, target_email: str):
    """Issue a pending-owner permission so OWNER_EMAIL can accept ownership."""
    validate_transfer_target(target_email)

    service.permissions().create(
        fileId=file_id,
        body={
            "type": "user",
            "role": "writer",
            "emailAddress": target_email,
            "pendingOwner": True,
        },
        sendNotificationEmail=True,
        fields="id",
    ).execute()
    audit_log("transfer_initiated", file_id=file_id, user=target_email)


def _try_transfer_ownership(service, file_id: str):
    """Best-effort ownership transfer; never raises."""
    if not ENABLE_OWNERSHIP_TRANSFER:
        return
    try:
        _initiate_ownership_transfer(service, file_id, OWNER_EMAIL)
    except Exception as e:
        print(f"[drive] Transfer warning (non-fatal): {e}")
        audit_log("transfer_failed", file_id=file_id, details=str(e)[:200])


# ═══════════════════════════════════════════════════════════════════════════════
# READ HELPER (module-level, shared with wiki adapter)
# ═══════════════════════════════════════════════════════════════════════════════

def _read_file(service, file_id: str) -> str:
    """Download a Drive file's content as UTF-8 text."""
    request = service.files().get_media(fileId=file_id)
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return buf.getvalue().decode("utf-8", errors="ignore")


# ═══════════════════════════════════════════════════════════════════════════════
# DriveNoteStore — NoteStore impl
# ═══════════════════════════════════════════════════════════════════════════════

class DriveNoteStore:
    """NoteStore impl backed by Google Drive.

    The constructor takes no arguments; all configuration is read from the
    `config` module. State (credentials, folder id) is cached at module level
    so multiple instances share the same resolution.
    """

    # ─── Connection check ────────────────────────────────────────────────────

    def test_connection(self) -> dict:
        """Verify Drive connectivity by resolving and fetching the notes folder."""
        folder_id = _get_or_create_notes_folder()
        validate_folder(folder_id)

        service = _get_service()
        folder = service.files().get(
            fileId=folder_id, fields="id, name, mimeType",
        ).execute()
        audit_log("test_connection", file_id=folder.get("id"), filename=folder.get("name"))
        return folder

    # ─── Create / save ───────────────────────────────────────────────────────

    def save_note(
        self, title: str, content: str, custom_filename: str | None = None
    ) -> tuple[str, str]:
        """Save a new note as a markdown file.

        Args:
            title: human title (may be Claude-generated or user-supplied).
            content: note body.
            custom_filename: if provided, use this name verbatim (already sanitized)
                instead of prefixing a timestamp.

        Returns (filename, drive_file_id).
        """
        check_rate_limit()
        folder_id = _get_or_create_notes_folder()
        validate_folder(folder_id)

        if custom_filename:
            filename = (
                custom_filename
                if custom_filename.endswith(".md")
                else f"{custom_filename}.md"
            )
        else:
            safe_title = title.replace("/", "-").replace("\\", "-").strip()[:40]
            if not safe_title:
                safe_title = "untitled"
            filename = f"{filename_timestamp()}_{safe_title}.md"

        validate_file_creation(filename, MIME_MARKDOWN)

        markdown = f"""---
title: {title}
date: {datetime_str()}
source: telegram-bot
---

{content}
"""
        service = _get_service()
        media = MediaInMemoryUpload(
            markdown.encode("utf-8"), mimetype=MIME_MARKDOWN, resumable=False,
        )
        file_meta = {"name": filename, "parents": [folder_id]}
        file = service.files().create(
            body=file_meta, media_body=media, fields="id, name",
        ).execute()

        file_id = file.get("id")
        audit_log("create_file", file_id=file_id, filename=filename)
        _try_transfer_ownership(service, file_id)

        return file.get("name"), file_id

    # ─── Fuzzy filename match ────────────────────────────────────────────────

    def find_files_fuzzy(self, query: str) -> list[dict]:
        """Return files whose name contains `query` (case-insensitive)."""
        folder_id = _get_or_create_notes_folder()
        validate_folder(folder_id)

        service = _get_service()
        # Fetch all .md files in the folder, then filter client-side.
        results = service.files().list(
            q=f"'{folder_id}' in parents and trashed=false and mimeType='{MIME_MARKDOWN}'",
            fields="files(id, name, modifiedTime)",
            orderBy="modifiedTime desc",
            pageSize=FUZZY_SCAN_LIMIT,
        ).execute()

        files = results.get("files", [])
        query_lower = query.lower().strip()

        matches = [f for f in files if query_lower in f["name"].lower()]
        audit_log(
            "fuzzy_match",
            details=f"query='{query}', matched={len(matches)}/{len(files)}",
        )
        return matches

    # ─── Read / list ─────────────────────────────────────────────────────────

    def read_file_by_id(self, file_id: str) -> dict:
        """Read one file by its id, verifying it belongs to the bot's folder."""
        folder_id = _get_or_create_notes_folder()
        validate_folder(folder_id)

        service = _get_service()
        # Defense in depth: confirm parent folder before reading.
        meta = service.files().get(
            fileId=file_id, fields="id, name, parents, modifiedTime",
        ).execute()

        if folder_id not in (meta.get("parents") or []):
            raise PermissionError(
                f"[SECURITY] File does not belong to trusted folder: {file_id}"
            )

        content = _read_file(service, file_id)
        audit_log("read_file", file_id=file_id, filename=meta.get("name"))
        return {
            "id": file_id,
            "name": meta.get("name"),
            "content": content,
            "modifiedTime": meta.get("modifiedTime"),
        }

    def list_recent_files(self, limit: int | None = None) -> list[dict]:
        """List the N most recently modified files in the notes folder."""
        if limit is None:
            limit = LIST_RECENT_LIMIT

        folder_id = _get_or_create_notes_folder()
        validate_folder(folder_id)

        service = _get_service()
        results = service.files().list(
            q=f"'{folder_id}' in parents and trashed=false and mimeType='{MIME_MARKDOWN}'",
            fields="files(id, name, modifiedTime)",
            orderBy="modifiedTime desc",
            pageSize=limit,
        ).execute()

        files = results.get("files", [])
        audit_log("list_recent", details=f"limit={limit}, found={len(files)}")
        return files

    # ─── Append ──────────────────────────────────────────────────────────────

    def append_to_file(self, file_id: str, append_content: str) -> str:
        """Append content to the end of an existing file."""
        folder_id = _get_or_create_notes_folder()
        validate_folder(folder_id)

        service = _get_service()
        meta = service.files().get(
            fileId=file_id, fields="id, name, parents, mimeType",
        ).execute()

        if folder_id not in (meta.get("parents") or []):
            raise PermissionError(
                f"[SECURITY] File does not belong to trusted folder: {file_id}"
            )
        if meta.get("mimeType") != MIME_MARKDOWN:
            raise PermissionError(
                f"[SECURITY] File is not markdown: {meta.get('mimeType')}"
            )

        current = _read_file(service, file_id)
        if not current.endswith("\n"):
            current += "\n"
        new_content = current + append_content

        media = MediaInMemoryUpload(
            new_content.encode("utf-8"), mimetype=MIME_MARKDOWN, resumable=False,
        )
        service.files().update(fileId=file_id, media_body=media).execute()

        audit_log("append_file", file_id=file_id, filename=meta.get("name"))
        return meta.get("name")

    # ─── Daily journal ───────────────────────────────────────────────────────

    def add_to_daily_journal(self, content: str) -> tuple[str, str, str]:
        """Append (or create) today's journal entry. Returns (filename, action, drive_file_id)."""
        folder_id = _get_or_create_notes_folder()
        validate_folder(folder_id)

        filename = daily_journal_filename()    # e.g. 2026-04-25_NhatKy.md
        timestamp = time_str()                  # e.g. 14:30
        new_entry = f"\n## {timestamp}\n{content}\n"

        service = _get_service()

        # Look for today's file (escape ' to avoid query injection).
        safe_filename = filename.replace("'", "\\'")
        query = (
            f"name='{safe_filename}' "
            f"and '{folder_id}' in parents "
            f"and trashed=false "
            f"and mimeType='{MIME_MARKDOWN}'"
        )
        results = service.files().list(q=query, fields="files(id, name)").execute()
        files = results.get("files", [])

        if files:
            # Append branch.
            file_id = files[0]["id"]
            current = _read_file(service, file_id)
            if not current.endswith("\n"):
                current += "\n"
            updated = current + new_entry
            media = MediaInMemoryUpload(
                updated.encode("utf-8"), mimetype=MIME_MARKDOWN, resumable=False,
            )
            service.files().update(fileId=file_id, media_body=media).execute()
            audit_log("daily_journal_append", file_id=file_id, filename=filename)
            return filename, "appended", file_id

        # Create branch.
        check_rate_limit()
        validate_file_creation(filename, MIME_MARKDOWN)

        markdown = f"""---
title: Nhật ký {today_str()}
date: {today_str()}
source: telegram-bot
---
{new_entry}"""

        media = MediaInMemoryUpload(
            markdown.encode("utf-8"), mimetype=MIME_MARKDOWN, resumable=False,
        )
        file_meta = {"name": filename, "parents": [folder_id]}
        file = service.files().create(
            body=file_meta, media_body=media, fields="id, name",
        ).execute()

        file_id = file.get("id")
        audit_log("daily_journal_create", file_id=file_id, filename=filename)
        _try_transfer_ownership(service, file_id)
        return filename, "created", file_id

    def get_today_journal(self) -> dict | None:
        """Return today's journal file content, or None if not created yet."""
        folder_id = _get_or_create_notes_folder()
        validate_folder(folder_id)

        filename = daily_journal_filename()
        safe_filename = filename.replace("'", "\\'")

        service = _get_service()
        query = (
            f"name='{safe_filename}' "
            f"and '{folder_id}' in parents "
            f"and trashed=false "
            f"and mimeType='{MIME_MARKDOWN}'"
        )
        results = service.files().list(
            q=query, fields="files(id, name, modifiedTime)",
        ).execute()
        files = results.get("files", [])

        if not files:
            return None

        return self.read_file_by_id(files[0]["id"])

    # ─── Search ──────────────────────────────────────────────────────────────

    def search_notes(self, keyword: str, max_results: int = 5) -> list[dict]:
        """Drive full-text search scoped to the bot's folder."""
        folder_id = _get_or_create_notes_folder()
        validate_folder(folder_id)

        service = _get_service()
        safe_keyword = keyword.replace("'", "\\'")
        query = (
            f"fullText contains '{safe_keyword}' "
            f"and '{folder_id}' in parents "
            f"and trashed=false"
        )
        results = service.files().list(
            q=query,
            fields="files(id, name, modifiedTime)",
            orderBy="modifiedTime desc",
            pageSize=max_results,
        ).execute()

        notes: list[dict] = []
        for f in results.get("files", []):
            try:
                content = _read_file(service, f["id"])
                notes.append({
                    "id": f["id"],
                    "name": f["name"],
                    "modified": f["modifiedTime"][:10],
                    "content": content[:500],
                })
            except Exception as e:
                print(f"[drive] Skip file {f['id']}: {e}")

        audit_log("search_notes", details=f"keyword='{keyword}', found={len(notes)}")
        return notes

    def smart_search(
        self,
        keywords: list[str],
        days_back: int = 0,
        max_per_keyword: int = 3,
    ) -> list[dict]:
        """Multi-keyword search with optional timeframe filter.

        Pairs with LLMClient.extract_search_intent for free-form questions.
        """
        if not keywords:
            return []

        folder_id = _get_or_create_notes_folder()
        validate_folder(folder_id)

        service = _get_service()

        # Build optional timeframe filter.
        timeframe = ""
        if days_back > 0:
            since = (now_local() - timedelta(days=days_back)).strftime("%Y-%m-%dT%H:%M:%S")
            timeframe = f"and modifiedTime > '{since}' "

        # Search per keyword, deduplicating by file id.
        seen_ids: set[str] = set()
        notes: list[dict] = []

        for kw in keywords[:5]:  # cap at 5 keywords to bound API calls
            safe_kw = kw.replace("'", "\\'")
            query = (
                f"fullText contains '{safe_kw}' "
                f"and '{folder_id}' in parents "
                f"and trashed=false "
                f"{timeframe}"
            )
            try:
                results = service.files().list(
                    q=query,
                    fields="files(id, name, modifiedTime)",
                    orderBy="modifiedTime desc",
                    pageSize=max_per_keyword,
                ).execute()
            except Exception as e:
                print(f"[drive] smart_search error for '{kw}': {e}")
                continue

            for f in results.get("files", []):
                if f["id"] in seen_ids:
                    continue
                seen_ids.add(f["id"])
                try:
                    content = _read_file(service, f["id"])
                    notes.append({
                        "id": f["id"],
                        "name": f["name"],
                        "modified": f["modifiedTime"][:10],
                        "content": content[:800],
                    })
                except Exception as e:
                    print(f"[drive] Skip file {f['id']}: {e}")

        audit_log(
            "smart_search",
            details=f"keywords={keywords}, days_back={days_back}, found={len(notes)}",
        )
        return notes

    def get_current_week_notes(self, max_results: int = 20) -> list[dict]:
        """All notes modified during the current local week (Mon..Sun, GMT+7)."""
        folder_id = _get_or_create_notes_folder()
        validate_folder(folder_id)

        service = _get_service()
        week_start = current_week_start()
        week_end = current_week_end()

        # Drive expects RFC3339; convert local times to UTC for a precise range.
        start_utc = week_start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        end_utc = week_end.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

        query = (
            f"modifiedTime >= '{start_utc}' "
            f"and modifiedTime <= '{end_utc}' "
            f"and '{folder_id}' in parents "
            f"and trashed=false "
            f"and mimeType='{MIME_MARKDOWN}'"
        )
        results = service.files().list(
            q=query,
            fields="files(id, name, modifiedTime)",
            orderBy="modifiedTime desc",
            pageSize=max_results,
        ).execute()

        notes: list[dict] = []
        for f in results.get("files", []):
            try:
                content = _read_file(service, f["id"])
                notes.append({
                    "id": f["id"],
                    "name": f["name"],
                    "modified": f["modifiedTime"][:10],
                    "content": content,
                })
            except Exception as e:
                print(f"[drive] Skip file {f['id']}: {e}")

        audit_log(
            "get_current_week_notes",
            details=f"week={week_start.date()}..{week_end.date()}, found={len(notes)}",
        )
        return notes

    def get_recent_notes(self, days: int = 7, max_results: int = 5) -> list[dict]:
        """Notes modified within the last N days. Legacy helper kept for compatibility."""
        folder_id = _get_or_create_notes_folder()
        validate_folder(folder_id)

        service = _get_service()
        since = (now_local() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
        query = (
            f"modifiedTime > '{since}' "
            f"and '{folder_id}' in parents "
            f"and trashed=false"
        )
        results = service.files().list(
            q=query,
            fields="files(id, name, modifiedTime)",
            orderBy="modifiedTime desc",
            pageSize=max_results,
        ).execute()

        notes: list[dict] = []
        for f in results.get("files", []):
            try:
                content = _read_file(service, f["id"])
                notes.append({
                    "id": f["id"],
                    "name": f["name"],
                    "modified": f["modifiedTime"][:10],
                    "content": content,
                })
            except Exception as e:
                print(f"[drive] Skip file {f['id']}: {e}")

        audit_log("get_recent_notes", details=f"days={days}, found={len(notes)}")
        return notes
