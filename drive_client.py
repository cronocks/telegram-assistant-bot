import io
import json
import os
import base64
from datetime import datetime, timedelta
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaInMemoryUpload
from google.oauth2 import service_account
from config import GDRIVE_FOLDER_ID, CLAUDE_NOTES_FOLDER

SCOPES = ["https://www.googleapis.com/auth/drive"]


def _get_credentials():
    """Đọc credentials từ env GOOGLE_CREDENTIALS_B64 hoặc file local."""
    raw_b64 = os.environ.get("GOOGLE_CREDENTIALS_B64", "").strip()

    if raw_b64:
        print(f"[drive] Found GOOGLE_CREDENTIALS_B64, length={len(raw_b64)}")
        try:
            decoded = base64.b64decode(raw_b64).decode("utf-8")
            info = json.loads(decoded)
            print(f"[drive] Loaded credentials for: {info.get('client_email', 'unknown')}")
            return service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
        except Exception as e:
            raise RuntimeError(f"Khong decode duoc base64: {e}")

    # Fallback: file local (dev mode)
    if os.path.exists("credentials.json"):
        print("[drive] Reading from local credentials.json")
        return service_account.Credentials.from_service_account_file(
            "credentials.json", scopes=SCOPES
        )

    raise RuntimeError("Khong tim thay credentials nao ca!")


def _get_service():
    creds = _get_credentials()
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def test_drive_connection() -> dict:
    """Test kết nối Drive — trả về thông tin folder vault."""
    service = _get_service()
    folder = service.files().get(
        fileId=GDRIVE_FOLDER_ID,
        fields="id, name, mimeType"
    ).execute()
    return folder


def _get_or_create_notes_folder() -> str:
    """Trả về thẳng folder ID đã share, không tạo subfolder nữa."""
    return GDRIVE_FOLDER_ID


def save_note(title: str, content: str) -> str:
    service = _get_service()
    folder_id = GDRIVE_FOLDER_ID  # Lưu thẳng vào folder đã share
    now = datetime.now()
    safe_title = title.replace("/", "-").replace("\\", "-")[:40]
    filename = f"{now.strftime('%Y-%m-%d_%H%M')}_{safe_title}.md"
    markdown = f"""---
title: {title}
date: {now.strftime('%Y-%m-%d %H:%M')}
source: telegram-bot
---

{content}
"""
    media = MediaInMemoryUpload(
        markdown.encode("utf-8"), mimetype="text/markdown", resumable=False
    )
    file_meta = {"name": filename, "parents": [folder_id]}
    file = service.files().create(
        body=file_meta,
        media_body=media,
        fields="id, name",
        supportsAllDrives=True,
    ).execute()
    return file.get("name")


def search_notes(keyword: str, max_results: int = 5) -> list:
    service = _get_service()
    folder_id = _get_or_create_notes_folder()
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
    supportsAllDrives=True,
    includeItemsFromAllDrives=True,
).execute()
    notes = []
    for f in results.get("files", []):
        content = _read_file(service, f["id"])
        notes.append({
            "name": f["name"],
            "modified": f["modifiedTime"][:10],
            "content": content[:500],
        })
    return notes


def get_recent_notes(days: int = 7, max_results: int = 5) -> list:
    service = _get_service()
    folder_id = _get_or_create_notes_folder()
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
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute()
    notes = []
    for f in results.get("files", []):
        content = _read_file(service, f["id"])
        notes.append({
            "name": f["name"],
            "modified": f["modifiedTime"][:10],
            "content": content,
        })
    return notes


def _read_file(service, file_id: str) -> str:
    request = service.files().get_media(fileId=file_id)
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return buf.getvalue().decode("utf-8", errors="ignore")