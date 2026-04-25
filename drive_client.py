import io
import json
from datetime import datetime
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaInMemoryUpload
from google.oauth2 import service_account
from config import CREDENTIALS_FILE, GDRIVE_FOLDER_ID, CLAUDE_NOTES_FOLDER

SCOPES = ["https://www.googleapis.com/auth/drive"]


def _get_service():
    import json as _json
    import os
    import base64
    
    raw_b64 = os.environ.get("GOOGLE_CREDENTIALS_B64", "")
    if not raw_b64:
        raise RuntimeError("GOOGLE_CREDENTIALS_B64 env variable is empty!")
    
    decoded = base64.b64decode(raw_b64).decode("utf-8")
    info = _json.loads(decoded)
    
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=SCOPES
    )
    return build("drive", "v3", credentials=creds)


def _get_or_create_notes_folder() -> str:
    """Tìm hoặc tạo thư mục Claude-Notes bên trong vault."""
    service = _get_service()
    query = (
        f"name='{CLAUDE_NOTES_FOLDER}' "
        f"and '{GDRIVE_FOLDER_ID}' in parents "
        f"and mimeType='application/vnd.google-apps.folder' "
        f"and trashed=false"
    )
    results = service.files().list(q=query, fields="files(id, name)").execute()
    files = results.get("files", [])

    if files:
        return files[0]["id"]

    # Tạo mới nếu chưa có
    folder_meta = {
        "name": CLAUDE_NOTES_FOLDER,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [GDRIVE_FOLDER_ID],
    }
    folder = service.files().create(body=folder_meta, fields="id").execute()
    return folder["id"]


def save_note(title: str, content: str) -> str:
    """Lưu ghi chú mới vào Claude-Notes dưới dạng file .md."""
    service = _get_service()
    folder_id = _get_or_create_notes_folder()

    now = datetime.now()
    filename = f"{now.strftime('%Y-%m-%d')}_{title[:40].replace(' ', '-')}.md"

    # Nội dung file Markdown với metadata
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
        body=file_meta, media_body=media, fields="id, name"
    ).execute()
    return file.get("name")


def search_notes(keyword: str, max_results: int = 5) -> list[dict]:
    """Tìm kiếm ghi chú theo từ khóa."""
    service = _get_service()
    folder_id = _get_or_create_notes_folder()

    query = (
        f"fullText contains '{keyword}' "
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
        content = _read_file(service, f["id"])
        notes.append({
            "name": f["name"],
            "modified": f["modifiedTime"][:10],
            "content": content[:500],  # Chỉ lấy 500 ký tự đầu
        })
    return notes


def get_recent_notes(days: int = 7, max_results: int = 5) -> list[dict]:
    """Lấy các ghi chú gần đây."""
    service = _get_service()
    folder_id = _get_or_create_notes_folder()

    from datetime import timedelta
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
        content = _read_file(service, f["id"])
        notes.append({
            "name": f["name"],
            "modified": f["modifiedTime"][:10],
            "content": content,
        })
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
