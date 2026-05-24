"""cmd_utils.py — Shared helpers, pending-state machine, and ACL note utilities.

Imported by cmd_*.py modules and core_handler. Nothing in this module imports
from other cmd_* modules (no circular deps).
"""
import re
import time
import traceback

import acl as acl_mod
from config import FUZZY_SHOW_LIMIT, PENDING_CHOICE_TIMEOUT_SEC
from deps import CoreDeps
from interfaces import User
from timeutils import time_str


# ═══════════════════════════════════════════════════════════════════════════════
# Pending state
# ═══════════════════════════════════════════════════════════════════════════════
# State per chat_id, used to resolve follow-up replies (choice 1/2 or yes/no).
# Shape:
#   {
#     "type": "fuzzy_append" | "fuzzy_view" | "create_new_confirm",
#     "expires_at": float (unix ts),
#     "data": {...}  # depends on type
#   }

_pending: dict[str, dict] = {}


def _set_pending(chat_id: str, ptype: str, data: dict) -> None:
    _pending[chat_id] = {
        "type": ptype,
        "expires_at": time.time() + PENDING_CHOICE_TIMEOUT_SEC,
        "data": data,
    }


def _get_pending(chat_id: str) -> dict | None:
    p = _pending.get(chat_id)
    if not p:
        return None
    if time.time() > p["expires_at"]:
        _pending.pop(chat_id, None)
        return None
    return p


def _clear_pending(chat_id: str) -> None:
    _pending.pop(chat_id, None)


# ═══════════════════════════════════════════════════════════════════════════════
# Parsing helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _norm(text: str) -> str:
    """Lowercase + strip. Diacritics are preserved (Vietnamese)."""
    return text.strip().lower()


def _starts_with_any(text: str, prefixes: list[str]) -> str | None:
    """Return the matching prefix or None."""
    low = _norm(text)
    for p in prefixes:
        if low.startswith(p.lower()):
            return p
    return None


def _strip_prefix(text: str, prefix: str) -> str:
    """Strip prefix (case-insensitive) from the start; return the remainder."""
    if text.lower().startswith(prefix.lower()):
        return text[len(prefix):].strip()
    return text.strip()


def _parse_choice_number(text: str) -> int | None:
    """Parse a single number ('1', '2', '10') from a short reply; else None."""
    cleaned = text.strip().rstrip(".").rstrip(")")
    if cleaned.isdigit():
        n = int(cleaned)
        if 1 <= n <= 99:
            return n
    return None


def _parse_yes_no(text: str) -> bool | None:
    """Parse Vietnamese yes/no markers; returns True/False/None."""
    low = _norm(text)
    yes_words = {
        "yes", "y", "co", "có", "ok",
        "đồng ý", "dong y",
        "tao moi", "tạo mới", "tạo", "tao",
    }
    no_words = {"no", "n", "khong", "không", "huy", "hủy", "thoi", "thôi"}
    if low in yes_words:
        return True
    if low in no_words:
        return False
    return None


def _sanitize_filename(name: str) -> str:
    """Strip filesystem-unsafe characters; truncate to 80 chars."""
    name = name.strip()
    # Drop control / FS-illegal characters; keep Vietnamese letters, digits, spaces, dashes.
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '', name)
    name = name.strip()
    return name[:80] if name else "untitled"


# ═══════════════════════════════════════════════════════════════════════════════
# Pending state resolvers
# ═══════════════════════════════════════════════════════════════════════════════

