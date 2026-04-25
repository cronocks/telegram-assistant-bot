"""
drive_client.py — Google Drive client dùng OAuth (account bot).

Logic folder:
1. Nếu GDRIVE_FOLDER_ID set và bot truy cập được → dùng folder đó
2. Ngược lại → search folder do bot tạo trước đây (theo tên)
3. Ngược lại → bot tạo folder mới + initiate ownership transfer

Mọi file tạo ra → tự động initiate ownership transfer tới OWNER_EMAIL.
"""
import io
import json
import os
import base64
from datetime import datetime, timedelta
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaInMemoryUpload
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

from config import (
    GDRIVE_FOLDER_ID, CLAUDE_NOTES_FOLDER,
    OWNER_EMAIL, ENABLE_OWNERSHIP_TRANSFER,
)
from security import (
    validate_scope, validate_folder, validate_file_creation,
    validate_transfer_target, check_rate_limit, audit_log,
    register_trusted_folder, ALLOWED_SCOPES,
)

TOKEN_FILE = "token.json"

# Cache folder ID đã được xác định (1 lần/process)
_cached_folder_id: str = ""


def _get_credentials() -> Credentials:
    """Load OAuth credentials từ env (Render) hoặc file local."""
    raw_b64 = os.environ.get("GOOGLE_OAUTH_TOKEN_B64", "").strip()

    if raw_b64:
        print(f"[drive] Found GOOGLE_OAUTH_TOKEN_B64, length={len(raw_b64)}")
        try:
            decoded = base64.b64decode(raw_b64).decode("utf-8")
            info = json.loads(decoded)
            creds = Credentials.from_authorized_user_info(info, list(ALLOWED_SCOPES))
        except Exception as e:
            raise RuntimeError(f"Khong decode duoc OAuth token: {e}")
    elif os.path.exists(TOKEN_FILE):
        print(f"[drive] Reading from local {TOKEN_FILE}")
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, list(ALLOWED_SCOPES))
    else:
        raise RuntimeError(
            "Khong tim thay OAuth token! "
            "Chay 'python oauth_setup.py' truoc, hoac set GOOGLE_OAUTH_TOKEN_B64."
        )

    # Lớp 1: Validate scope ngay khi load
    validate_scope(creds.scopes)

    # Refresh access token nếu hết hạn
    if creds.expired and creds.refresh_token:
        print("[drive] Token expired, refreshing...")
        creds.refresh(Request())
        audit_log("token_refreshed")

    return creds


def _get_service():
    """Khởi tạo Drive API service."""
    creds = _get_credentials()
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _get_or_create_notes_folder() -> str:
    """
    Trả về folder ID để bot lưu/tìm ghi chú.
    Cache kết quả ở cấp module — không gọi lại API mỗi request.

    Thứ tự ưu tiên:
    1. GDRIVE_FOLDER_ID đã cấu hình và bot truy cập được
    2. Folder do bot tạo trước đây (search theo tên)
    3. Tạo folder mới + initiate ownership transfer (nếu bật)
    """
    global _cached_folder_id

    if _cached_folder_id:
        return _cached_folder_id

    service = _get_service()

    # Ưu tiên 1: Dùng GDRIVE_FOLDER_ID nếu set và truy cập được
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
            print(f"[drive] Se search hoac tao folder moi...")

    # Ưu tiên 2: Search folder bot đã tạo trước đây (chỉ thấy được file/folder do bot tạo với drive.file scope)
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
    audit_log("folder_created",
              file_id=_cached_folder_id,
              filename=CLAUDE_NOTES_FOLDER)

    # Initiate ownership transfer cho folder (1 lần duy nhất)
    if ENABLE_OWNERSHIP_TRANSFER:
        try:
            _initiate_ownership_transfer(service, _cached_folder_id, OWNER_EMAIL)
            print(f"[drive] Da gui email transfer ownership folder toi {OWNER_EMAIL}")
            print(f"[drive] Hay vao Gmail accept de folder thuoc tai khoan chinh")
        except Exception as e:
            print(f"[drive] Folder transfer warning: {e}")
            audit_log("folder_transfer_failed",
                      file_id=_cached_folder_id,
                      details=str(e)[:200])

    return _cached_folder_id


def test_drive_connection() -> dict:
    """Test kết nối Drive — trả về thông tin folder."""
    folder_id = _get_or_create_notes_folder()
    validate_folder(folder_id)

    service = _get_service()
    folder = service.files().get(
        fileId=folder_id,
        fields="id, name, mimeType",
    ).execute()
    audit_log("test_connection",
              file_id=folder.get("id"),
              filename=folder.get("name"))
    return folder


def save_note(title: str, content: str) -> str:
    """Lưu ghi chú dưới dạng file .md, áp dụng đầy đủ security layers."""
    # Lớp 5: Rate limit
    check_rate_limit()

    # Lớp 2: Lấy folder hợp lệ và validate
    folder_id = _get_or_create_notes_folder()
    validate_folder(folder_id)

    # Build filename và nội dung
    now = datetime.now()
    safe_title = title.replace("/", "-").replace("\\", "-").strip()[:40]
    if not safe_title:
        safe_title = "untitled"
    filename = f"{now.strftime('%Y-%m-%d_%H%M')}_{safe_title}.md"
    mimetype = "text/markdown"

    # Lớp 3: Validate file type
    validate_file_creation(filename, mimetype)

    markdown = f"""---
title: {title}
date: {now.strftime('%Y-%m-%d %H:%M')}
source: telegram-bot
---

{content}
"""

    service = _get_service()
    media = MediaInMemoryUpload(
        markdown.encode("utf-8"), mimetype=mimetype, resumable=False
    )
    file_meta = {"name": filename, "parents": [folder_id]}
    file = service.files().create(
        body=file_meta,
        media_body=media,
        fields="id, name",
    ).execute()

    file_id = file.get("id")
    audit_log("create_file", file_id=file_id, filename=filename)

    # Lớp 4: Optional ownership transfer
    if ENABLE_OWNERSHIP_TRANSFER:
        try:
            _initiate_ownership_transfer(service, file_id, OWNER_EMAIL)
        except Exception as e:
            # Không fail nếu transfer lỗi — file vẫn lưu được
            print(f"[drive] Transfer warning (non-fatal): {e}")
            audit_log("transfer_failed", file_id=file_id, details=str(e)[:200])

    return file.get("name")


def _initiate_ownership_transfer(service, file_id: str, target_email: str):
    """
    Khởi tạo transfer ownership tới OWNER_EMAIL.

    Với consumer Gmail, dùng pattern pendingOwner — gửi email mời,
    OWNER phải accept thủ công lần đầu trong Google Drive.
    """
    # Lớp 4: Validate target email
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


def search_notes(keyword: str, max_results: int = 5) -> list:
    """
    Tìm kiếm ghi chú theo keyword.
    Lưu ý: với scope drive.file, chỉ thấy file do chính bot tạo.
    """
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


def get_recent_notes(days: int = 7, max_results: int = 5) -> list:
    """
    Lấy các ghi chú gần đây trong N ngày.
    Lưu ý: với scope drive.file, chỉ thấy file do chính bot tạo.
    """
    folder_id = _get_or_create_notes_folder()
    validate_folder(folder_id)

    service = _get_service()
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
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


def _read_file(service, file_id: str) -> str:
    """Đọc nội dung file từ Drive."""
    request = service.files().get_media(fileId=file_id)
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return buf.getvalue().decode("utf-8", errors="ignore")