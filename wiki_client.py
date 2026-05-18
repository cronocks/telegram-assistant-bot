"""wiki_client.py — Google Drive backed implementation of WikiStore.

Folder layout:
  Claude-Notes/
  └── Wiki/                ← WIKI_SUBFOLDER (auto-created)
      ├── _index.md        ← topic index, one TLDR row per page
      └── <slug>.md        ← one file per topic

Each wiki page carries a frontmatter block:
  ---
  wiki: true
  topic: "Topic name"
  type: person|project|concept|event|place|other
  updated: YYYY-MM-DD
  ---

Retrieval shape:
  retrieve_pages(question, keywords) is the single entry point called by the
  core handler. When migrating to a vector DB, swap this method's body —
  the caller signature stays identical.
"""
import re

from googleapiclient.http import MediaInMemoryUpload

from config import (
    MAX_WIKI_CONTEXT_CHARS,
    MAX_WIKI_PAGES_CONTEXT,
    WIKI_SUBFOLDER,
)
from cost_monitor import record_usage
from drive_client import (
    MIME_MARKDOWN,
    _get_or_create_notes_folder,
    _get_service,
    _read_file,
)
from interfaces import LLMClient
from security import (
    audit_log,
    check_rate_limit,
    register_trusted_folder,
    validate_file_creation,
    validate_folder,
)
from timeutils import time_str, today_str

_cached_wiki_folder_id: str = ""

INDEX_FILENAME = "_index.md"
INDEX_HEADER = (
    "# Wiki Index\n\n"
    "| Topic | File | Type | TLDR |\n"
    "|-------|------|------|------|\n"
)


# ═══════════════════════════════════════════════════════════════════════════════
# Folder management (module-level — cached once per process)
# ═══════════════════════════════════════════════════════════════════════════════

def _get_wiki_folder_id() -> str:
    """Resolve (and lazily create) the Wiki subfolder inside Claude-Notes."""
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
        print(f"[wiki] Found existing subfolder: {_cached_wiki_folder_id}")
    else:
        meta = {
            "name": WIKI_SUBFOLDER,
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [parent_id],
        }
        folder = service.files().create(body=meta, fields="id").execute()
        _cached_wiki_folder_id = folder["id"]
        print(f"[wiki] Created subfolder: {_cached_wiki_folder_id}")
        audit_log(
            "wiki_folder_created",
            file_id=_cached_wiki_folder_id,
            filename=WIKI_SUBFOLDER,
        )

    register_trusted_folder(_cached_wiki_folder_id)
    return _cached_wiki_folder_id


# ═══════════════════════════════════════════════════════════════════════════════
# Utility: topic -> filesystem-safe slug
# ═══════════════════════════════════════════════════════════════════════════════

def topic_to_slug(topic: str) -> str:
    """Convert a topic name into a filesystem-safe slug for a wiki filename."""
    slug = topic.lower().strip()
    slug = re.sub(r'[<>:"/\\|?*\s\x00-\x1f]+', '_', slug)
    slug = slug.strip('_')
    return slug[:60] if slug else "untitled"


# ═══════════════════════════════════════════════════════════════════════════════
# DriveWikiStore — WikiStore impl
# ═══════════════════════════════════════════════════════════════════════════════