async def _resolve_fuzzy_append(
    chat_id: str, pending: dict, text: str, deps: CoreDeps,
) -> bool:
    """Handle a reply to a pending fuzzy_append prompt.

    Returns True if the message was consumed; False to let it fall through to
    normal command handling.
    """
    data = pending["data"]
    matches = data["matches"]
    content = data["content"]

    n = _parse_choice_number(text)
    if n is not None:
        if 1 <= n <= len(matches):
            chosen = matches[n - 1]
            _clear_pending(chat_id)
            await deps.channel.send(
                chat_id, f"Dang them vao file: {chosen['name']}...", use_markdown=False,
            )
            try:
                entry = f"\n## {time_str()}\n{content}\n"
                filename = deps.notes.append_to_file(chosen["id"], entry)
                await deps.channel.send(
                    chat_id, f"Da them vao: {filename}", use_markdown=False,
                )
            except Exception as e:
                traceback.print_exc()
                await deps.channel.send(
                    chat_id, f"Loi khi them: {str(e)[:400]}", use_markdown=False,
                )
            return True
        await deps.channel.send(
            chat_id, f"So khong hop le. Hay chon tu 1 den {len(matches)}.",
            use_markdown=False,
        )
        return True

    yn = _parse_yes_no(text)
    if yn is False:
        _clear_pending(chat_id)
        await deps.channel.send(chat_id, "Da huy.", use_markdown=False)
        return True

    return False


async def _resolve_fuzzy_view(
    chat_id: str, pending: dict, text: str, deps: CoreDeps,
) -> bool:
    """Handle a reply to a pending fuzzy_view prompt."""
    matches = pending["data"]["matches"]

    n = _parse_choice_number(text)
    if n is not None:
        if 1 <= n <= len(matches):
            chosen = matches[n - 1]
            _clear_pending(chat_id)
            try:
                file_data = deps.notes.read_file_by_id(chosen["id"])
                content = file_data["content"]
                # Telegram limit is ~4096 chars; cut conservatively.
                if len(content) > 3500:
                    content = content[:3500] + "\n\n[...] (file qua dai, da cat)"
                await deps.channel.send(
                    chat_id,
                    f"=== {file_data['name']} ===\n\n{content}",
                    use_markdown=False,
                )
            except Exception as e:
                traceback.print_exc()
                await deps.channel.send(
                    chat_id, f"Loi khi doc: {str(e)[:400]}", use_markdown=False,
                )
            return True
        await deps.channel.send(
            chat_id, f"So khong hop le. Hay chon tu 1 den {len(matches)}.",
            use_markdown=False,
        )
        return True

    yn = _parse_yes_no(text)
    if yn is False:
        _clear_pending(chat_id)
        await deps.channel.send(chat_id, "Da huy.", use_markdown=False)
        return True

    return False


async def _resolve_create_new_confirm(
    chat_id: str, pending: dict, text: str, user: User, deps: CoreDeps,
) -> bool:
    """Handle yes/no confirmation for creating a new file after fuzzy miss."""
    data = pending["data"]
    yn = _parse_yes_no(text)

    if yn is True:
        _clear_pending(chat_id)
        filename = data["filename"]
        content = data["content"]
        await deps.channel.send(
            chat_id, f"Dang tao file: {filename}...", use_markdown=False,
        )
        try:
            saved_name, file_id = deps.notes.save_note(
                title=filename,
                content=content,
                custom_filename=_sanitize_filename(filename),
            )
            _register_note(file_id, user.id, "note", saved_name, deps)
            await deps.channel.send(
                chat_id, f"Da tao: {saved_name}", use_markdown=False,
            )
        except Exception as e:
            traceback.print_exc()
            await deps.channel.send(
                chat_id, f"Loi khi tao: {str(e)[:400]}", use_markdown=False,
            )
        return True

    if yn is False:
        _clear_pending(chat_id)
        await deps.channel.send(chat_id, "Da huy.", use_markdown=False)
        return True

    return False


async def _try_resolve_pending(
    chat_id: str, text: str, user: User, deps: CoreDeps,
) -> bool:
    """If a pending state exists, attempt to resolve it. Returns True if handled."""
    pending = _get_pending(chat_id)
    if not pending:
        return False

    ptype = pending["type"]
    if ptype == "fuzzy_append":
        return await _resolve_fuzzy_append(chat_id, pending, text, deps)
    if ptype == "fuzzy_view":
        return await _resolve_fuzzy_view(chat_id, pending, text, deps)
    if ptype == "create_new_confirm":
        return await _resolve_create_new_confirm(chat_id, pending, text, user, deps)

    return False


