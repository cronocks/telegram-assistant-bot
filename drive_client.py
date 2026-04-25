"""
drive_client.py — Google Drive client dùng OAuth (account bot).

Logic folder:
1. Nếu GDRIVE_FOLDER_ID set và bot truy cập được → dùng folder đó
2. Ngược lại → search folder do bot tạo trước đây (theo tên)
3. Ngược lại → bot tạo folder mới + initiate ownership transfer

Mọi file tạo ra → tự động initiate ownership transfer tới OWNER_EMAIL (nếu bật).

Tính năng mở rộng (v5):
- Fuzzy match tên file (chỉ cần nhớ 1 phần)
- Append nội dung vào file có sẵn
- Daily journal — file nhật ký theo ngày, append entries với timestamp GMT+7
- Liệt kê N file gần nhất
- Smart search có timeframe (cho câu hỏi mơ hồ)
"""
import io
import json
import os
import base64
from datetime import timedelta, timezone
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaInMemoryUpload
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

from config import (
    GDRIVE_FOLDER_ID, CLAUDE_NOTES_FOLDER,
    OWNER_EMAIL, ENABLE_OWNERSHIP_TRANSFER,
    FUZZY_SCAN_LIMIT, LIST_RECENT_LIMIT,
)
from security import (
    validate_scope, validate_folder, validate_file_creation,
    validate_transfer_target, check_rate_limit, audit_log,
    register_trusted_folder, ALLOWED_SCOPES,
)
from timeutils import (
    now_local, today_str, time_str, filename_timestamp,
    datetime_str, daily_journal_filename,
    current_week_start, current_week_end,
)

TOKEN_FILE = "token.json"
MIME_MARKDOWN = "text/markdown"

# Cache folder ID đã được xác định (1 lần/process)
_cached_folder_id: str = ""


# ═══════════════════════════════════════════════════════════════════════════════
# CREDENTIALS & SERVICE
# ═══════════════════════════════════════════════════════════════════════════════

def _get_credentials() -> Credentials:
    """Load OAuth credentials từ env (Render) hoặc file local."""
    raw_b64 = os.environ.get("GOOGLE_OAUTH_TOKEN_B64", "").strip()

    if raw_b64:
        try:
            decoded = base64.b64decode(raw_b64).decode("utf-8")
            info = json.loads(decoded)
            creds = Credentials.from_authorized_user_info(info, list(ALLOWED_SCOPES))
        except Exception as e:
            raise RuntimeError(f"Khong decode duoc OAuth token: {e}")
    elif os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, list(ALLOWED_SCOPES))
    else:
        raise RuntimeError(
            "Khong tim thay OAuth token! "
            "Chay 'python oauth_setup.py' truoc, hoac set GOOGLE_OAUTH_TOKEN_B64."
        )

    validate_scope(creds.scopes)

    if creds.expired and creds.refresh_token:
        print("[drive] Token expired, refreshing...")
        creds.refresh(Request())
        audit_log("token_refreshed")

    return creds


def _get_service():
    """Khởi tạo Drive API service."""
    creds = _get_credentials()
    return build("drive", "v3", credentials=creds, cache_discovery=False)


# ═══════════════════════════════════════════════════════════════════════════════
# FOLDER MANAGEMENT
# ═══════════════════════════════════════════════════════════════════════════════

