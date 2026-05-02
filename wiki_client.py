"""
wiki_client.py — LLM Wiki layer: Drive operations cho wiki pages.

Cấu trúc thư mục:
  Claude-Notes/
  └── Wiki/           ← WIKI_SUBFOLDER (auto-created)
      └── <slug>.md   ← mỗi file = 1 topic

Mỗi wiki page có frontmatter:
  ---
  wiki: true
  topic: "Tên topic"
  type: person|project|concept|event|place|other
  updated: YYYY-MM-DD
  ---
"""
import re
from googleapiclient.http import MediaInMemoryUpload

from drive_client import _get_service, _get_or_create_notes_folder, _read_file, MIME_MARKDOWN
from security import (
    validate_folder, check_rate_limit, validate_file_creation,
    audit_log, register_trusted_folder,
)
from config import WIKI_SUBFOLDER
from timeutils import today_str, time_str

_cached_wiki_folder_id: str = ""


# ═══════════════════════════════════════════════════════════════════════════════
# FOLDER
# ═══════════════════════════════════════════════════════════════════════════════

def get_wiki_folder_id() -> str:
    """Get hoặc create Wiki subfolder bên trong Claude-Notes. Cache kết quả."""
    global _cached_wiki_folder_id
    if _cached_wiki_folder_id:
        return _cached_wiki_folder_id

    parent_id = _get_or_create_notes_folder()
    service = _get_service()

    safe_name = WIKI_SUBFOLDER.replace("'", "\\'")
    query = (
        f"name='{safe_name}' "
        f"and '{parent_id}' in parents "
        f"and mimeType='application/vnd.google-apps.folder' "
        f"and trashed=false"
    )
    results = service.files().list(q=query, fields="files(id, name)").execute()
    files = results.get("files", [])

    if files:
        _cached_wiki_folder_id = files[0]["id"]
        print(f"[wiki] Tim thay subfolder: {_cached_wiki_folder_id}")
    else:
        meta = {
            "name": WIKI_SUBFOLDER,
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [parent_id],
        }
        folder = service.files().create(body=meta, fields="id").execute()
        _cached_wiki_folder_id = folder["id"]
        print(f"[wiki] Da tao subfolder: {_cached_wiki_folder_id}")
        audit_log("wiki_folder_created", file_id=_cached_wiki_folder_id, filename=WIKI_SUBFOLDER)

    register_trusted_folder(_cached_wiki_folder_id)
    return _cached_wiki_folder_id


# ═══════════════════════════════════════════════════════════════════════════════
# LIST / FIND
# ═══════════════════════════════════════════════════════════════════════════════

def list_wiki_pages() -> list[dict]:
    """Liệt kê tất cả wiki pages. Trả về [{id, name, modifiedTime}]."""
    wiki_folder_id = get_wiki_folder_id()
    validate_folder(wiki_folder_id)

    service = _get_service()
    results = service.files().list(
        q=f"'{wiki_folder_id}' in parents and trashed=false and mimeType='{MIME_MARKDOWN}'",
        fields="files(id, name, modifiedTime)",
        orderBy="modifiedTime desc",
        pageSize=100,
    ).execute()
    pages = results.get("files", [])
    audit_log("wiki_list", details=f"count={len(pages)}")
    return pages


def get_wiki_topic_names() -> list[str]:
    """Trả về danh sách tên topic (slug đã bỏ .md) để đưa vào Claude prompt."""
    pages = list_wiki_pages()
    return [p["name"].replace(".md", "").replace("_", " ") for p in pages]


def find_wiki_page(topic: str) -> dict | None:
    """
    Tìm wiki page theo topic (slug hoặc partial match).
    Trả về {id, name, content} hoặc None.
    """
    pages = list_wiki_pages()
    if not pages:
        return None

    slug = _topic_to_slug(topic)
    topic_lower = topic.lower().strip()

    matched = None
    # 1. Exact slug match
    for p in pages:
        if p["name"].lower() == f"{slug}.md":
            matched = p
            break

    # 2. Partial match trên slug (bỏ .md, thay _ thành space)
    if not matched:
        for p in pages:
            page_topic = p["name"].lower().replace(".md", "").replace("_", " ")
            if topic_lower in page_topic or page_topic in topic_lower:
                matched = p
                break

    if not matched:
        return None

    service = _get_service()
    content = _read_file(service, matched["id"])
    audit_log("wiki_read", file_id=matched["id"], filename=matched["name"])
    return {"id": matched["id"], "name": matched["name"], "content": content}