# ═══════════════════════════════════════════════════════════════════════════════
# Note registration + ACL helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _register_note(
    file_id: str, owner_id: int, kind: str, title: str, deps: CoreDeps
) -> None:
    """Insert a note row into the SQLite index (best-effort; logs on failure)."""
    try:
        deps.note_index.add_note(file_id, owner_id, kind=kind, title=title, scope="private")
    except Exception:
        traceback.print_exc()


def _register_wiki_page(
    file_id: str, owner_id: int, topic: str, slug: str, deps: CoreDeps
) -> None:
    """Insert a wiki_page row into the SQLite index (best-effort; logs on failure)."""
    try:
        deps.note_index.add_wiki_page(file_id, owner_id, topic=topic, slug=slug, scope="everyone")
    except Exception:
        traceback.print_exc()


def _acl_filter_notes(notes: list[dict], viewer: User, deps: CoreDeps) -> list[dict]:
    """Filter a list of note search results to those the viewer may read.

    Each note dict must contain an 'id' field (drive_file_id). Notes with no
    SQLite row (orphans) are treated as invisible — safe default. Stealth-read
    rows (FR-4) emit one audit row per revealed resource.
    """
    if not notes:
        return []
    file_ids = [n["id"] for n in notes if n.get("id")]
    meta_rows = deps.note_index.note_meta_for_ids(file_ids)
    visible = acl_mod.filter_visible(viewer, meta_rows, user_store=deps.user_store)
    for row in visible:
        if row.get("is_stealth_read"):
            deps.audit.log(
                actor_user_id=viewer.id,
                action="stealth_read_note",
                target_type="note",
                target_id=row.get("drive_file_id"),
                payload={"owner_user_id": row.get("owner_user_id")},
            )
    visible_ids = {r["drive_file_id"] for r in visible}
    return [n for n in notes if n.get("id") in visible_ids]


def _visible_notes_with_meta(
    files: list[dict], user: User, deps: CoreDeps
) -> tuple[list[dict], dict]:
    """ACL-filter Drive files against the note index.

    Returns (visible files, {drive_file_id: meta}). Orphans (files with no
    index row) are dropped — safe default per FR-3. FR-4 stealth-read rows
    emit one audit row each.
    """
    if not files:
        return [], {}
    metas = {
        m["drive_file_id"]: m
        for m in deps.note_index.note_meta_for_ids([f["id"] for f in files])
    }
    visible: list[dict] = []
    for f in files:
        m = metas.get(f["id"])
        if m is None:
            continue
        allowed, is_stealth = acl_mod.can_read(
            user, m["scope"], m["owner_user_id"], user_store=deps.user_store,
        )
        if not allowed:
            continue
        if is_stealth:
            deps.audit.log(
                actor_user_id=user.id,
                action="stealth_read_note",
                target_type="note",
                target_id=m["drive_file_id"],
                payload={"owner_user_id": m["owner_user_id"]},
            )
        visible.append(f)
    return visible, metas


# ═══════════════════════════════════════════════════════════════════════════════
# Elevation helpers (shared by cmd_sudo and cmd_notes)
# ═══════════════════════════════════════════════════════════════════════════════

def _elevation_remaining_minutes(expires_at_iso: str) -> int:
    """Return whole minutes left until expires_at (ISO UTC). 0 if past or unparseable."""
    from datetime import datetime, timezone
    try:
        # Stored format: "%Y-%m-%dT%H:%M:%SZ"
        expires = datetime.strptime(expires_at_iso, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return 0
    delta = expires - datetime.now(timezone.utc)
    return max(0, int(delta.total_seconds() // 60))