def _get_or_create_notes_folder() -> str:
    """
    Trả về folder ID để bot lưu/tìm ghi chú. Cache kết quả ở module level.

    Thứ tự ưu tiên:
    1. GDRIVE_FOLDER_ID đã cấu hình và bot truy cập được
    2. Folder do bot tạo trước đây (search theo tên)
    3. Tạo folder mới + initiate ownership transfer (nếu bật)
    """
    global _cached_folder_id

    if _cached_folder_id:
        return _cached_folder_id

    service = _get_service()

    # Ưu tiên 1: GDRIVE_FOLDER_ID
    if GDRIVE_FOLDER_ID:
        try:
            folder = service.files().get(
                fileId=GDRIVE_FOLDER_ID, fields="id, name"
            ).execute()
            _cached_folder_id = GDRIVE_FOLDER_ID
            register_trusted_folder(_cached_folder_id)
            print(f"[drive] Su dung folder cau hinh: {folder.get('name')} ({_cached_folder_id})")
            return _cached_folder_id
        except Exception as e:
            print(f"[drive] Khong truy cap duoc GDRIVE_FOLDER_ID: {e}")

    # Ưu tiên 2: Search folder bot đã tạo
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
        print(f"[drive] Tim thay folder bot da tao: {_cached_folder_id}")
        return _cached_folder_id

    # Ưu tiên 3: Tạo folder mới
    folder_meta = {
        "name": CLAUDE_NOTES_FOLDER,
        "mimeType": "application/vnd.google-apps.folder",
    }
    folder = service.files().create(body=folder_meta, fields="id").execute()
    _cached_folder_id = folder["id"]
    register_trusted_folder(_cached_folder_id)
    print(f"[drive] Da tao folder moi: {_cached_folder_id}")
    audit_log("folder_created", file_id=_cached_folder_id, filename=CLAUDE_NOTES_FOLDER)

    if ENABLE_OWNERSHIP_TRANSFER:
        try:
            _initiate_ownership_transfer(service, _cached_folder_id, OWNER_EMAIL)
            print(f"[drive] Da gui email transfer ownership folder toi {OWNER_EMAIL}")
        except Exception as e:
            print(f"[drive] Folder transfer warning: {e}")
            audit_log("folder_transfer_failed", file_id=_cached_folder_id, details=str(e)[:200])

    return _cached_folder_id


def test_drive_connection() -> dict:
    """Test kết nối Drive — trả về thông tin folder."""
    folder_id = _get_or_create_notes_folder()
    validate_folder(folder_id)

    service = _get_service()
    folder = service.files().get(
        fileId=folder_id, fields="id, name, mimeType",
    ).execute()
    audit_log("test_connection", file_id=folder.get("id"), filename=folder.get("name"))
    return folder


# ═══════════════════════════════════════════════════════════════════════════════
# OWNERSHIP TRANSFER
# ═══════════════════════════════════════════════════════════════════════════════

def _initiate_ownership_transfer(service, file_id: str, target_email: str):
    """Khởi tạo transfer ownership tới OWNER_EMAIL (pendingOwner pattern)."""
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
    """Wrapper — chỉ transfer nếu bật, log warning nếu lỗi (không fail)."""
    if not ENABLE_OWNERSHIP_TRANSFER:
        return
    try:
        _initiate_ownership_transfer(service, file_id, OWNER_EMAIL)
    except Exception as e:
        print(f"[drive] Transfer warning (non-fatal): {e}")
        audit_log("transfer_failed", file_id=file_id, details=str(e)[:200])


# ═══════════════════════════════════════════════════════════════════════════════
# CREATE / SAVE NOTE
# ═══════════════════════════════════════════════════════════════════════════════

def save_note(title: str, content: str, custom_filename: str = None) -> str:
    """
    Lưu ghi chú dưới dạng file .md mới.

    Args:
        title: tiêu đề (Claude tạo hoặc user truyền)
        content: nội dung
        custom_filename: nếu set → dùng tên này (đã sanitize), ko prefix timestamp
    """
    check_rate_limit()
    folder_id = _get_or_create_notes_folder()
    validate_folder(folder_id)

    if custom_filename:
        filename = custom_filename if custom_filename.endswith(".md") else f"{custom_filename}.md"
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
    media = MediaInMemoryUpload(markdown.encode("utf-8"), mimetype=MIME_MARKDOWN, resumable=False)
    file_meta = {"name": filename, "parents": [folder_id]}
    file = service.files().create(
        body=file_meta, media_body=media, fields="id, name",
    ).execute()

    file_id = file.get("id")
    audit_log("create_file", file_id=file_id, filename=filename)
    _try_transfer_ownership(service, file_id)

    return file.get("name")


# ═══════════════════════════════════════════════════════════════════════════════
# FUZZY MATCH FILE TÌM KIẾM (mới)
# ═══════════════════════════════════════════════════════════════════════════════

def find_files_fuzzy(query: str) -> list:
    """
    Tìm các file có tên CHỨA query (case-insensitive, không cần khớp full).

    Trả về list dict: [{id, name, modifiedTime}, ...] — sắp theo modifiedTime desc.
    """
    folder_id = _get_or_create_notes_folder()
    validate_folder(folder_id)

    service = _get_service()
    # Lấy tất cả file .md trong folder, sau đó filter ở client
    results = service.files().list(
        q=f"'{folder_id}' in parents and trashed=false and mimeType='{MIME_MARKDOWN}'",
        fields="files(id, name, modifiedTime)",
        orderBy="modifiedTime desc",
        pageSize=FUZZY_SCAN_LIMIT,
    ).execute()

    files = results.get("files", [])
    query_lower = query.lower().strip()

    matches = [f for f in files if query_lower in f["name"].lower()]
    audit_log("fuzzy_match", details=f"query='{query}', matched={len(matches)}/{len(files)}")
    return matches