def find_wiki_pages_by_keywords(keywords: list[str], max_pages: int = 2) -> list[dict]:
    """
    Tìm các wiki pages liên quan theo danh sách keywords (match tên topic).
    Dùng cho QA context — trả về [{id, name, content}].
    Giới hạn max_pages để kiểm soát token.
    """
    if not keywords:
        return []

    pages = list_wiki_pages()
    if not pages:
        return []

    scored: list[tuple[int, dict]] = []
    for p in pages:
        page_topic = p["name"].lower().replace(".md", "").replace("_", " ")
        score = sum(1 for kw in keywords if kw.lower() in page_topic)
        if score > 0:
            scored.append((score, p))

    scored.sort(key=lambda x: x[0], reverse=True)
    top = [p for _, p in scored[:max_pages]]

    if not top:
        return []

    service = _get_service()
    results = []
    for p in top:
        try:
            content = _read_file(service, p["id"])
            results.append({"id": p["id"], "name": p["name"], "content": content})
        except Exception as e:
            print(f"[wiki] Skip page {p['id']}: {e}")

    audit_log("wiki_keyword_search", details=f"keywords={keywords}, found={len(results)}")
    return results


# ═══════════════════════════════════════════════════════════════════════════════
# SAVE / APPEND
# ═══════════════════════════════════════════════════════════════════════════════

def save_wiki_page(topic: str, content: str, file_id: str = None) -> str:
    """
    Tạo mới hoặc ghi đè wiki page.
    file_id: nếu có → update; không có → create mới.
    Trả về filename.
    """
    wiki_folder_id = get_wiki_folder_id()
    validate_folder(wiki_folder_id)

    service = _get_service()
    filename = f"{_topic_to_slug(topic)}.md"
    media = MediaInMemoryUpload(content.encode("utf-8"), mimetype=MIME_MARKDOWN, resumable=False)

    if file_id:
        service.files().update(fileId=file_id, media_body=media).execute()
        audit_log("wiki_page_updated", file_id=file_id, filename=filename)
    else:
        check_rate_limit()
        validate_file_creation(filename, MIME_MARKDOWN)
        file_meta = {"name": filename, "parents": [wiki_folder_id]}
        file = service.files().create(
            body=file_meta, media_body=media, fields="id, name",
        ).execute()
        filename = file.get("name")
        audit_log("wiki_page_created", file_id=file.get("id"), filename=filename)

    return filename


def append_to_wiki_page(file_id: str, new_section: str) -> str:
    """
    Append 1 section có timestamp vào wiki page hiện có.
    Trả về filename.
    """
    wiki_folder_id = get_wiki_folder_id()
    validate_folder(wiki_folder_id)

    service = _get_service()
    meta = service.files().get(
        fileId=file_id, fields="id, name, parents, mimeType",
    ).execute()

    if wiki_folder_id not in (meta.get("parents") or []):
        raise PermissionError(f"[SECURITY] Wiki file khong thuoc wiki folder: {file_id}")
    if meta.get("mimeType") != MIME_MARKDOWN:
        raise PermissionError(f"[SECURITY] File khong phai markdown: {meta.get('mimeType')}")

    current = _read_file(service, file_id)
    if not current.endswith("\n"):
        current += "\n"
    updated = current + new_section

    media = MediaInMemoryUpload(updated.encode("utf-8"), mimetype=MIME_MARKDOWN, resumable=False)
    service.files().update(fileId=file_id, media_body=media).execute()
    audit_log("wiki_page_append", file_id=file_id, filename=meta.get("name"))
    return meta.get("name")


# ═══════════════════════════════════════════════════════════════════════════════
# BUILDERS
# ═══════════════════════════════════════════════════════════════════════════════

def build_new_wiki_page(topic: str, topic_type: str, content_to_add: str) -> str:
    """Tạo nội dung markdown đầy đủ cho wiki page mới."""
    return (
        f"---\n"
        f"wiki: true\n"
        f"topic: \"{topic}\"\n"
        f"type: {topic_type}\n"
        f"updated: {today_str()}\n"
        f"---\n\n"
        f"## Tóm tắt\n"
        f"{content_to_add}\n"
    )


def build_wiki_section(content_to_add: str) -> str:
    """Tạo 1 section có timestamp để append vào wiki page hiện có."""
    return f"\n## Cập nhật {time_str()} — {today_str()}\n{content_to_add}\n"


# ═══════════════════════════════════════════════════════════════════════════════
# INTERNAL
# ═══════════════════════════════════════════════════════════════════════════════

def _topic_to_slug(topic: str) -> str:
    """Chuyển tên topic thành slug an toàn cho tên file."""
    slug = topic.lower().strip()
    slug = re.sub(r'[<>:"/\\|?*\s\x00-\x1f]+', '_', slug)
    slug = slug.strip('_')
    return slug[:60] if slug else "untitled"