class DriveWikiStore:
    """WikiStore impl backed by Google Drive.

    Holds an LLMClient reference because `retrieve_pages` delegates page
    selection to the model. When swapping to a vector DB the LLM dep can be
    removed.
    """

    def __init__(self, llm: LLMClient):
        self._llm = llm

    # ─── Index management ───────────────────────────────────────────────────

    def _get_or_create_index(self) -> dict:
        """Return {id, content} for _index.md, creating it if needed."""
        wiki_folder_id = _get_wiki_folder_id()
        validate_folder(wiki_folder_id)

        service = _get_service()
        safe_name = INDEX_FILENAME.replace("'", "\\'")
        query = (
            f"name='{safe_name}' "
            f"and '{wiki_folder_id}' in parents "
            f"and trashed=false"
        )
        results = service.files().list(q=query, fields="files(id, name)").execute()
        files = results.get("files", [])

        if files:
            content = _read_file(service, files[0]["id"])
            return {"id": files[0]["id"], "content": content}

        # Create a fresh index file.
        media = MediaInMemoryUpload(
            INDEX_HEADER.encode("utf-8"), mimetype=MIME_MARKDOWN, resumable=False,
        )
        file_meta = {"name": INDEX_FILENAME, "parents": [wiki_folder_id]}
        file = service.files().create(
            body=file_meta, media_body=media, fields="id",
        ).execute()
        audit_log(
            "wiki_index_created",
            file_id=file.get("id"),
            filename=INDEX_FILENAME,
        )
        return {"id": file.get("id"), "content": INDEX_HEADER}

    def _read_wiki_index(self) -> dict:
        """Return {id, content} for the wiki index (creating it if missing)."""
        return self._get_or_create_index()

    def add_to_index(
        self, topic: str, slug: str, topic_type: str, tldr: str
    ) -> None:
        """Append a row to the index for a newly created wiki page.

        No-op if the slug already appears in the index (avoids duplicates).
        """
        index = self._get_or_create_index()

        if f"{slug}.md" in index["content"]:
            return  # already present, skip duplicate

        tldr_clean = tldr.replace("|", "—").replace("\n", " ").strip()[:120]
        new_row = f"| {topic} | {slug}.md | {topic_type} | {tldr_clean} |\n"

        updated = index["content"]
        if not updated.endswith("\n"):
            updated += "\n"
        updated += new_row

        service = _get_service()
        media = MediaInMemoryUpload(
            updated.encode("utf-8"), mimetype=MIME_MARKDOWN, resumable=False,
        )
        service.files().update(fileId=index["id"], media_body=media).execute()
        audit_log(
            "wiki_index_updated",
            file_id=index["id"],
            details=f"added={slug}",
        )

    # ─── Retrieval (single entry point) ─────────────────────────────────────

    def retrieve_pages(
        self, question: str, keywords: list[str]
    ) -> list[dict]:
        """Single retrieval entry point.

        Current implementation (text index):
          1. Read _index.md (1 Drive call)
          2. LLM picks relevant pages from the index (1 LLM call, ~350 tokens)
          3. Read the selected pages from Drive (1-2 Drive calls)

        Future (vector DB):
          1. embed(question) -> vector_search() -> top-k pages
          (caller signature unchanged)
        """
        # Stage 1: read the index.
        try:
            index = self._read_wiki_index()
            index_content = index["content"]
        except Exception as e:
            print(f"[wiki] retrieve: cannot read index: {e}")
            return []

        if "| Topic |" not in index_content:
            return []  # index has no rows yet

        # Stage 2: ask the LLM to pick filenames.
        try:
            selected_files, sel_tokens = self._llm.select_wiki_pages_from_index(
                question, index_content,
            )
            record_usage(sel_tokens // 2, sel_tokens // 2)
        except Exception as e:
            print(f"[wiki] retrieve: selection error: {e}")
            return []

        if not selected_files:
            return []

        # Stage 3: load the selected pages.
        pages = self.list_pages()
        pages_by_name = {p["name"]: p for p in pages}
        service = _get_service()

        results: list[dict] = []
        for filename in selected_files[:MAX_WIKI_PAGES_CONTEXT]:
            page_meta = pages_by_name.get(filename)
            if not page_meta:
                continue
            try:
                content = _read_file(service, page_meta["id"])
                results.append({
                    "id": page_meta["id"],
                    "name": page_meta["name"],
                    "content": content[:MAX_WIKI_CONTEXT_CHARS],
                })
            except Exception as e:
                print(f"[wiki] retrieve: cannot read {filename}: {e}")

        audit_log(
            "wiki_retrieve",
            details=f"selected={selected_files}, returned={len(results)}",
        )
        return results

    # ─── List / find ────────────────────────────────────────────────────────

    def list_pages(self) -> list[dict]:
        """List all wiki pages (excluding the index)."""
        wiki_folder_id = _get_wiki_folder_id()
        validate_folder(wiki_folder_id)

        service = _get_service()
        results = service.files().list(
            q=f"'{wiki_folder_id}' in parents and trashed=false and mimeType='{MIME_MARKDOWN}'",
            fields="files(id, name, modifiedTime)",
            orderBy="modifiedTime desc",
            pageSize=100,
        ).execute()
        pages = [p for p in results.get("files", []) if p["name"] != INDEX_FILENAME]
        audit_log("wiki_list", details=f"count={len(pages)}")
        return pages

    def get_topic_names(self) -> list[str]:
        """Topic names (slug-decoded) suitable for prompting the LLM."""
        pages = self.list_pages()
        return [p["name"].replace(".md", "").replace("_", " ") for p in pages]

    def find_page(self, topic: str) -> dict | None:
        """Find a wiki page by topic (exact slug or partial match)."""
        pages = self.list_pages()
        if not pages:
            return None

        slug = topic_to_slug(topic)
        topic_lower = topic.lower().strip()

        matched: dict | None = None
        for p in pages:
            if p["name"].lower() == f"{slug}.md":
                matched = p
                break

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

    # ─── Save / append ──────────────────────────────────────────────────────

    def save_page(
        self, topic: str, content: str, file_id: str | None = None
    ) -> tuple[str, str]:
        """Create or overwrite a wiki page. Returns (filename, drive_file_id)."""
        wiki_folder_id = _get_wiki_folder_id()
        validate_folder(wiki_folder_id)

        service = _get_service()
        filename = f"{topic_to_slug(topic)}.md"
        media = MediaInMemoryUpload(
            content.encode("utf-8"), mimetype=MIME_MARKDOWN, resumable=False,
        )

        if file_id:
            service.files().update(fileId=file_id, media_body=media).execute()
            audit_log("wiki_page_updated", file_id=file_id, filename=filename)
            return filename, file_id
        else:
            check_rate_limit()
            validate_file_creation(filename, MIME_MARKDOWN)
            file_meta = {"name": filename, "parents": [wiki_folder_id]}
            file = service.files().create(
                body=file_meta, media_body=media, fields="id, name",
            ).execute()
            new_file_id = file.get("id")
            filename = file.get("name")
            audit_log("wiki_page_created", file_id=new_file_id, filename=filename)
            return filename, new_file_id

    def append_to_page(self, file_id: str, new_section: str) -> str:
        """Append a timestamped section to an existing wiki page."""
        wiki_folder_id = _get_wiki_folder_id()
        validate_folder(wiki_folder_id)

        service = _get_service()
        meta = service.files().get(
            fileId=file_id, fields="id, name, parents, mimeType",
        ).execute()

        if wiki_folder_id not in (meta.get("parents") or []):
            raise PermissionError(
                f"[SECURITY] Wiki file does not belong to wiki folder: {file_id}"
            )
        if meta.get("mimeType") != MIME_MARKDOWN:
            raise PermissionError(
                f"[SECURITY] File is not markdown: {meta.get('mimeType')}"
            )

        current = _read_file(service, file_id)
        if not current.endswith("\n"):
            current += "\n"
        updated = current + new_section

        media = MediaInMemoryUpload(
            updated.encode("utf-8"), mimetype=MIME_MARKDOWN, resumable=False,
        )
        service.files().update(fileId=file_id, media_body=media).execute()
        audit_log("wiki_page_append", file_id=file_id, filename=meta.get("name"))
        return meta.get("name")

    # ─── Builders (pure markdown helpers) ───────────────────────────────────

    def build_new_page(
        self, topic: str, topic_type: str, content_to_add: str
    ) -> str:
        """Build the full markdown body (with frontmatter) for a new wiki page."""
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

    def build_section(self, content_to_add: str) -> str:
        """Build a timestamped section snippet for appending to an existing page."""
        return f"\n## Cập nhật {time_str()} — {today_str()}\n{content_to_add}\n"