# ═══════════════════════════════════════════════════════════════════════════════
# READ / LIST FILE (mới)
# ═══════════════════════════════════════════════════════════════════════════════

def read_file_by_id(file_id: str) -> dict:
    """Đọc 1 file theo ID. Trả về {id, name, content, modifiedTime}."""
    folder_id = _get_or_create_notes_folder()
    validate_folder(folder_id)

    service = _get_service()
    # Verify file thuộc folder hợp lệ (defense in depth)
    meta = service.files().get(
        fileId=file_id, fields="id, name, parents, modifiedTime",
    ).execute()

    if folder_id not in (meta.get("parents") or []):
        raise PermissionError(f"[SECURITY] File khong thuoc folder duoc trust: {file_id}")

    content = _read_file(service, file_id)
    audit_log("read_file", file_id=file_id, filename=meta.get("name"))
    return {
        "id": file_id,
        "name": meta.get("name"),
        "content": content,
        "modifiedTime": meta.get("modifiedTime"),
    }


def list_recent_files(limit: int = None) -> list:
    """Liệt kê N file gần nhất theo modifiedTime desc."""
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


# ═══════════════════════════════════════════════════════════════════════════════
# APPEND TO FILE (mới)
# ═══════════════════════════════════════════════════════════════════════════════

def append_to_file(file_id: str, append_content: str) -> str:
    """
    Append nội dung vào cuối file. Trả về tên file đã update.
    """
    folder_id = _get_or_create_notes_folder()
    validate_folder(folder_id)

    service = _get_service()
    # Verify file thuộc folder hợp lệ
    meta = service.files().get(
        fileId=file_id, fields="id, name, parents, mimeType",
    ).execute()

    if folder_id not in (meta.get("parents") or []):
        raise PermissionError(f"[SECURITY] File khong thuoc folder duoc trust: {file_id}")
    if meta.get("mimeType") != MIME_MARKDOWN:
        raise PermissionError(f"[SECURITY] File khong phai markdown: {meta.get('mimeType')}")

    # Đọc nội dung hiện tại + nối
    current = _read_file(service, file_id)
    if not current.endswith("\n"):
        current += "\n"
    new_content = current + append_content

    media = MediaInMemoryUpload(new_content.encode("utf-8"), mimetype=MIME_MARKDOWN, resumable=False)
    service.files().update(fileId=file_id, media_body=media).execute()

    audit_log("append_file", file_id=file_id, filename=meta.get("name"))
    return meta.get("name")


# ═══════════════════════════════════════════════════════════════════════════════
# DAILY JOURNAL (mới)
# ═══════════════════════════════════════════════════════════════════════════════

def add_to_daily_journal(content: str) -> tuple[str, str]:
    """
    Thêm 1 entry vào file nhật ký hôm nay (GMT+7).
    Tạo mới nếu chưa có. Trả về (filename, action) — action: "created" | "appended".
    """
    folder_id = _get_or_create_notes_folder()
    validate_folder(folder_id)

    filename = daily_journal_filename()  # 2026-04-25_NhatKy.md
    timestamp = time_str()                # 14:30
    new_entry = f"\n## {timestamp}\n{content}\n"

    service = _get_service()

    # Tìm file ngày hôm nay (escape ' để chống injection)
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
        # Đã có → append
        file_id = files[0]["id"]
        current = _read_file(service, file_id)
        if not current.endswith("\n"):
            current += "\n"
        updated = current + new_entry
        media = MediaInMemoryUpload(updated.encode("utf-8"), mimetype=MIME_MARKDOWN, resumable=False)
        service.files().update(fileId=file_id, media_body=media).execute()
        audit_log("daily_journal_append", file_id=file_id, filename=filename)
        return filename, "appended"

    # Chưa có → tạo mới (rate limit + validate)
    check_rate_limit()
    validate_file_creation(filename, MIME_MARKDOWN)

    markdown = f"""---
title: Nhật ký {today_str()}
date: {today_str()}
source: telegram-bot
---
{new_entry}"""

    media = MediaInMemoryUpload(markdown.encode("utf-8"), mimetype=MIME_MARKDOWN, resumable=False)
    file_meta = {"name": filename, "parents": [folder_id]}
    file = service.files().create(body=file_meta, media_body=media, fields="id, name").execute()

    file_id = file.get("id")
    audit_log("daily_journal_create", file_id=file_id, filename=filename)
    _try_transfer_ownership(service, file_id)
    return filename, "created"


def get_today_journal() -> dict:
    """Đọc file nhật ký hôm nay. Trả về None nếu chưa có."""
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
    results = service.files().list(q=query, fields="files(id, name, modifiedTime)").execute()
    files = results.get("files", [])

    if not files:
        return None

    return read_file_by_id(files[0]["id"])


# ═══════════════════════════════════════════════════════════════════════════════
# SEARCH NOTES (legacy + smart)
# ═══════════════════════════════════════════════════════════════════════════════

def search_notes(keyword: str, max_results: int = 5) -> list:
    """Tìm theo keyword (full text). Chỉ thấy file bot tạo (drive.file scope)."""
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

    notes = []
    for f in results.get("files", []):
        try:
            content = _read_file(service, f["id"])
            notes.append({
                "name": f["name"],
                "modified": f["modifiedTime"][:10],
                "content": content[:500],
            })
        except Exception as e:
            print(f"[drive] Skip file {f['id']}: {e}")

    audit_log("search_notes", details=f"keyword='{keyword}', found={len(notes)}")
    return notes


def smart_search(keywords: list, days_back: int = 0, max_per_keyword: int = 3) -> list:
    """
    Search nâng cao theo nhiều keyword + filter timeframe.
    Dùng cho câu hỏi mơ hồ — kết hợp với extract_search_intent().

    Args:
        keywords: list từ khóa
        days_back: chỉ lấy file modified trong N ngày qua (0 = không filter)
        max_per_keyword: số file tối đa lấy cho mỗi keyword
    """
    if not keywords:
        return []

    folder_id = _get_or_create_notes_folder()
    validate_folder(folder_id)

    service = _get_service()

    # Build timeframe filter
    timeframe = ""
    if days_back > 0:
        since = (now_local() - timedelta(days=days_back)).strftime("%Y-%m-%dT%H:%M:%S")
        timeframe = f"and modifiedTime > '{since}' "

    # Search từng keyword, dedupe theo file id
    seen_ids = set()
    notes = []

    for kw in keywords[:5]:  # giới hạn 5 keyword để tránh quá nhiều API call
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
                    "name": f["name"],
                    "modified": f["modifiedTime"][:10],
                    "content": content[:800],
                })
            except Exception as e:
                print(f"[drive] Skip file {f['id']}: {e}")

    audit_log("smart_search",
              details=f"keywords={keywords}, days_back={days_back}, found={len(notes)}")
    return notes


def get_current_week_notes(max_results: int = 20) -> list:
    """
    Lấy ghi chú trong tuần hiện tại (thứ 2 → chủ nhật, GMT+7).
    Dùng cho lệnh 'tom tat tuan nay'.
    """
    folder_id = _get_or_create_notes_folder()
    validate_folder(folder_id)

    service = _get_service()
    week_start = current_week_start()
    week_end = current_week_end()

    # Drive API expect RFC3339 — convert sang UTC để query chính xác
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

    notes = []
    for f in results.get("files", []):
        try:
            content = _read_file(service, f["id"])
            notes.append({
                "name": f["name"],
                "modified": f["modifiedTime"][:10],
                "content": content,
            })
        except Exception as e:
            print(f"[drive] Skip file {f['id']}: {e}")

    audit_log("get_current_week_notes",
              details=f"week={week_start.date()}..{week_end.date()}, found={len(notes)}")
    return notes


def get_recent_notes(days: int = 7, max_results: int = 5) -> list:
    """Lấy ghi chú gần đây trong N ngày (legacy — giữ cho compatibility)."""
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

    notes = []
    for f in results.get("files", []):
        try:
            content = _read_file(service, f["id"])
            notes.append({
                "name": f["name"],
                "modified": f["modifiedTime"][:10],
                "content": content,
            })
        except Exception as e:
            print(f"[drive] Skip file {f['id']}: {e}")

    audit_log("get_recent_notes", details=f"days={days}, found={len(notes)}")
    return notes


# ═══════════════════════════════════════════════════════════════════════════════
# INTERNAL HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _read_file(service, file_id: str) -> str:
    """Đọc nội dung file từ Drive (raw bytes → utf-8)."""
    request = service.files().get_media(fileId=file_id)
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return buf.getvalue().decode("utf-8", errors="ignore")
