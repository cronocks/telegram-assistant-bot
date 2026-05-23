"""core_handler.py — Channel-agnostic message dispatcher.

The single public entry point is `handle_message(msg, deps)`. It assumes a
caller (a channel adapter) has already authorized the inbound message and
normalized it to a ChannelMessage. All side effects (LLM calls, storage
access, replies) go through `deps`, which holds the active adapters.

User-facing strings remain Vietnamese; everything else is English.
"""
import re
import time
import traceback

import config
from config import (
    FUZZY_SHOW_LIMIT,
    MAX_WIKI_UPDATES,
    PENDING_CHOICE_TIMEOUT_SEC,
)
from cost_monitor import check_and_alert, get_current_cost, record_usage
import acl as acl_mod
from deps import CoreDeps
from interfaces import AuditLog, ChannelAdapter, ChannelMessage, ElevationStore, LLMClient, MemoryStore, NoteIndex, NoteStore, NotificationService, User, UserStore, WikiStore
from permissions import can_manage, has_role
from text_utils import match_command, normalize_vn, validate_username
from security import get_security_status
from timeutils import current_week_range_str, time_str, today_str


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
# Command handlers
# ═══════════════════════════════════════════════════════════════════════════════

_VALID_ROLES = {"admin", "manager", "member", "readonly"}


async def _cmd_them_user(
    chat_id: str, body: str, user: User, deps: CoreDeps,
) -> None:
    """them user: <name>, <role> — admin creates a new user and returns an invite code."""
    if not has_role(user, "admin"):
        await deps.channel.send(chat_id, "Chỉ admin mới có thể thêm user.", use_markdown=False)
        return

    parts = body.split(",", 1)
    if len(parts) != 2:
        await deps.channel.send(
            chat_id,
            "Cú pháp: them user: <tên>, <role>\n"
            "Role hợp lệ: admin, manager, member, readonly",
            use_markdown=False,
        )
        return

    name = parts[0].strip()
    role = parts[1].strip().lower()

    if not name:
        await deps.channel.send(chat_id, "Thiếu tên user.", use_markdown=False)
        return
    if role not in _VALID_ROLES:
        await deps.channel.send(
            chat_id,
            f"Role không hợp lệ: '{role}'. Chọn: admin, manager, member, readonly",
            use_markdown=False,
        )
        return

    try:
        new_user = deps.user_store.create_user(name=name, role=role)
        code = deps.user_store.create_invite_code(
            intended_user_id=new_user.id, created_by=user.id
        )
        await deps.channel.send(
            chat_id,
            f"Da tao user {name} (role: {role}, id: {new_user.id}).\n\n"
            f"Ma moi (het han sau 7 ngay): {code}\n\n"
            f"Gui ma nay cho {name}, ho dung lenh:\n"
            f"dang ky: {code}",
            use_markdown=False,
        )
    except Exception as e:
        await deps.channel.send(
            chat_id, f"Lỗi khi tạo user: {str(e)[:400]}", use_markdown=False,
        )


async def _cmd_xem_danh_sach_user(
    chat_id: str, user: User, deps: CoreDeps,
) -> None:
    """xem danh sach user — admin/manager lists all active users."""
    if not can_manage(user):
        await deps.channel.send(
            chat_id, "Chỉ admin hoặc manager mới có thể xem danh sách user.",
            use_markdown=False,
        )
        return
    try:
        users = deps.user_store.list_users()
        if not users:
            await deps.channel.send(chat_id, "Chưa có user nào.", use_markdown=False)
            return
        lines = [f"Danh sách user ({len(users)} người):"]
        for u in users:
            lines.append(f"• [{u.id}] {u.name} — {u.role}")
        await deps.channel.send(chat_id, "\n".join(lines), use_markdown=False)
    except Exception as e:
        await deps.channel.send(chat_id, f"Lỗi: {str(e)[:400]}", use_markdown=False)


async def _resolve_user_or_reply(
    token: str, chat_id: str, deps: CoreDeps,
) -> "User | None":
    """Resolve a name-or-id token to a User, or send an error and return None.

    If token is numeric, look up by ID.
    If token is a name/username string, search all active users case-insensitively.
    Returns None (after sending an error message) when not found or ambiguous.
    """
    token = token.strip()
    if token.isdigit():
        u = deps.user_store.get_user_by_id(int(token))
        if u is None or not u.is_active:
            await deps.channel.send(chat_id, f"Không tìm thấy user #{token}.", use_markdown=False)
            return None
        return u

    needle = token.lower()
    matches = [
        u for u in deps.user_store.list_users()
        if u.is_active and (
            u.name.lower() == needle
            or (u.username is not None and u.username.lower() == needle)
        )
    ]
    if not matches:
        await deps.channel.send(chat_id, f"Không tìm thấy user '{token}'.", use_markdown=False)
        return None
    if len(matches) > 1:
        ids = ", ".join(f"#{u.id} ({u.name})" for u in matches)
        await deps.channel.send(
            chat_id,
            f"Nhiều user trùng tên '{token}': {ids}.\nDùng ID để chỉ định chính xác.",
            use_markdown=False,
        )
        return None
    return matches[0]


async def _cmd_xoa_user(
    chat_id: str, body: str, user: User, deps: CoreDeps,
) -> None:
    """xoa user: <tên/id> — admin soft-deletes a user."""
    if not has_role(user, "admin"):
        await deps.channel.send(chat_id, "Chỉ admin mới có thể xóa user.", use_markdown=False)
        return

    if not body.strip():
        await deps.channel.send(
            chat_id, "Cú pháp: xoa user: <tên/id>\nVí dụ: xoa user: 3",
            use_markdown=False,
        )
        return

    target = await _resolve_user_or_reply(body.strip(), chat_id, deps)
    if target is None:
        return

    if target.id == user.id:
        await deps.channel.send(
            chat_id, "Không thể tự xóa tài khoản của mình.", use_markdown=False,
        )
        return

    try:
        deps.user_store.soft_delete_user(target.id)
        await deps.channel.send(
            chat_id, f"Đã vô hiệu hóa user: {target.name} (id={target.id}).",
            use_markdown=False,
        )
    except Exception as e:
        await deps.channel.send(chat_id, f"Lỗi: {str(e)[:400]}", use_markdown=False)


async def _cmd_doi_role(
    chat_id: str, body: str, user: User, deps: CoreDeps,
) -> None:
    """doi role: <ten/id> <role moi> — admin changes an existing user's role."""
    if not has_role(user, "admin"):
        await deps.channel.send(chat_id, "Chỉ admin mới có thể đổi role.", use_markdown=False)
        return

    parts = body.strip().split(None, 1)
    if len(parts) != 2:
        await deps.channel.send(
            chat_id,
            "Cú pháp: doi role: <tên/id> <role mới>\n"
            "Role hợp lệ: admin, manager, member, readonly\n"
            "Ví dụ: doi role: an manager",
            use_markdown=False,
        )
        return

    target_token = parts[0].strip()
    new_role = parts[1].strip().lower()

    if new_role not in _VALID_ROLES:
        await deps.channel.send(
            chat_id,
            f"Role không hợp lệ: '{new_role}'. Chọn: admin, manager, member, readonly",
            use_markdown=False,
        )
        return

    target = await _resolve_user_or_reply(target_token, chat_id, deps)
    if target is None:
        return

    # Safety: prevent admin from demoting themselves (could lock out admin access).
    if target.id == user.id and new_role != "admin":
        await deps.channel.send(
            chat_id,
            "Không thể tự hạ role của chính mình. Nhờ một admin khác thực hiện.",
            use_markdown=False,
        )
        return

    if target.role == new_role:
        await deps.channel.send(
            chat_id,
            f"{target.name} (#{target.id}) đã ở role '{new_role}'. Không cần đổi.",
            use_markdown=False,
        )
        return

    try:
        old_role = target.role
        deps.user_store.update_user_role(target.id, new_role)
        await deps.channel.send(
            chat_id,
            f"Đã đổi role của {target.name} (#{target.id}): {old_role} → {new_role}.",
            use_markdown=False,
        )
    except Exception as e:
        await deps.channel.send(chat_id, f"Lỗi khi đổi role: {str(e)[:400]}", use_markdown=False)


_MIN_BIRTHDATE = "1900-01-01"


async def _cmd_dat_birthdate(
    chat_id: str, body: str, user: User, deps: CoreDeps,
) -> None:
    """dat birthdate: YYYY-MM-DD — request a birthdate change (requires manager approval)."""
    raw = body.strip()
    try:
        new_bd = date.fromisoformat(raw)
    except ValueError:
        await deps.channel.send(
            chat_id,
            "Định dạng không hợp lệ. Dùng: dat birthdate: YYYY-MM-DD\nVí dụ: dat birthdate: 1990-05-15",
            use_markdown=False,
        )
        return

    today = date.today()
    if new_bd > today:
        await deps.channel.send(chat_id, "Ngày sinh không thể là ngày tương lai.", use_markdown=False)
        return
    if new_bd < date.fromisoformat(_MIN_BIRTHDATE):
        await deps.channel.send(chat_id, f"Ngày sinh không hợp lệ (trước {_MIN_BIRTHDATE}).", use_markdown=False)
        return

    try:
        req_id = deps.user_store.request_birthdate_change(user.id, new_bd)
        await deps.channel.send(
            chat_id,
            f"Đã gửi yêu cầu đổi ngày sinh thành *{raw}* (mã #{req_id}).\n"
            f"Vui lòng chờ manager/admin duyệt.",
        )
    except ValueError as e:
        await deps.channel.send(chat_id, f"Bạn đã có yêu cầu đang chờ duyệt. {str(e)}", use_markdown=False)
    except Exception as e:
        await deps.channel.send(chat_id, f"Lỗi: {str(e)[:400]}", use_markdown=False)


async def _cmd_duyet_birthdate(
    chat_id: str, body: str, user: User, deps: CoreDeps,
) -> None:
    """duyet birthdate: <id> ok | tu choi [ly do] — manager approves or rejects a request."""
    if not can_manage(user):
        await deps.channel.send(
            chat_id, "Chỉ manager/admin mới có thể duyệt yêu cầu.", use_markdown=False,
        )
        return

    # No body → show pending list
    if not body.strip():
        pending = deps.user_store.list_pending_birthdate_changes()
        if not pending:
            await deps.channel.send(chat_id, "Không có yêu cầu ngày sinh nào đang chờ duyệt.", use_markdown=False)
            return
        lines = ["Yêu cầu đổi ngày sinh đang chờ:"]
        for r in pending:
            lines.append(f"• #{r['id']} — {r['user_name']} → {r['new_birthdate']}")
        lines.append("\nDùng: duyet birthdate: <id> ok | tu choi [ly do]")
        await deps.channel.send(chat_id, "\n".join(lines), use_markdown=False)
        return

    parts = body.strip().split(None, 2)
    if len(parts) < 2 or not parts[0].isdigit():
        await deps.channel.send(
            chat_id,
            "Cú pháp: duyet birthdate: <id> ok | tu choi [lý do]\n"
            "Hoặc: duyet birthdate để xem danh sách đang chờ.",
            use_markdown=False,
        )
        return

    req_id = int(parts[0])
    action = parts[1].lower()
    note = parts[2].strip() if len(parts) > 2 else ""

    if action in ("ok", "duyet", "duyệt"):
        ok = deps.user_store.approve_birthdate_change(req_id, user.id)
        if ok:
            await deps.channel.send(
                chat_id, f"Đã duyệt yêu cầu #{req_id}.", use_markdown=False,
            )
        else:
            await deps.channel.send(
                chat_id, f"Không tìm thấy yêu cầu #{req_id} hoặc đã xử lý.", use_markdown=False,
            )
    elif action in ("tu choi", "từ chối", "reject", "no"):
        ok = deps.user_store.reject_birthdate_change(req_id, user.id, note)
        if ok:
            reason = f" Lý do: {note}" if note else ""
            await deps.channel.send(
                chat_id, f"Đã từ chối yêu cầu #{req_id}.{reason}", use_markdown=False,
            )
        else:
            await deps.channel.send(
                chat_id, f"Không tìm thấy yêu cầu #{req_id} hoặc đã xử lý.", use_markdown=False,
            )
    else:
        await deps.channel.send(
            chat_id,
            f"Hành động không hợp lệ: '{action}'. Dùng 'ok' hoặc 'tu choi'.",
            use_markdown=False,
        )


async def _cmd_dat_username(
    chat_id: str, body: str, user: User, deps: CoreDeps,
) -> None:
    """dat username: <name> — set username for the first time, or queue a change request."""
    name = body.strip()
    if not name:
        await deps.channel.send(
            chat_id,
            "Cú pháp: dat username: <tên>\nVí dụ: dat username: alice99",
            use_markdown=False,
        )
        return

    err = validate_username(name)
    if err:
        await deps.channel.send(chat_id, err, use_markdown=False)
        return

    try:
        if user.username is None:
            deps.user_store.set_username_direct(user.id, name)
            await deps.channel.send(
                chat_id, f"Username đã được đặt thành *{name}*.",
            )
        else:
            req_id = deps.user_store.request_username_change(user.id, name)
            await deps.channel.send(
                chat_id,
                f"Đã gửi yêu cầu đổi username thành *{name}* (mã #{req_id}).\n"
                f"Vui lòng chờ admin duyệt.",
            )
    except ValueError as e:
        await deps.channel.send(chat_id, str(e), use_markdown=False)
    except Exception as e:
        await deps.channel.send(chat_id, f"Lỗi: {str(e)[:400]}", use_markdown=False)


async def _cmd_duyet_username(
    chat_id: str, body: str, user: User, deps: CoreDeps,
) -> None:
    """duyet username: <id> ok | tu choi [ly do] — admin approves or rejects a request."""
    if not has_role(user, "admin"):
        await deps.channel.send(
            chat_id, "Chỉ admin mới có thể duyệt yêu cầu đổi username.", use_markdown=False,
        )
        return

    if not body.strip():
        pending = deps.user_store.list_pending_username_changes()
        if not pending:
            await deps.channel.send(chat_id, "Không có yêu cầu đổi username nào đang chờ duyệt.", use_markdown=False)
            return
        lines = ["Yêu cầu đổi username đang chờ:"]
        for r in pending:
            old = r["old_username"] or "(chưa có)"
            lines.append(f"• #{r['id']} — {r['user_name']}: {old} → {r['new_username']}")
        lines.append("\nDùng: duyet username: <id> ok | tu choi [ly do]")
        await deps.channel.send(chat_id, "\n".join(lines), use_markdown=False)
        return

    parts = body.strip().split(None, 2)
    if len(parts) < 2 or not parts[0].isdigit():
        await deps.channel.send(
            chat_id,
            "Cú pháp: duyet username: <id> ok | tu choi [lý do]\n"
            "Hoặc: duyet username để xem danh sách đang chờ.",
            use_markdown=False,
        )
        return

    req_id = int(parts[0])
    action = parts[1].lower()
    note = parts[2].strip() if len(parts) > 2 else ""

    if action in ("ok", "duyet", "duyệt"):
        ok = deps.user_store.approve_username_change(req_id, user.id)
        if ok:
            await deps.channel.send(chat_id, f"Đã duyệt yêu cầu #{req_id}.", use_markdown=False)
        else:
            await deps.channel.send(chat_id, f"Không tìm thấy yêu cầu #{req_id} hoặc đã xử lý.", use_markdown=False)
    elif action in ("tu choi", "từ chối", "reject", "no"):
        ok = deps.user_store.reject_username_change(req_id, user.id, note)
        if ok:
            reason = f" Lý do: {note}" if note else ""
            await deps.channel.send(chat_id, f"Đã từ chối yêu cầu #{req_id}.{reason}", use_markdown=False)
        else:
            await deps.channel.send(chat_id, f"Không tìm thấy yêu cầu #{req_id} hoặc đã xử lý.", use_markdown=False)
    else:
        await deps.channel.send(
            chat_id,
            f"Hành động không hợp lệ: '{action}'. Dùng 'ok' hoặc 'tu choi'.",
            use_markdown=False,
        )


async def _cmd_dat_cha(
    chat_id: str, body: str, user: User, deps: CoreDeps,
) -> None:
    """dat cha: <tên/id-con> <tên/id-cha> — admin/manager sets a parent for a user."""
    if not has_role(user, "admin", "manager"):
        await deps.channel.send(chat_id, "Chỉ admin/manager mới có thể đặt quan hệ cha-con.", use_markdown=False)
        return

    parts = body.strip().split(None, 1)
    if len(parts) != 2:
        await deps.channel.send(
            chat_id,
            "Cú pháp: dat cha: <tên/id-con> <tên/id-cha>\n"
            "Ví dụ: dat cha: an 1 (user 'an' có cha là user #1)\n"
            "Dùng: dat cha: <tên/id-con> 0 để xóa quan hệ cha-con.",
            use_markdown=False,
        )
        return

    child_token, parent_token = parts[0].strip(), parts[1].strip()

    child = await _resolve_user_or_reply(child_token, chat_id, deps)
    if child is None:
        return

    # parent_token == "0" means remove relationship
    if parent_token == "0":
        try:
            removed = deps.user_store.remove_parent(child.id, user.id)
            if removed:
                await deps.channel.send(chat_id, f"Đã xóa quan hệ cha-con của {child.name} (#{child.id}).", use_markdown=False)
            else:
                await deps.channel.send(chat_id, f"{child.name} (#{child.id}) không có quan hệ cha-con nào đang hoạt động.", use_markdown=False)
        except ValueError as e:
            await deps.channel.send(chat_id, str(e), use_markdown=False)
        return

    parent = await _resolve_user_or_reply(parent_token, chat_id, deps)
    if parent is None:
        return

    try:
        deps.user_store.set_parent(child.id, parent.id, user.id)
        await deps.channel.send(
            chat_id,
            f"Đã đặt {child.name} (#{child.id}) có cha là {parent.name} (#{parent.id}).",
            use_markdown=False,
        )
    except ValueError as e:
        await deps.channel.send(chat_id, str(e), use_markdown=False)


async def _cmd_xem_cha(
    chat_id: str, body: str, user: User, deps: CoreDeps,
) -> None:
    """xem cha: <tên/id> — show parent and children of a user."""
    if not has_role(user, "admin", "manager"):
        await deps.channel.send(chat_id, "Chỉ admin/manager mới có thể xem quan hệ cha-con.", use_markdown=False)
        return

    if not body.strip():
        await deps.channel.send(
            chat_id, "Cú pháp: xem cha: <tên/id>", use_markdown=False,
        )
        return

    target = await _resolve_user_or_reply(body.strip(), chat_id, deps)
    if target is None:
        return

    lines = [f"Quan hệ của {target.name} (#{target.id}):"]

    parent = deps.user_store.get_parent(target.id)
    if parent:
        lines.append(f"• Cha: {parent.name} (#{parent.id})")
    else:
        lines.append("• Cha: (chưa có)")

    children = deps.user_store.get_children(target.id)
    if children:
        child_list = ", ".join(f"{c.name} (#{c.id})" for c in children)
        lines.append(f"• Con: {child_list}")
    else:
        lines.append("• Con: (chưa có)")

    await deps.channel.send(chat_id, "\n".join(lines), use_markdown=False)


async def _cmd_xem_quota(
    chat_id: str, body: str, user: User, deps: CoreDeps,
) -> None:
    """xem quota [tên/id] — show token quota. Admin can view any user; others see own."""
    if body.strip() and has_role(user, "admin", "manager"):
        target = await _resolve_user_or_reply(body.strip(), chat_id, deps)
        if target is None:
            return
    else:
        target = deps.user_store.get_user_by_id(user.id)
        if target is None:
            await deps.channel.send(chat_id, "Không tìm thấy thông tin user của bạn.", use_markdown=False)
            return

    quota = deps.user_store.get_quota(target.id)
    if quota is None or quota["monthly_token_limit"] == 0:
        limit_str = "không giới hạn"
    else:
        limit_str = f"{quota['monthly_token_limit']:,} tokens/tháng"

    used = quota["used_tokens"] if quota else 0
    month = quota["month"] if quota else "N/A"

    lines = [
        f"Quota của {target.name} (#{target.id}):",
        f"• Giới hạn: {limit_str}",
        f"• Đã dùng tháng {month}: {used:,} tokens",
    ]
    if quota and quota["monthly_token_limit"] > 0:
        pct = min(100, round(used / quota["monthly_token_limit"] * 100, 1))
        lines.append(f"• Sử dụng: {pct}%")

    await deps.channel.send(chat_id, "\n".join(lines), use_markdown=False)


async def _cmd_dat_quota(
    chat_id: str, body: str, user: User, deps: CoreDeps,
) -> None:
    """dat quota: <tên/id> <tokens> — admin sets monthly token limit (0 = unlimited)."""
    if not has_role(user, "admin"):
        await deps.channel.send(chat_id, "Chỉ admin mới có thể đặt quota.", use_markdown=False)
        return

    parts = body.strip().split(None, 1)
    if len(parts) != 2 or not parts[1].strip().isdigit():
        await deps.channel.send(
            chat_id,
            "Cú pháp: dat quota: <tên/id> <tokens>\n"
            "Ví dụ: dat quota: an 100000 (100k tokens/tháng)\n"
            "Dùng 0 để bỏ giới hạn.",
            use_markdown=False,
        )
        return

    target = await _resolve_user_or_reply(parts[0].strip(), chat_id, deps)
    if target is None:
        return

    limit = int(parts[1].strip())
    deps.user_store.set_quota(target.id, limit)
    if limit == 0:
        await deps.channel.send(chat_id, f"Đã bỏ giới hạn quota cho {target.name} (#{target.id}).", use_markdown=False)
    else:
        await deps.channel.send(
            chat_id,
            f"Đã đặt quota cho {target.name} (#{target.id}): {limit:,} tokens/tháng.",
            use_markdown=False,
        )


async def _cmd_reset_quota(
    chat_id: str, body: str, user: User, deps: CoreDeps,
) -> None:
    """reset quota: <tên/id> — admin resets a user's current-month usage to 0."""
    if not has_role(user, "admin"):
        await deps.channel.send(chat_id, "Chỉ admin mới có thể reset quota.", use_markdown=False)
        return

    if not body.strip():
        await deps.channel.send(chat_id, "Cú pháp: reset quota: <tên/id>", use_markdown=False)
        return

    target = await _resolve_user_or_reply(body.strip(), chat_id, deps)
    if target is None:
        return

    deps.user_store.reset_usage(target.id)
    await deps.channel.send(chat_id, f"Đã reset usage của {target.name} (#{target.id}).", use_markdown=False)


# ─── FR-4: Audit log viewer ───────────────────────────────────────────────────

_AUDIT_PAGE_SIZE = 20


def _fmt_relative_time(iso_ts: str) -> str:
    """Format a SQLite CURRENT_TIMESTAMP-style 'YYYY-MM-DD HH:MM:SS' as a short relative string."""
    from datetime import datetime, timezone

    try:
        # SQLite CURRENT_TIMESTAMP is UTC, no timezone suffix.
        ts = datetime.strptime(iso_ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return iso_ts or "?"

    now = datetime.now(timezone.utc)
    delta = now - ts
    secs = int(delta.total_seconds())
    if secs < 0:
        return iso_ts
    if secs < 60:
        return f"{secs}s trước"
    if secs < 3600:
        return f"{secs // 60}m trước"
    if secs < 86400:
        return f"{secs // 3600}h trước"
    return f"{secs // 86400}d trước"


async def _cmd_xem_audit(
    chat_id: str, body: str, user: User, deps: CoreDeps,
) -> None:
    """xem audit [page|action|target_type target_id] — admin views recent audit events.

    Usage forms:
      xem audit                       → page 1 (20 most recent events)
      xem audit 2                     → page 2
      xem audit sudo_elevate          → filter by action
      xem audit note 42               → filter by target_type + target_id
    """
    if not has_role(user, "admin"):
        await deps.channel.send(chat_id, "Chỉ admin mới có thể xem audit log.", use_markdown=False)
        return

    raw = body.strip()
    page = 1
    action_filter: str | None = None
    target_type_filter: str | None = None
    target_id_filter: str | None = None

    if raw:
        parts = raw.split(None, 1)
        first = parts[0]
        if first.isdigit():
            page = max(1, int(first))
        elif len(parts) == 2:
            # Two tokens — interpreted as `<target_type> <target_id>`.
            target_type_filter = first
            target_id_filter = parts[1].strip()
        else:
            # Single non-numeric token — interpreted as `<action>`.
            action_filter = first

    offset = (page - 1) * _AUDIT_PAGE_SIZE
    events = deps.audit.list_recent(
        limit=_AUDIT_PAGE_SIZE,
        offset=offset,
        action=action_filter,
        target_type=target_type_filter,
        target_id=target_id_filter,
    )

    if not events:
        if page > 1:
            await deps.channel.send(chat_id, f"Trang {page}: không có sự kiện.", use_markdown=False)
        else:
            await deps.channel.send(chat_id, "Audit log trống (chưa có sự kiện nào).", use_markdown=False)
        return

    # Build a name lookup for actor ids that appear in the page.
    actor_ids = {e.actor_user_id for e in events if e.actor_user_id is not None}
    name_by_id: dict[int, str] = {}
    for aid in actor_ids:
        u = deps.user_store.get_user_by_id(aid)
        if u is not None:
            name_by_id[aid] = u.name

    header = f"Audit log — trang {page} ({len(events)} sự kiện)"
    if action_filter:
        header += f" — action={action_filter}"
    elif target_type_filter:
        header += f" — target={target_type_filter} {target_id_filter}"
    lines = [header]

    for ev in events:
        rel = _fmt_relative_time(ev.created_at)
        actor = "system" if ev.actor_user_id is None else f"{name_by_id.get(ev.actor_user_id, '?')}#{ev.actor_user_id}"
        target = ""
        if ev.target_type:
            target = f" [{ev.target_type}"
            if ev.target_id:
                target += f" {ev.target_id}"
            target += "]"
        payload_hint = ""
        if ev.payload:
            # Compact one-line preview, truncated.
            try:
                import json as _json
                preview = _json.dumps(ev.payload, ensure_ascii=False)
            except Exception:
                preview = str(ev.payload)
            if len(preview) > 120:
                preview = preview[:117] + "..."
            payload_hint = f" {preview}"
        lines.append(f"• {rel} — {actor} — {ev.action}{target}{payload_hint}")

    if len(events) == _AUDIT_PAGE_SIZE:
        lines.append(f"\n(Trang sau: `xem audit {page + 1}`)")

    await deps.channel.send(chat_id, "\n".join(lines), use_markdown=False)


# ─── FR-4 sub 4.3: Recycle bin commands ───────────────────────────────────────

_RECYCLE_KINDS = ("user", "note", "wiki")


def _parse_recycle_target(body: str) -> "tuple[str, int] | None":
    """Parse `<kind> <id>` syntax used by `khoi phuc` and `xoa han`.

    Returns (kind, id) if valid; None otherwise. Kind must be one of user/note/wiki
    and id must be a positive integer.
    """
    parts = body.strip().split()
    if len(parts) != 2:
        return None
    kind = parts[0].lower()
    if kind not in _RECYCLE_KINDS:
        return None
    if not parts[1].isdigit():
        return None
    target_id = int(parts[1])
    if target_id <= 0:
        return None
    return kind, target_id


async def _cmd_xem_thung_rac(chat_id: str, user: User, deps: CoreDeps) -> None:
    """xem thung rac — admin lists all soft-deleted users/notes/wiki pages."""
    if not has_role(user, "admin"):
        await deps.channel.send(
            chat_id, "Chỉ admin mới có thể xem thùng rác.", use_markdown=False,
        )
        return

    deleted_users = deps.user_store.list_deleted_users()
    deleted_notes = deps.note_index.list_deleted_notes()
    deleted_wikis = deps.note_index.list_deleted_wiki_pages()

    deps.audit.log(
        actor_user_id=user.id,
        action="recycle_view",
        payload={
            "items": len(deleted_users) + len(deleted_notes) + len(deleted_wikis),
            "users": len(deleted_users),
            "notes": len(deleted_notes),
            "wiki": len(deleted_wikis),
        },
    )

    total = len(deleted_users) + len(deleted_notes) + len(deleted_wikis)
    if total == 0:
        await deps.channel.send(
            chat_id, "Thung rac trong.", use_markdown=False,
        )
        return

    lines = [f"Thung rac ({total} muc):"]
    if deleted_users:
        lines.append("\n— Users —")
        for u in deleted_users:
            del_at = u.deleted_at.strftime("%Y-%m-%d") if u.deleted_at else "?"
            lines.append(f"• [user {u.id}] {u.name} (role={u.role}) — da xoa {del_at}")
    if deleted_notes:
        lines.append("\n— Notes —")
        for n in deleted_notes:
            title = n.get("title") or "(no title)"
            del_at = (n.get("deleted_at") or "")[:10]
            lines.append(f"• [note {n['id']}] {title} (owner={n['owner_user_id']}) — da xoa {del_at}")
    if deleted_wikis:
        lines.append("\n— Wiki —")
        for w in deleted_wikis:
            del_at = (w.get("deleted_at") or "")[:10]
            lines.append(f"• [wiki {w['id']}] {w['topic']} (owner={w['owner_user_id']}) — da xoa {del_at}")

    lines.append(
        "\nKhoi phuc: `khoi phuc: <kind> <id>` (vd `khoi phuc: user 3`)"
        "\nXoa han:   `xoa han: <kind> <id>`"
    )
    await deps.channel.send(chat_id, "\n".join(lines), use_markdown=False)


async def _cmd_khoi_phuc(
    chat_id: str, body: str, user: User, deps: CoreDeps,
) -> None:
    """khoi phuc: <kind> <id> — admin restores a soft-deleted item."""
    if not has_role(user, "admin"):
        await deps.channel.send(
            chat_id, "Chỉ admin mới có thể khôi phục.", use_markdown=False,
        )
        return

    parsed = _parse_recycle_target(body)
    if parsed is None:
        await deps.channel.send(
            chat_id,
            "Cu phap: khoi phuc: <kind> <id>\n"
            "Kind hop le: user, note, wiki\n"
            "Vi du: khoi phuc: user 3",
            use_markdown=False,
        )
        return

    kind, target_id = parsed

    if kind == "user":
        ok = deps.user_store.restore_user(target_id)
        label = f"user #{target_id}"
    elif kind == "note":
        ok = deps.note_index.restore_note(target_id)
        label = f"note #{target_id}"
    else:  # wiki
        ok = deps.note_index.restore_wiki(target_id)
        label = f"wiki #{target_id}"

    if not ok:
        await deps.channel.send(
            chat_id,
            f"Khong tim thay {label} trong thung rac (hoac da khoi phuc).",
            use_markdown=False,
        )
        return

    deps.audit.log(
        actor_user_id=user.id,
        action="recycle_restore",
        target_type=kind,
        target_id=target_id,
    )
    await deps.channel.send(
        chat_id, f"Da khoi phuc {label}.", use_markdown=False,
    )


async def _cmd_xoa_han(
    chat_id: str, body: str, user: User, deps: CoreDeps,
) -> None:
    """xoa han: <kind> <id> — admin permanently purges an item.

    For notes/wiki: also issues a best-effort Drive delete. For users: detects
    FK constraint violations and surfaces a clear error.
    """
    if not has_role(user, "admin"):
        await deps.channel.send(
            chat_id, "Chỉ admin mới có thể xóa hẳn.", use_markdown=False,
        )
        return

    parsed = _parse_recycle_target(body)
    if parsed is None:
        await deps.channel.send(
            chat_id,
            "Cu phap: xoa han: <kind> <id>\n"
            "Kind hop le: user, note, wiki\n"
            "Vi du: xoa han: note 12",
            use_markdown=False,
        )
        return

    kind, target_id = parsed

    if kind == "user":
        ok = deps.user_store.hard_delete_user(target_id)
        if not ok:
            await deps.channel.send(
                chat_id,
                f"Khong the xoa han user #{target_id}. "
                "Co the user khong ton tai, hoac con du lieu tham chieu "
                "(channel_bindings, notes, parent_links...). "
                "Hay thu khoi phuc + cleanup tay neu can.",
                use_markdown=False,
            )
            return
        deps.audit.log(
            actor_user_id=user.id,
            action="recycle_purge",
            target_type="user",
            target_id=target_id,
        )
        await deps.channel.send(
            chat_id, f"Da xoa han user #{target_id}.", use_markdown=False,
        )
        return

    # note / wiki — purge SQLite + best-effort Drive delete
    if kind == "note":
        meta = deps.note_index.hard_delete_note(target_id)
        adapter = deps.notes
        target_type = "note"
    else:  # wiki
        meta = deps.note_index.hard_delete_wiki(target_id)
        adapter = deps.wiki
        target_type = "wiki"

    if meta is None:
        await deps.channel.send(
            chat_id, f"Khong tim thay {kind} #{target_id}.", use_markdown=False,
        )
        return

    drive_file_id = meta.get("drive_file_id")
    drive_deleted = False
    if drive_file_id:
        try:
            drive_deleted = bool(adapter.delete_file(drive_file_id))
        except Exception as e:
            print(f"[recycle] Drive delete exception (kind={kind} id={target_id}): {e}")
            drive_deleted = False

    deps.audit.log(
        actor_user_id=user.id,
        action="recycle_purge",
        target_type=target_type,
        target_id=target_id,
        payload={"drive_file_id": drive_file_id, "drive_deleted": drive_deleted},
    )

    suffix = " (Drive deleted)" if drive_deleted else " (Drive delete failed — file orphaned)"
    await deps.channel.send(
        chat_id, f"Da xoa han {kind} #{target_id}.{suffix}", use_markdown=False,
    )


async def _cmd_start(chat_id: str, deps: CoreDeps) -> None:
    await deps.channel.send(chat_id, (
        "Xin chao! Toi la Claude Bot.\n\n"
        "Chon nhom lenh de xem chi tiet:\n\n"
        "📝 *Ghi chu & Nhat ky* — `/help ghi chu`\n"
        "📚 *Wiki* — `/help wiki`\n"
        "🧠 *Tri nho* — `/help tri nho`\n"
        "👥 *Nguoi dung* — `/help nguoi dung`\n"
        "💰 *Quota* — `/help quota`\n"
        "🔍 *Tim kiem & Xem* — `/help xem`\n"
        "🔐 *Quan tri (sudo)* — `/help sudo`\n"
        "⚙️ *He thong* — `/help he thong`\n\n"
        "💬 *Hoi dap tu do:* Gõ cau hoi bat ky — bot tim wiki + vault roi tra loi."
    ))


_HELP_PAGES: dict[str, tuple[str, str]] = {
    "ghi chu": (
        "📝 *GHI CHU & NHAT KY*",
        "`ghi nho [noi dung]` — Tao file ghi chu moi (Claude tu dat ten)\n"
        "`ghi nho vao [ten]: [noi dung]` — Them vao file co san (fuzzy match)\n"
        "`nhat ky [noi dung]` — Them vao file nhat ky hom nay (GMT+7)\n"
        "`chia se [ten-file]` — Chia se file voi ca nha (scope = everyone)\n"
        "`bo chia se [ten-file]` — Chuyen file ve rieng tu (scope = private)",
    ),
    "wiki": (
        "📚 *WIKI*",
        "`wiki [noi dung]` — Ingest vao wiki (Claude tu to chuc theo topic)\n"
        "`hoi wiki [cau hoi]` — Hoi truc tiep tu wiki\n"
        "`xem wiki` — Liet ke tat ca wiki pages\n"
        "`xem wiki [topic]` — Doc 1 wiki page",
    ),
    "nguoi dung": (
        "👥 *NGUOI DUNG*",
        "`them user: [ten], [role]` — Them user moi (admin)\n"
        "`xem danh sach user` — Liet ke tat ca user (admin)\n"
        "`xoa user: [ten/id]` — Xoa user (admin)\n"
        "`doi role: [ten/id] [role moi]` — Doi role cua user (admin)\n"
        "`dat username: [ten]` — Dat username cua ban\n"
        "`duyet username` — Duyet yeu cau doi username (admin)\n"
        "`dat birthdate: [YYYY-MM-DD]` — Dat ngay sinh\n"
        "`duyet birthdate` — Duyet yeu cau doi ngay sinh (admin/manager)\n"
        "`dat cha: [ten/id-con] [ten/id-cha]` — Gan quan he cha/me — con (admin)\n"
        "`xem cha: [ten/id]` — Xem quan he cha/me cua user (admin)\n"
        "`toi la ai` — Xem tai khoan ban dang dung",
    ),
    "quota": (
        "💰 *QUOTA*",
        "`xem quota` — Xem muc su dung token cua ban\n"
        "`xem quota [ten/id]` — Xem quota cua user khac (admin)\n"
        "`dat quota: [ten/id] [so-token]` — Dat gioi han token (admin)\n"
        "`reset quota: [ten/id]` — Reset so dung ve 0 (admin)",
    ),
    "xem": (
        "🔍 *TIM KIEM & XEM*",
        "`xem nhat ky` — Doc nhat ky hom nay\n"
        "`xem [ten]` — Doc 1 file (fuzzy match)\n"
        "`xem scope [ten]` — Xem scope/owner cua 1 file\n"
        "`liet ke` — Liet ke tat ca file (phan trang, moi nhat truoc)\n"
        "`liet ke [trang]` — Xem trang cu the\n"
        "`tim [tu khoa]` — Tim trong noi dung file\n"
        "`tom tat tuan nay` — Tom tat ghi chu 7 ngay",
    ),
    "tri nho": (
        "🧠 *TRI NHO*",
        "`xem tri nho` — Xem snapshot bo nho cua ban\n"
        "`xem ho so` — Xem ho so ca nhan cua ban\n"
        "`cap nhat tri nho` — Cap nhat bo nho tu ghi chu gan day (LLM curation)",
    ),
    "sudo": (
        "🔐 *QUAN TRI (SUDO)*",
        "`sudo: [mat khau]` — Nang quyen len admin trong 15 phut (chi role manager)\n"
        "`thoat sudo` — Ha quyen admin ngay lap tuc\n"
        "`dat mat khau: [mat khau]` — Dat/doi mat khau admin (chi tu tai khoan admin goc)\n"
        "`xem audit` — Xem audit log gan day (admin); ho tro phan trang va filter\n"
        "  `xem audit [trang]` — Trang cu the (vd `xem audit 2`)\n"
        "  `xem audit [action]` — Filter theo action (vd `xem audit sudo_elevate`)\n"
        "  `xem audit [type] [id]` — Filter theo target (vd `xem audit note 42`)\n"
        "`xem thung rac` — Liet ke item da xoa (user/note/wiki) (admin)\n"
        "`khoi phuc: [kind] [id]` — Khoi phuc item (vd `khoi phuc: user 3`) (admin)\n"
        "`xoa han: [kind] [id]` — Xoa han khoi he thong (vd `xoa han: note 12`) (admin)\n"
        "`xuat du lieu` — Export du lieu cua ban len Drive (ZIP) (moi user; gioi han 5 phut)\n"
        "`xuat du lieu: [ten]` — Admin export du lieu cua nguoi khac len Drive (admin only)\n"
        "Luu y: tin nhan chua mat khau se duoc bot tu dong xoa khoi chat.",
    ),
    "he thong": (
        "⚙️ *HE THONG*",
        "`/cost` — Chi phi su dung thang nay\n"
        "`/test` — Kiem tra ket noi Drive\n"
        "`/security` — Cau hinh bao mat",
    ),
}

# Alias map: normalized input → canonical key in _HELP_PAGES
_HELP_ALIASES: dict[str, str] = {
    "ghi chu": "ghi chu",
    "ghi chú": "ghi chu",
    "wiki": "wiki",
    "nguoi dung": "nguoi dung",
    "người dùng": "nguoi dung",
    "quota": "quota",
    "xem": "xem",
    "tri nho": "tri nho",
    "trí nhớ": "tri nho",
    "he thong": "he thong",
    "hệ thống": "he thong",
    "sudo": "sudo",
    "quan tri": "sudo",
    "quản trị": "sudo",
}


async def _cmd_help(chat_id: str, group: str, deps: CoreDeps) -> None:
    key = _HELP_ALIASES.get(_norm(group).strip())
    if key is None:
        groups = "  ".join(f"`/help {k}`" for k in _HELP_PAGES)
        await deps.channel.send(
            chat_id,
            f"Nhom lenh '{group}' khong ton tai.\n\nCac nhom: {groups}",
            use_markdown=False,
        )
        return
    title, body = _HELP_PAGES[key]
    await deps.channel.send(chat_id, f"{title}\n\n{body}")


async def _cmd_cost(chat_id: str, deps: CoreDeps) -> None:
    info = get_current_cost()
    bar_filled = int(info["percent"] / 10)
    bar = "█" * bar_filled + "░" * (10 - bar_filled)
    await deps.channel.send(chat_id, (
        f"Chi phi thang {info['month']}\n\n"
        f"`{bar}` {info['percent']}%\n"
        f"Da dung: `${info['cost_usd']}` / `$10.00`\n"
        f"Input tokens: `{info.get('input_tokens', 0):,}`\n"
        f"Output tokens: `{info.get('output_tokens', 0):,}`"
    ))


async def _cmd_test(chat_id: str, deps: CoreDeps) -> None:
    await deps.channel.send(chat_id, "Dang kiem tra Drive...")
    try:
        result = deps.notes.test_connection()
        await deps.channel.send(
            chat_id,
            f"OK Drive\nFolder: {result.get('name')}\nID: {result.get('id')}",
            use_markdown=False,
        )
    except Exception as e:
        traceback.print_exc()
        await deps.channel.send(
            chat_id, f"Drive Error: {str(e)[:500]}", use_markdown=False,
        )


async def _cmd_security(chat_id: str, deps: CoreDeps) -> None:
    try:
        s = get_security_status()
        msg = (
            f"Cau hinh bao mat:\n\n"
            f"Scope: {s['scope']}\n"
            f"Folder ID: {s.get('configured_folder_id', 'N/A')}\n"
            f"Trusted folders: {s.get('trusted_folders_count', 0)}\n"
            f"Owner email: {s['owner_email']}\n"
            f"Transfer ownership: {s['ownership_transfer_enabled']}\n"
            f"Rate limit: {s['rate_limit_used']} files/hour\n"
            f"Allowed extensions: {s['allowed_extensions']}\n"
            f"Allowed mimetypes: {s['allowed_mimetypes']}"
        )
        await deps.channel.send(chat_id, msg, use_markdown=False)
    except Exception as e:
        traceback.print_exc()
        await deps.channel.send(chat_id, f"Loi: {str(e)[:500]}", use_markdown=False)


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


async def _cmd_ghi_nho(chat_id: str, content: str, user: User, deps: CoreDeps) -> None:
    """ghi nhớ <content> → create a new file with a Claude-generated title."""
    if not content:
        await deps.channel.send(chat_id, "Vui long nhap noi dung can ghi nho.")
        return
    await deps.channel.send(chat_id, "Dang luu...")
    try:
        title, tokens = deps.llm.ask(
            f"Tao tieu de ngan (toi da 6 tu) cho ghi chu sau, chi tra ve tieu de: {content}"
        )
        record_usage(tokens // 2, tokens // 2)
        filename, file_id = deps.notes.save_note(title.strip(), content)
        _register_note(file_id, user.id, "note", filename, deps)
        await deps.channel.send(chat_id, f"Da luu: {filename}", use_markdown=False)
    except PermissionError as e:
        traceback.print_exc()
        await deps.channel.send(
            chat_id, f"Tu choi vi ly do bao mat: {str(e)[:400]}", use_markdown=False,
        )
    except Exception as e:
        traceback.print_exc()
        await deps.channel.send(
            chat_id, f"Loi khi luu: {str(e)[:500]}", use_markdown=False,
        )


async def _cmd_ghi_nho_vao(chat_id: str, body: str, deps: CoreDeps) -> None:
    """ghi nhớ vào <name>: <content> — split on the first ':' and append."""
    if ":" not in body:
        await deps.channel.send(
            chat_id,
            "Cu phap: ghi nho vao <ten-file>: <noi dung>\n"
            "Vi du: ghi nho vao kiem tra: them cau hoi moi",
            use_markdown=False,
        )
        return

    name_part, content = body.split(":", 1)
    name_part = name_part.strip()
    content = content.strip()

    if not name_part:
        await deps.channel.send(chat_id, "Thieu ten file.", use_markdown=False)
        return
    if not content:
        await deps.channel.send(chat_id, "Thieu noi dung.", use_markdown=False)
        return

    await deps.channel.send(
        chat_id, f"Dang tim file '{name_part}'...", use_markdown=False,
    )

    try:
        matches = deps.notes.find_files_fuzzy(name_part)
    except Exception as e:
        traceback.print_exc()
        await deps.channel.send(
            chat_id, f"Loi khi tim: {str(e)[:400]}", use_markdown=False,
        )
        return

    # Case 1: exactly one match → append directly.
    if len(matches) == 1:
        chosen = matches[0]
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
        return

    # Case 2: multiple matches → ask user to pick.
    if len(matches) > 1:
        shown = matches[:FUZZY_SHOW_LIMIT]
        msg_lines = [f"Tim thay {len(matches)} file khop voi '{name_part}':"]
        for i, f in enumerate(shown, 1):
            msg_lines.append(f"{i}. {f['name']}")
        if len(matches) > FUZZY_SHOW_LIMIT:
            msg_lines.append(f"... ({len(matches) - FUZZY_SHOW_LIMIT} file khac)")
        msg_lines.append(f"\nTra loi 1-{len(shown)} de chon, hoac 'huy'.")
        msg_lines.append(f"(Het han sau {PENDING_CHOICE_TIMEOUT_SEC}s)")

        _set_pending(chat_id, "fuzzy_append", {
            "matches": shown,
            "content": content,
        })
        await deps.channel.send(chat_id, "\n".join(msg_lines), use_markdown=False)
        return

    # Case 3: no match → offer to create.
    _set_pending(chat_id, "create_new_confirm", {
        "filename": name_part,
        "content": content,
    })
    await deps.channel.send(
        chat_id,
        f"Khong tim thay file '{name_part}'.\n"
        f"Tao file moi voi ten do? (yes/no)\n"
        f"(Het han sau {PENDING_CHOICE_TIMEOUT_SEC}s)",
        use_markdown=False,
    )


async def _cmd_nhat_ky(chat_id: str, content: str, user: User, deps: CoreDeps) -> None:
    """nhật ký <content> → append to today's journal."""
    if not content:
        await deps.channel.send(chat_id, "Vui long nhap noi dung.", use_markdown=False)
        return

    await deps.channel.send(chat_id, "Dang ghi nhat ky...")
    try:
        filename, action, file_id = deps.notes.add_to_daily_journal(content)
        if action == "created":
            _register_note(file_id, user.id, "journal", filename, deps)
        else:
            deps.note_index.touch_note(file_id)
        verb = "Da tao moi" if action == "created" else "Da them vao"
        await deps.channel.send(
            chat_id, f"{verb}: {filename}", use_markdown=False,
        )
    except PermissionError as e:
        traceback.print_exc()
        await deps.channel.send(
            chat_id, f"Tu choi vi ly do bao mat: {str(e)[:400]}", use_markdown=False,
        )
    except Exception as e:
        traceback.print_exc()
        await deps.channel.send(chat_id, f"Loi: {str(e)[:500]}", use_markdown=False)


async def _cmd_xem_nhat_ky(chat_id: str, deps: CoreDeps) -> None:
    """xem nhật ký → read today's journal."""
    try:
        journal = deps.notes.get_today_journal()
        if not journal:
            await deps.channel.send(
                chat_id,
                f"Chua co nhat ky cho ngay {today_str()}. "
                f"Hay tao bang lenh: nhat ky <noi dung>",
                use_markdown=False,
            )
            return
        content = journal["content"]
        if len(content) > 3500:
            content = content[:3500] + "\n\n[...] (qua dai, da cat)"
        await deps.channel.send(
            chat_id, f"=== {journal['name']} ===\n\n{content}", use_markdown=False,
        )
    except Exception as e:
        traceback.print_exc()
        await deps.channel.send(chat_id, f"Loi: {str(e)[:500]}", use_markdown=False)


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


async def _cmd_xem(chat_id: str, name_query: str, user: User, deps: CoreDeps) -> None:
    """xem <name> → read a file (fuzzy match, ACL-filtered)."""
    if not name_query:
        await deps.channel.send(chat_id, "Cu phap: xem <ten-file>", use_markdown=False)
        return

    try:
        matches = deps.notes.find_files_fuzzy(name_query)
    except Exception as e:
        traceback.print_exc()
        await deps.channel.send(
            chat_id, f"Loi khi tim: {str(e)[:400]}", use_markdown=False,
        )
        return

    matches, _ = _visible_notes_with_meta(matches, user, deps)

    if not matches:
        await deps.channel.send(
            chat_id,
            f"Khong tim thay file nao khop voi '{name_query}'.",
            use_markdown=False,
        )
        return

    if len(matches) == 1:
        chosen = matches[0]
        try:
            file_data = deps.notes.read_file_by_id(chosen["id"])
            content = file_data["content"]
            if len(content) > 3500:
                content = content[:3500] + "\n\n[...] (qua dai, da cat)"
            await deps.channel.send(
                chat_id, f"=== {file_data['name']} ===\n\n{content}",
                use_markdown=False,
            )
        except Exception as e:
            traceback.print_exc()
            await deps.channel.send(
                chat_id, f"Loi khi doc: {str(e)[:400]}", use_markdown=False,
            )
        return

    # Multiple matches → ask user to pick.
    shown = matches[:FUZZY_SHOW_LIMIT]
    msg_lines = [f"Tim thay {len(matches)} file khop voi '{name_query}':"]
    for i, f in enumerate(shown, 1):
        msg_lines.append(f"{i}. {f['name']}")
    if len(matches) > FUZZY_SHOW_LIMIT:
        msg_lines.append(f"... ({len(matches) - FUZZY_SHOW_LIMIT} file khac)")
    msg_lines.append(f"\nTra loi 1-{len(shown)} de chon, hoac 'huy'.")

    _set_pending(chat_id, "fuzzy_view", {"matches": shown})
    await deps.channel.send(chat_id, "\n".join(msg_lines), use_markdown=False)


_LIET_KE_PAGE_SIZE = 20


async def _cmd_liet_ke(
    chat_id: str, page_arg: str, user: User, deps: CoreDeps
) -> None:
    """liet ke [trang] → list all visible files, newest-created first, paginated."""
    page = 1
    if page_arg.strip().isdigit():
        page = max(1, int(page_arg.strip()))

    try:
        files = deps.notes.list_all_notes()
    except Exception as e:
        traceback.print_exc()
        await deps.channel.send(chat_id, f"Loi: {str(e)[:500]}", use_markdown=False)
        return

    visible, metas = _visible_notes_with_meta(files, user, deps)
    if not visible:
        await deps.channel.send(
            chat_id, "Vault trong, chua co ghi chu nao.", use_markdown=False,
        )
        return

    total = len(visible)
    total_pages = (total + _LIET_KE_PAGE_SIZE - 1) // _LIET_KE_PAGE_SIZE
    page = min(page, total_pages)
    start = (page - 1) * _LIET_KE_PAGE_SIZE
    chunk = visible[start:start + _LIET_KE_PAGE_SIZE]

    lines = [f"Tat ca file ({total} file) - Trang {page}/{total_pages}", ""]
    for i, f in enumerate(chunk, start + 1):
        meta = metas.get(f["id"])
        icon = "🌐" if (meta and meta["scope"] == "everyone") else "🔒"
        created = (f.get("createdTime") or "")[:10]
        lines.append(f"{i:>2}. {icon} {f['name']}  ({created})")

    if page < total_pages:
        lines.append("")
        lines.append(f"Trang sau: liet ke {page + 1}")

    await deps.channel.send(chat_id, "\n".join(lines), use_markdown=False)


async def _cmd_tim(chat_id: str, keyword: str, user: User, deps: CoreDeps) -> None:
    if not keyword:
        await deps.channel.send(chat_id, "Vui long nhap tu khoa.")
        return
    await deps.channel.send(
        chat_id, f"Dang tim '{keyword}'...", use_markdown=False,
    )
    try:
        notes = deps.notes.search_notes(keyword)
        notes = _acl_filter_notes(notes, user, deps)
        if not notes:
            await deps.channel.send(
                chat_id, "Khong tim thay ghi chu nao.", use_markdown=False,
            )
            return
        summary, tokens = deps.llm.summarize_notes(notes)
        record_usage(tokens // 2, tokens // 2)
        check_and_alert()
        await deps.channel.send(
            chat_id, f"Ket qua:\n\n{summary}", use_markdown=False,
        )
    except Exception as e:
        traceback.print_exc()
        await deps.channel.send(
            chat_id, f"Loi khi tim: {str(e)[:500]}", use_markdown=False,
        )


def _update_index_after_create(
    topic: str,
    filename: str,
    topic_type: str,
    content_to_add: str,
    deps: CoreDeps,
) -> None:
    """Generate a TLDR and append it to the wiki index. Non-fatal on error."""
    try:
        tldr, tldr_tokens = deps.llm.generate_wiki_tldr(topic, content_to_add)
        record_usage(tldr_tokens // 2, tldr_tokens // 2)
        slug = filename.replace(".md", "")
        deps.wiki.add_to_index(topic, slug, topic_type, tldr)
    except Exception as e:
        print(f"[core] Wiki index update (non-fatal): {e}")


async def _cmd_set_scope(
    chat_id: str, name: str, new_scope: str, user: User, deps: CoreDeps
) -> None:
    """Shared logic for chia se / bo chia se — change scope of a note or wiki page."""
    if not name:
        verb = "chia se" if new_scope == "everyone" else "bo chia se"
        await deps.channel.send(
            chat_id, f"Cu phap: {verb} <ten-file>", use_markdown=False,
        )
        return

    # 1. Search notes folder first.
    try:
        matches = deps.notes.find_files_fuzzy(name)
    except Exception as e:
        await deps.channel.send(chat_id, f"Loi khi tim: {str(e)[:400]}", use_markdown=False)
        return

    if len(matches) > 1:
        names = "\n".join(f"- {m['name']}" for m in matches[:5])
        await deps.channel.send(
            chat_id,
            f"Tim thay {len(matches)} file khop voi '{name}':\n{names}\n\nVui long nhap ten cu the hon.",
            use_markdown=False,
        )
        return

    if len(matches) == 1:
        file_id = matches[0]["id"]
        meta = deps.note_index.get_note_meta(file_id)
        if meta is None:
            await deps.channel.send(
                chat_id,
                "File nay chua duoc index. Vui long lien he admin de backfill.",
                use_markdown=False,
            )
            return
        if meta["owner_user_id"] != user.id:
            await deps.channel.send(
                chat_id, "Ban khong phai chu file nay.", use_markdown=False,
            )
            return
        ok = deps.note_index.set_note_scope(file_id, new_scope, user.id)
        if ok:
            label = "chia se voi moi nguoi" if new_scope == "everyone" else "rieng tu"
            await deps.channel.send(
                chat_id,
                f"Da doi '{matches[0]['name']}' thanh {label}.",
                use_markdown=False,
            )
        else:
            await deps.channel.send(chat_id, "Khong the doi scope.", use_markdown=False)
        return

    # 2. No note match — try wiki.
    try:
        page = deps.wiki.find_page(name)
    except Exception as e:
        await deps.channel.send(chat_id, f"Loi khi tim wiki: {str(e)[:400]}", use_markdown=False)
        return

    if page:
        file_id = page["id"]
        meta = deps.note_index.get_wiki_meta(file_id)
        if meta is None:
            await deps.channel.send(
                chat_id,
                "Trang wiki nay chua duoc index. Vui long lien he admin de backfill.",
                use_markdown=False,
            )
            return
        if meta["owner_user_id"] != user.id:
            await deps.channel.send(
                chat_id, "Ban khong phai chu trang wiki nay.", use_markdown=False,
            )
            return
        ok = deps.note_index.set_wiki_scope(file_id, new_scope, user.id)
        if ok:
            label = "chia se voi moi nguoi" if new_scope == "everyone" else "rieng tu"
            await deps.channel.send(
                chat_id,
                f"Da doi wiki '{page['name'].removesuffix('.md')}' thanh {label}.",
                use_markdown=False,
            )
        else:
            await deps.channel.send(chat_id, "Khong the doi scope.", use_markdown=False)
        return

    await deps.channel.send(
        chat_id, f"Khong tim thay file '{name}' trong ghi chu hoac wiki.", use_markdown=False,
    )


async def _cmd_chia_se(chat_id: str, name: str, user: User, deps: CoreDeps) -> None:
    """chia se <ten-file> — set scope = everyone (share with all family members)."""
    await _cmd_set_scope(chat_id, name, "everyone", user, deps)


async def _cmd_bo_chia_se(chat_id: str, name: str, user: User, deps: CoreDeps) -> None:
    """bo chia se <ten-file> — set scope = private (owner only)."""
    await _cmd_set_scope(chat_id, name, "private", user, deps)


async def _send_scope_info(
    chat_id: str, filename: str, meta: dict, deps: CoreDeps, is_wiki: bool = False
) -> None:
    """Send a formatted scope/owner/kind summary for one note or wiki page."""
    owner = deps.user_store.get_user_by_id(meta["owner_user_id"])
    owner_str = f"{owner.name} (#{owner.id})" if owner else f"#{meta['owner_user_id']}"
    if meta["scope"] == "everyone":
        scope_str = "🌐 chia se voi moi nguoi"
    else:
        scope_str = "🔒 rieng tu"
    kind = "wiki" if is_wiki else meta.get("kind", "note")
    created = (meta.get("created_at") or "")[:10]
    lines = [
        f"📄 {filename}",
        f"   Scope: {scope_str}",
        f"   Owner: {owner_str}",
        f"   Loai:  {kind}",
        f"   Ngay:  {created}",
    ]
    await deps.channel.send(chat_id, "\n".join(lines), use_markdown=False)


async def _cmd_xem_scope(
    chat_id: str, name: str, user: User, deps: CoreDeps
) -> None:
    """xem scope <ten-file> — show scope/owner/kind of a note or wiki page."""
    if not name:
        await deps.channel.send(
            chat_id, "Cu phap: xem scope <ten-file>", use_markdown=False,
        )
        return

    # 1. Search notes folder first.
    try:
        matches = deps.notes.find_files_fuzzy(name)
    except Exception as e:
        await deps.channel.send(chat_id, f"Loi khi tim: {str(e)[:400]}", use_markdown=False)
        return

    if len(matches) > 1:
        names = "\n".join(f"- {m['name']}" for m in matches[:FUZZY_SHOW_LIMIT])
        await deps.channel.send(
            chat_id,
            f"Tim thay {len(matches)} file khop voi '{name}':\n{names}\n\n"
            f"Vui long nhap ten cu the hon.",
            use_markdown=False,
        )
        return

    if len(matches) == 1:
        meta = deps.note_index.get_note_meta(matches[0]["id"])
        if meta is None:
            await deps.channel.send(
                chat_id, "File nay chua duoc index.", use_markdown=False,
            )
            return
        allowed, is_stealth = acl_mod.can_read(
            user, meta["scope"], meta["owner_user_id"], user_store=deps.user_store,
        )
        if not allowed:
            await deps.channel.send(
                chat_id, f"Khong tim thay file '{name}'.", use_markdown=False,
            )
            return
        if is_stealth:
            deps.audit.log(
                actor_user_id=user.id,
                action="stealth_read_note",
                target_type="note",
                target_id=meta["drive_file_id"],
                payload={"owner_user_id": meta["owner_user_id"]},
            )
        await _send_scope_info(chat_id, matches[0]["name"], meta, deps)
        return

    # 2. No note match — try wiki.
    try:
        page = deps.wiki.find_page(name)
    except Exception as e:
        await deps.channel.send(chat_id, f"Loi khi tim wiki: {str(e)[:400]}", use_markdown=False)
        return

    if page:
        meta = deps.note_index.get_wiki_meta(page["id"])
        if meta is None:
            await deps.channel.send(
                chat_id, "Trang wiki nay chua duoc index.", use_markdown=False,
            )
            return
        allowed, is_stealth = acl_mod.can_read(
            user, meta["scope"], meta["owner_user_id"], user_store=deps.user_store,
        )
        if not allowed:
            await deps.channel.send(
                chat_id, f"Khong tim thay file '{name}'.", use_markdown=False,
            )
            return
        if is_stealth:
            deps.audit.log(
                actor_user_id=user.id,
                action="stealth_read_wiki",
                target_type="wiki_page",
                target_id=meta["drive_file_id"],
                payload={"owner_user_id": meta["owner_user_id"]},
            )
        await _send_scope_info(chat_id, page["name"], meta, deps, is_wiki=True)
        return

    await deps.channel.send(
        chat_id, f"Khong tim thay file '{name}' trong ghi chu hoac wiki.", use_markdown=False,
    )


async def _cmd_whoami(chat_id: str, user: User, deps: CoreDeps) -> None:
    """toi la ai — show the user bound to this chat, plus any active elevation."""
    role_labels = {
        "admin": "Quan tri vien",
        "manager": "Nguoi quan ly",
        "member": "Thanh vien",
        "readonly": "Chi doc",
    }
    # Resolve base role from DB so elevation override doesn't mask it.
    base = deps.user_store.get_user_by_id(user.id) or user
    session = deps.elevation_store.get_active_session("telegram", chat_id)

    username = base.username or "(chua dat)"
    lines = [
        "👤 Tai khoan hien tai:",
        f"   Ten:      {base.name}",
        f"   Username: {username}",
        f"   Vai tro:  {role_labels.get(base.role, base.role)}",
        f"   User ID:  #{base.id}",
    ]
    if session is not None:
        remaining = _elevation_remaining_minutes(session["expires_at"])
        if remaining > 0:
            lines.append(f"   Sudo:     dang nang quyen admin (con ~{remaining} phut)")
    await deps.channel.send(chat_id, "\n".join(lines), use_markdown=False)


# ── FR-3.5 — sudo / dat mat khau ────────────────────────────────────────────

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


async def _try_delete_message(
    chat_id: str, message_id: int | None, deps: CoreDeps
) -> None:
    """Best-effort delete of a message containing a password. Never raises."""
    if message_id is None:
        return
    try:
        await deps.channel.delete_message(chat_id, message_id)
    except Exception as e:
        print(f"[sudo] delete_message error (non-fatal): {e}")


async def _cmd_dat_mat_khau(
    chat_id: str,
    password: str,
    user: User,
    message_id: int | None,
    deps: CoreDeps,
) -> None:
    """dat mat khau: <mat khau> — set/replace the admin password.

    Allowed only for a natively-admin (role='admin' in DB and no active
    elevation session for this chat). This is both the initial-set path and
    the recovery path; there is no separate "forgot password" flow.
    """
    # Always try to delete the message first — even on validation errors —
    # because it contains plaintext.
    await _try_delete_message(chat_id, message_id, deps)

    base = deps.user_store.get_user_by_id(user.id)
    if base is None or base.role != "admin":
        await deps.channel.send(
            chat_id,
            "Chi tai khoan admin (khong qua sudo) moi co the dat mat khau.",
            use_markdown=False,
        )
        return

    # Reject if the caller is only admin via elevation — they're not natively-admin.
    if deps.elevation_store.get_active_session("telegram", chat_id) is not None:
        await deps.channel.send(
            chat_id,
            "Lenh nay khong dung khi dang sudo. Hay dung tu tai khoan admin goc.",
            use_markdown=False,
        )
        return

    password = password.strip()
    if len(password) < 8:
        await deps.channel.send(
            chat_id, "Mat khau phai dai it nhat 8 ky tu.", use_markdown=False,
        )
        return

    try:
        deps.user_store.set_password(base.id, password)
    except Exception as e:
        await deps.channel.send(
            chat_id, f"Loi khi dat mat khau: {str(e)[:200]}", use_markdown=False,
        )
        return

    print(f"[audit] password_set user_id={base.id} name={base.name!r}")
    deps.audit.log(
        actor_user_id=base.id,
        action="password_set",
        target_type="user",
        target_id=base.id,
        payload={"name": base.name},
    )
    await deps.channel.send(
        chat_id,
        "Da dat mat khau admin. Tin nhan chua mat khau da bi xoa khoi chat.",
        use_markdown=False,
    )


async def _cmd_dat_web_pass(
    chat_id: str,
    remainder: str,
    user: User,
    deps: CoreDeps,
) -> None:
    """dat web pass: <ten_user>, <mat_khau> — admin sets web password for another user.

    Sets password_hash + must_change_password=1 so the user is forced to choose
    their own password on first web login. Admin-only.
    """
    if not user.is_admin:
        await deps.channel.send(
            chat_id, "Chi admin moi co the dat mat khau web cho nguoi khac.", use_markdown=False,
        )
        return

    parts = remainder.split(",", 1)
    if len(parts) != 2:
        await deps.channel.send(
            chat_id,
            "Cu phap: dat web pass: <ten_user>, <mat_khau>",
            use_markdown=False,
        )
        return

    target_name = parts[0].strip()
    password = parts[1].strip()

    if len(password) < 8:
        await deps.channel.send(
            chat_id, "Mat khau phai co it nhat 8 ky tu.", use_markdown=False,
        )
        return

    target = deps.user_store.find_by_username_or_name(target_name)
    if target is None or not target.is_active:
        await deps.channel.send(
            chat_id, f"Khong tim thay user: {target_name}", use_markdown=False,
        )
        return

    deps.user_store.set_password(target.id, password)
    deps.user_store.set_must_change_password(target.id, True)
    deps.audit.log(
        actor_user_id=user.id,
        action="web_password_set",
        target_type="user",
        target_id=target.id,
        payload={"target_name": target.name, "set_by": user.name},
    )
    await deps.channel.send(
        chat_id,
        f"Da dat mat khau web cho {target.name}. Ho se phai doi mat khau khi dang nhap lan dau.",
        use_markdown=False,
    )


async def _cmd_sudo(
    chat_id: str,
    password: str,
    user: User,
    message_id: int | None,
    deps: CoreDeps,
) -> None:
    """sudo: <mat khau> — elevate role to admin for SUDO_TTL_MINUTES."""
    # Delete the password message immediately, before any validation reply.
    await _try_delete_message(chat_id, message_id, deps)

    base = deps.user_store.get_user_by_id(user.id)
    if base is None:
        return  # Should not happen; webhook layer guarantees registration.

    # Already admin natively — no need to elevate.
    if base.role == "admin":
        await deps.channel.send(
            chat_id,
            "Ban da la admin, khong can sudo.",
            use_markdown=False,
        )
        return

    if base.role != "manager":
        await deps.channel.send(
            chat_id,
            "Chi role manager moi duoc dung sudo.",
            use_markdown=False,
        )
        print(f"[audit] sudo_fail reason=role_not_manager user_id={base.id} role={base.role}")
        deps.audit.log(
            actor_user_id=base.id,
            action="sudo_fail",
            payload={"reason": "role_not_manager", "role": base.role},
        )
        return

    locked, locked_until = deps.elevation_store.is_locked("telegram", chat_id)
    if locked:
        await deps.channel.send(
            chat_id,
            f"Da bi khoa do nhap sai qua nhieu. Thu lai sau (mo khoa luc {locked_until} UTC).",
            use_markdown=False,
        )
        print(f"[audit] sudo_locked user_id={base.id} until={locked_until}")
        deps.audit.log(
            actor_user_id=base.id,
            action="sudo_locked",
            payload={"locked_until": locked_until},
        )
        return

    password = password.strip()
    if not password:
        await deps.channel.send(
            chat_id, "Cu phap: sudo: <mat khau>", use_markdown=False,
        )
        return

    # Verify against any active admin's stored hash.
    admins = [u for u in deps.user_store.list_users() if u.role == "admin"]
    matched_admin = None
    for adm in admins:
        if deps.user_store.check_password(adm.id, password):
            matched_admin = adm
            break

    if matched_admin is None:
        state = deps.elevation_store.record_failure("telegram", chat_id)
        print(
            f"[audit] sudo_fail user_id={base.id} failed_count={state['failed_count']} "
            f"locked_until={state['locked_until']}"
        )
        deps.audit.log(
            actor_user_id=base.id,
            action="sudo_fail",
            payload={
                "reason": "wrong_password",
                "failed_count": state["failed_count"],
                "locked_until": state["locked_until"],
            },
        )
        if state["locked_until"]:
            await deps.channel.send(
                chat_id,
                f"Mat khau sai. Da bi khoa den {state['locked_until']} UTC.",
                use_markdown=False,
            )
        else:
            remaining = config.SUDO_MAX_FAILS - state["failed_count"]
            await deps.channel.send(
                chat_id,
                f"Mat khau sai. Con {remaining} lan thu truoc khi bi khoa.",
                use_markdown=False,
            )
        return

    deps.elevation_store.reset_failures("telegram", chat_id)
    expires_iso = deps.elevation_store.elevate(
        "telegram", chat_id, base_user_id=base.id
    )
    print(
        f"[audit] sudo_elevate user_id={base.id} matched_admin={matched_admin.id} "
        f"expires_at={expires_iso}"
    )
    deps.audit.log(
        actor_user_id=base.id,
        action="sudo_elevate",
        payload={"matched_admin": matched_admin.id, "expires_at": expires_iso},
    )
    await deps.channel.send(
        chat_id,
        f"Da nang quyen admin trong {config.SUDO_TTL_MINUTES} phut. "
        f"Dung 'thoat sudo' de ha quyen som.",
        use_markdown=False,
    )


async def _cmd_thoat_sudo(chat_id: str, user: User, deps: CoreDeps) -> None:
    """thoat sudo — drop the active elevation session, if any."""
    dropped = deps.elevation_store.drop_session("telegram", chat_id)
    if dropped:
        print(f"[audit] sudo_drop user_id={user.id}")
        deps.audit.log(
            actor_user_id=user.id,
            action="sudo_drop",
        )
        await deps.channel.send(
            chat_id, "Da ha quyen admin.", use_markdown=False,
        )
    else:
        await deps.channel.send(
            chat_id, "Ban khong dang trong phien sudo.", use_markdown=False,
        )


async def _cmd_wiki_ingest(chat_id: str, content: str, user: User, deps: CoreDeps) -> None:
    """wiki <content> — ingest raw content into the wiki layer.

    Flow: Claude analyzes → identifies topics → creates/appends wiki pages and
    updates the index.
    """
    if not content:
        await deps.channel.send(
            chat_id, "Cu phap: wiki <noi dung can luu vao wiki>", use_markdown=False,
        )
        return

    await deps.channel.send(
        chat_id, "Dang phan tich va cap nhat wiki...", use_markdown=False,
    )
    try:
        # 1. Existing topic names (lightweight).
        existing_topics = deps.wiki.get_topic_names()

        # 2. Claude returns a list of structured updates.
        updates, tokens = deps.llm.extract_wiki_updates(content, existing_topics)
        record_usage(tokens // 2, tokens // 2)

        if not updates:
            await deps.channel.send(
                chat_id,
                "Khong tim thay thong tin dang ke de luu vao wiki.\n"
                "Thu nhap chi tiet hon: ten nguoi, du an, khai niem cu the.",
                use_markdown=False,
            )
            return

        # 3. Apply each update up to MAX_WIKI_UPDATES.
        results: list[str] = []
        for upd in updates[:MAX_WIKI_UPDATES]:
            topic = upd.get("topic", "").strip()
            topic_type = upd.get("type", "other")
            action = upd.get("action", "create")
            existing_topic_name = upd.get("existing_topic", "").strip()
            content_to_add = upd.get("content_to_add", "").strip()

            if not topic or not content_to_add:
                continue

            try:
                if action == "update" and existing_topic_name:
                    page = deps.wiki.find_page(existing_topic_name)
                    if page:
                        section = deps.wiki.build_section(content_to_add)
                        filename = deps.wiki.append_to_page(page["id"], section)
                        deps.note_index.touch_wiki_page(page["id"])
                        results.append(f"Cap nhat: {filename}")
                    else:
                        # Fall back to create + index update.
                        page_content = deps.wiki.build_new_page(
                            topic, topic_type, content_to_add,
                        )
                        filename, file_id = deps.wiki.save_page(topic, page_content)
                        slug = filename.removesuffix(".md")
                        _register_wiki_page(file_id, user.id, topic, slug, deps)
                        _update_index_after_create(
                            topic, filename, topic_type, content_to_add, deps,
                        )
                        results.append(f"Tao moi: {filename}")
                else:
                    page_content = deps.wiki.build_new_page(
                        topic, topic_type, content_to_add,
                    )
                    filename, file_id = deps.wiki.save_page(topic, page_content)
                    slug = filename.removesuffix(".md")
                    _register_wiki_page(file_id, user.id, topic, slug, deps)
                    _update_index_after_create(
                        topic, filename, topic_type, content_to_add, deps,
                    )
                    results.append(f"Tao moi: {filename}")
            except PermissionError as e:
                results.append(f"Tu choi ({topic}): {str(e)[:100]}")
            except Exception as e:
                traceback.print_exc()
                results.append(f"Loi ({topic}): {str(e)[:100]}")

        if results:
            await deps.channel.send(
                chat_id,
                "Wiki da cap nhat:\n" + "\n".join(f"- {r}" for r in results),
                use_markdown=False,
            )
        else:
            await deps.channel.send(
                chat_id, "Khong co thay doi nao duoc thuc hien.",
                use_markdown=False,
            )

    except Exception as e:
        traceback.print_exc()
        await deps.channel.send(
            chat_id, f"Loi khi ingest wiki: {str(e)[:400]}", use_markdown=False,
        )


async def _cmd_wiki_query(chat_id: str, question: str, user: User, deps: CoreDeps) -> None:
    """hỏi wiki <question> — answer directly from the wiki layer."""
    if not question:
        await deps.channel.send(
            chat_id, "Cu phap: hoi wiki <cau hoi>", use_markdown=False,
        )
        return

    await deps.channel.send(
        chat_id, "Dang tim trong wiki...", use_markdown=False,
    )
    try:
        keywords = [w for w in question.lower().split() if len(w) > 2]
        visible_slugs = deps.note_index.visible_wiki_slugs(user.id)
        wiki_pages = deps.wiki.retrieve_pages(question, keywords, visible_slugs=visible_slugs)

        if not wiki_pages:
            await deps.channel.send(
                chat_id,
                "Khong tim thay trang wiki lien quan.\n"
                "Hay ingest truoc bang lenh: wiki <noi dung>",
                use_markdown=False,
            )
            return

        reply, tokens = deps.llm.answer_from_wiki(question, wiki_pages)
        record_usage(tokens // 2, tokens // 2)
        check_and_alert()

        page_names = ", ".join(p["name"].replace(".md", "") for p in wiki_pages)
        await deps.channel.send(
            chat_id,
            f"[Wiki: {page_names}]\n\n{reply}",
            use_markdown=False,
        )
    except Exception as e:
        traceback.print_exc()
        await deps.channel.send(
            chat_id, f"Loi khi query wiki: {str(e)[:400]}", use_markdown=False,
        )


async def _cmd_xem_wiki_list(chat_id: str, user: User, deps: CoreDeps) -> None:
    """xem wiki — list all wiki pages visible to the user."""
    try:
        pages = deps.wiki.list_pages()
        # ACL: keep only pages the user may read; orphans (no index row) dropped.
        visible = []
        for p in pages:
            meta = deps.note_index.get_wiki_meta(p["id"])
            if meta is None:
                continue
            allowed, is_stealth = acl_mod.can_read(
                user, meta["scope"], meta["owner_user_id"], user_store=deps.user_store,
            )
            if not allowed:
                continue
            if is_stealth:
                deps.audit.log(
                    actor_user_id=user.id,
                    action="stealth_read_wiki",
                    target_type="wiki_page",
                    target_id=meta["drive_file_id"],
                    payload={"owner_user_id": meta["owner_user_id"]},
                )
            visible.append(p)
        if not visible:
            await deps.channel.send(
                chat_id,
                "Wiki chua co trang nao. Hay ingest bang lenh: wiki <noi dung>",
                use_markdown=False,
            )
            return
        lines = [f"Wiki ({len(visible)} trang):"]
        for i, p in enumerate(visible, 1):
            modified = p.get("modifiedTime", "")[:10]
            topic = p["name"].replace(".md", "").replace("_", " ")
            lines.append(f"{i}. {topic}  ({modified})")
        await deps.channel.send(chat_id, "\n".join(lines), use_markdown=False)
    except Exception as e:
        traceback.print_exc()
        await deps.channel.send(chat_id, f"Loi: {str(e)[:400]}", use_markdown=False)


async def _cmd_xem_wiki_page(
    chat_id: str, topic_query: str, user: User, deps: CoreDeps,
) -> None:
    """xem wiki <topic> — read one wiki page (ACL-checked)."""
    if not topic_query:
        await _cmd_xem_wiki_list(chat_id, user, deps)
        return
    not_found_msg = (
        f"Khong tim thay wiki page cho '{topic_query}'.\n"
        f"Xem danh sach: xem wiki"
    )
    try:
        page = deps.wiki.find_page(topic_query)
        if not page:
            await deps.channel.send(chat_id, not_found_msg, use_markdown=False)
            return
        # ACL: an unindexed or unauthorized page returns the same "not found"
        # message so a private page's existence is never leaked.
        meta = deps.note_index.get_wiki_meta(page["id"])
        if meta is None:
            await deps.channel.send(chat_id, not_found_msg, use_markdown=False)
            return
        allowed, is_stealth = acl_mod.can_read(
            user, meta["scope"], meta["owner_user_id"], user_store=deps.user_store,
        )
        if not allowed:
            await deps.channel.send(chat_id, not_found_msg, use_markdown=False)
            return
        if is_stealth:
            deps.audit.log(
                actor_user_id=user.id,
                action="stealth_read_wiki",
                target_type="wiki_page",
                target_id=meta["drive_file_id"],
                payload={"owner_user_id": meta["owner_user_id"]},
            )
        content = page["content"]
        if len(content) > 3500:
            content = content[:3500] + "\n\n[...] (da cat)"
        topic_name = page["name"].replace(".md", "").replace("_", " ")
        await deps.channel.send(
            chat_id,
            f"=== Wiki: {topic_name} ===\n\n{content}",
            use_markdown=False,
        )
    except Exception as e:
        traceback.print_exc()
        await deps.channel.send(chat_id, f"Loi: {str(e)[:400]}", use_markdown=False)


async def _cmd_xem_tri_nho(chat_id: str, user: User, deps: CoreDeps) -> None:
    """xem tri nho — display the user's rolling memory snapshot."""
    content = deps.memory_store.get(user.id, "memory")
    if not content:
        await deps.channel.send(
            chat_id,
            "Bộ nhớ của bạn chưa có gì. Dùng lệnh `cap nhat tri nho` để tạo snapshot đầu tiên.",
            use_markdown=False,
        )
        return
    meta = deps.memory_store.get_meta(user.id, "memory")
    curated = (meta or {}).get("curated_at", "chưa rõ")
    await deps.channel.send(
        chat_id,
        f"=== Bộ nhớ của bạn (cập nhật: {curated}) ===\n\n{content}",
        use_markdown=False,
    )


async def _cmd_xem_ho_so(chat_id: str, user: User, deps: CoreDeps) -> None:
    """xem ho so — display the user's profile snapshot."""
    content = deps.memory_store.get(user.id, "user")
    if not content:
        await deps.channel.send(
            chat_id,
            "Hồ sơ của bạn chưa có gì. Dùng lệnh `cap nhat tri nho` để tạo snapshot đầu tiên.",
            use_markdown=False,
        )
        return
    meta = deps.memory_store.get_meta(user.id, "user")
    curated = (meta or {}).get("curated_at", "chưa rõ")
    await deps.channel.send(
        chat_id,
        f"=== Hồ sơ của bạn (cập nhật: {curated}) ===\n\n{content}",
        use_markdown=False,
    )


async def _cmd_cap_nhat_tri_nho(chat_id: str, user: User, deps: CoreDeps) -> None:
    """cap nhat tri nho — trigger LLM curation to refresh memory + profile snapshots."""
    await deps.channel.send(
        chat_id, "Đang đọc ghi chú gần đây và cập nhật bộ nhớ...", use_markdown=False,
    )
    try:
        # Read user's own recent notes (private to them + everyone-scoped they can see).
        recent = deps.notes.get_recent_notes(days=30, max_results=20)
        recent = _acl_filter_notes(recent, user, deps)

        current_memory = deps.memory_store.get(user.id, "memory")
        current_profile = deps.memory_store.get(user.id, "user")

        new_memory, new_profile, tokens = deps.llm.curate_memory(
            recent, current_memory, current_profile,
        )
        record_usage(tokens // 2, tokens // 2)
        deps.user_store.record_usage(user.id, tokens)

        saved: list[str] = []
        if new_memory:
            deps.memory_store.set(user.id, "memory", new_memory, mark_curated=True)
            saved.append("bộ nhớ")
        if new_profile:
            deps.memory_store.set(user.id, "user", new_profile, mark_curated=True)
            saved.append("hồ sơ")

        if not saved:
            await deps.channel.send(
                chat_id,
                "Curation không sinh ra nội dung nào. Thử lại sau hoặc thêm ghi chú trước.",
                use_markdown=False,
            )
            return

        await deps.channel.send(
            chat_id,
            f"Đã cập nhật {' và '.join(saved)} từ {len(recent)} ghi chú gần đây.\n"
            f"Dùng `xem tri nho` hoặc `xem ho so` để xem.",
            use_markdown=False,
        )
    except Exception as e:
        traceback.print_exc()
        await deps.channel.send(chat_id, f"Lỗi khi cập nhật bộ nhớ: {str(e)[:400]}", use_markdown=False)


async def _cmd_tom_tat_tuan(chat_id: str, user: User, deps: CoreDeps) -> None:
    week_range = current_week_range_str()
    await deps.channel.send(
        chat_id,
        f"Dang doc ghi chu tuan nay ({week_range})...",
        use_markdown=False,
    )
    try:
        notes = deps.notes.get_current_week_notes(max_results=20)
        notes = _acl_filter_notes(notes, user, deps)
        if not notes:
            await deps.channel.send(
                chat_id,
                f"Khong co ghi chu nao trong tuan nay ({week_range}).",
                use_markdown=False,
            )
            return
        summary, tokens = deps.llm.summarize_notes(notes)
        record_usage(tokens // 2, tokens // 2)
        check_and_alert()
        await deps.channel.send(
            chat_id,
            f"Tom tat tuan nay ({week_range}) — {len(notes)} ghi chu:\n\n{summary}",
            use_markdown=False,
        )
    except Exception as e:
        traceback.print_exc()
        await deps.channel.send(chat_id, f"Loi: {str(e)[:500]}", use_markdown=False)


async def _handle_general_question(
    chat_id: str, text: str, deps: CoreDeps, user: User | None = None,
) -> None:
    """Free-form question fallback — smart search + Claude answer.

    Pipeline:
      1. extract_search_intent(text) → keywords + days_back + needs_search
      2. If needs_search: retrieve wiki pages + smart_search raw notes
      3. ask Claude with the combined context
    """
    await deps.channel.send(chat_id, "Dang xu ly...")
    try:
        # Step 1: intent extraction.
        intent, intent_tokens = deps.llm.extract_search_intent(text)
        record_usage(intent_tokens // 2, intent_tokens // 2)

        context_parts: list[str] = []

        # Step 2: wiki pages via index (ACL-filtered).
        if intent.get("needs_search"):
            try:
                visible_slugs = (
                    deps.note_index.visible_wiki_slugs(user.id)
                    if user is not None else None
                )
                wiki_pages = deps.wiki.retrieve_pages(
                    text, intent.get("keywords", []), visible_slugs=visible_slugs,
                )
                if wiki_pages:
                    wiki_block = "\n\n".join(
                        f"[Wiki: {p['name'].replace('.md', '')}]\n{p['content']}"
                        for p in wiki_pages
                    )
                    context_parts.append(wiki_block)
            except Exception as e:
                print(f"[core] Wiki search error (non-fatal): {e}")

        # Step 3: smart search raw notes (ACL-filtered).
        if intent.get("needs_search") and intent.get("keywords"):
            try:
                notes = deps.notes.smart_search(
                    keywords=intent["keywords"],
                    days_back=intent.get("days_back", 0) or 0,
                )
                if user is not None:
                    notes = _acl_filter_notes(notes, user, deps)
                if notes:
                    notes_block = "\n\n".join(
                        [f"[{n['name']}]\n{n['content']}" for n in notes[:5]]
                    )
                    context_parts.append(notes_block)
            except Exception as e:
                print(f"[core] Smart search error: {e}")
                try:
                    fallback_notes = deps.notes.search_notes(
                        intent["keywords"][0], max_results=2,
                    )
                    if user is not None:
                        fallback_notes = _acl_filter_notes(fallback_notes, user, deps)
                    if fallback_notes:
                        context_parts.append("\n\n".join(
                            [f"[{n['name']}]\n{n['content']}" for n in fallback_notes]
                        ))
                except Exception:
                    pass

        notes_context = "\n\n".join(context_parts)

        # Step 4: prepend L1 memory snapshot (if any) so Claude knows the user.
        if user is not None:
            memory_content = deps.memory_store.get(user.id, "memory")
            if memory_content:
                memory_block = f"[Bộ nhớ cá nhân]\n{memory_content}"
                notes_context = (
                    memory_block + ("\n\n" + notes_context if notes_context else "")
                )

        # Step 5: ask Claude.
        reply, tokens = deps.llm.ask(text, notes_context)
        record_usage(tokens // 2, tokens // 2)
        check_and_alert()
        if user is not None:
            deps.user_store.record_usage(user.id, tokens)
        await deps.channel.send(chat_id, reply, use_markdown=False)
    except Exception as e:
        traceback.print_exc()
        await deps.channel.send(chat_id, f"Loi: {str(e)[:500]}", use_markdown=False)


def _is_over_quota(user: User, deps: CoreDeps) -> bool:
    """Return True if the user has exceeded their monthly token quota."""
    if has_role(user, "admin"):
        return False
    quota = deps.user_store.get_quota(user.id)
    if quota is None or quota["monthly_token_limit"] == 0:
        return False
    return quota["used_tokens"] >= quota["monthly_token_limit"]


# ═══════════════════════════════════════════════════════════════════════════════
# Backup / export commands (FR-6)
# ═══════════════════════════════════════════════════════════════════════════════

def _export_zip_filename(user_name: str) -> str:
    from datetime import datetime
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in user_name)
    return f"export_{safe}_{ts}.zip"


async def _cmd_xuat_du_lieu_self(chat_id: str, user: "User", deps: CoreDeps) -> None:
    """xuat du lieu — export caller's own data; upload ZIP to Drive; reply with link."""
    if deps.backup_engine is None:
        await deps.channel.send(chat_id, "Tinh nang backup chua duoc cau hinh.", use_markdown=False)
        return

    remaining = deps.backup_engine.export_cooldown_remaining(user.id)
    if remaining > 0:
        await deps.channel.send(
            chat_id,
            f"Vui long doi {remaining} giay truoc khi export lan tiep theo.",
            use_markdown=False,
        )
        return

    await deps.channel.send(chat_id, "Dang tao backup, vui long cho...", use_markdown=False)
    try:
        zip_bytes, manifest = deps.backup_engine.generate_export(user.id)
        filename = _export_zip_filename(user.name)
        _file_id, link = deps.backup_engine.upload_to_drive(filename, zip_bytes)
    except Exception as exc:
        await deps.channel.send(
            chat_id,
            f"Export that bai: {str(exc)[:300]}",
            use_markdown=False,
        )
        return

    stats = manifest.get("stats", {})
    await deps.channel.send(
        chat_id,
        f"Da tao backup thanh cong!\n"
        f"  Ghi chu: {stats.get('notes', 0)}\n"
        f"  Wiki: {stats.get('wiki_pages', 0)}\n"
        f"  Cuoc tro chuyen: {stats.get('web_conversations', 0)}\n"
        f"Link: {link}",
        use_markdown=False,
    )


async def _cmd_xuat_du_lieu_admin(
    chat_id: str, remainder: str, user: "User", deps: CoreDeps,
) -> None:
    """xuat du lieu: <ten> — admin exports data for a named user; uploads to Drive."""
    if not user.is_admin:
        await deps.channel.send(chat_id, "Chi admin moi co the xuat du lieu cho nguoi khac.", use_markdown=False)
        return

    if deps.backup_engine is None:
        await deps.channel.send(chat_id, "Tinh nang backup chua duoc cau hinh.", use_markdown=False)
        return

    target_name = remainder.strip()
    if not target_name:
        await deps.channel.send(chat_id, "Cu phap: xuat du lieu: <ten>", use_markdown=False)
        return

    target = deps.user_store.find_by_username_or_name(target_name)
    if target is None or not target.is_active:
        await deps.channel.send(
            chat_id, f"Khong tim thay user: {target_name}", use_markdown=False,
        )
        return

    remaining = deps.backup_engine.export_cooldown_remaining(target.id)
    if remaining > 0:
        await deps.channel.send(
            chat_id,
            f"Rate limit: doi {remaining} giay (cooldown cua user {target.name}).",
            use_markdown=False,
        )
        return

    await deps.channel.send(
        chat_id, f"Dang tao backup cho {target.name}, vui long cho...", use_markdown=False,
    )
    try:
        zip_bytes, manifest = deps.backup_engine.generate_export(target.id)
        # Override audit delivery field logged by generate_export.
        deps.audit.log(
            actor_user_id=user.id,
            action="data_export",
            target_type="user",
            target_id=target.id,
            payload={"size_bytes": len(zip_bytes), "delivery": "telegram_drive"},
        )
        filename = _export_zip_filename(target.name)
        _file_id, link = deps.backup_engine.upload_to_drive(filename, zip_bytes)
    except Exception as exc:
        await deps.channel.send(
            chat_id,
            f"Export that bai: {str(exc)[:300]}",
            use_markdown=False,
        )
        return

    stats = manifest.get("stats", {})
    await deps.channel.send(
        chat_id,
        f"Da tao backup cho {target.name}!\n"
        f"  Ghi chu: {stats.get('notes', 0)}\n"
        f"  Wiki: {stats.get('wiki_pages', 0)}\n"
        f"  Cuoc tro chuyen: {stats.get('web_conversations', 0)}\n"
        f"Link: {link}",
        use_markdown=False,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Main dispatcher
# ═══════════════════════════════════════════════════════════════════════════════

# Command table — {command_id: [prefix, ...]}
# Longer prefixes must be listed before shorter ones within the same command
# so longest-prefix-first in match_command resolves ambiguity correctly.
# Names that map directly to Vietnamese user input stay Vietnamese per CLAUDE.md.
_COMMAND_TABLE: dict[str, list[str]] = {
    "THEM_USER":          ["thêm user: ", "them user: ", "add user: "],
    "XEM_DANH_SACH_USER": ["xem danh sach user", "xem danh sách user", "list users"],
    "XOA_USER":           ["xoa user: ", "xóa user: ", "delete user: "],
    "DOI_ROLE":           ["doi role: ", "đổi role: ", "change role: "],
    "DAT_BIRTHDATE":      ["dat birthdate: ", "đặt birthdate: ", "set birthdate: "],
    "DUYET_BIRTHDATE":    ["duyet birthdate", "duyệt birthdate", "approve birthdate"],
    "DAT_USERNAME":       ["dat username: ", "đặt username: ", "set username: "],
    "DUYET_USERNAME":     ["duyet username", "duyệt username", "approve username"],
    "DAT_CHA":            ["dat cha: ", "đặt cha: ", "set parent: "],
    "XEM_CHA":            ["xem cha: ", "view parent: "],
    "XEM_QUOTA":          ["xem quota", "view quota"],
    "DAT_QUOTA":          ["dat quota: ", "đặt quota: ", "set quota: "],
    "RESET_QUOTA":        ["reset quota: "],
    "XEM_AUDIT":          ["xem audit"],
    "XEM_THUNG_RAC":      ["xem thung rac", "xem thùng rác"],
    "KHOI_PHUC":          ["khoi phuc: ", "khôi phục: "],
    "XOA_HAN":            ["xoa han: ", "xóa hẳn: "],
    "BO_CHIA_SE_FILE":    ["bỏ chia sẻ ", "bo chia se "],
    "CHIA_SE_FILE":       ["chia sẻ ", "chia se "],
    "HOI_WIKI":           ["hỏi wiki ", "hoi wiki ", "ask wiki "],
    "XEM_WIKI_PAGE":      ["xem wiki "],
    "XEM_WIKI":           ["xem wiki"],
    "WIKI":               ["wiki "],
    "GHI_NHO_VAO":        ["ghi nhớ vào ", "ghi nho vao "],
    "GHI_NHO":            ["ghi nhớ ", "ghi nho "],
    "NHAT_KY":            ["nhật ký ", "nhat ky "],
    "XEM_NHAT_KY":        ["xem nhật ký", "xem nhat ky"],
    "XEM_TRI_NHO":        ["xem trí nhớ", "xem tri nho"],
    "XEM_HO_SO":          ["xem hồ sơ", "xem ho so"],
    "XEM_SCOPE":          ["xem scope "],
    "TOI_LA_AI":          ["toi la ai", "tôi là ai", "tai khoan", "tài khoản", "who am i", "whoami"],
    "DAT_MAT_KHAU":       ["dat mat khau: ", "đặt mật khẩu: ", "set password: "],
    "DAT_WEB_PASS":       ["dat web pass: ", "đặt web pass: "],
    "THOAT_SUDO":         ["thoat sudo", "thoát sudo", "exit sudo"],
    "SUDO":               ["sudo: "],
    "CAP_NHAT_TRI_NHO":   ["cập nhật trí nhớ", "cap nhat tri nho"],
    "LIET_KE":            ["liệt kê", "liet ke"],
    "TIM":                ["tìm ", "tim ", "search "],
    "XEM":                ["xem "],
    "TOM_TAT_TUAN":       ["tóm tắt tuần này", "tom tat tuan nay", "tóm tắt tuần", "tom tat tuan"],
    # Longer prefix first: "xuat du lieu: <ten>" must match before "xuat du lieu"
    "XUAT_DU_LIEU_ADMIN": ["xuat du lieu: ", "xuất dữ liệu: "],
    "XUAT_DU_LIEU_SELF":  ["xuat du lieu", "xuất dữ liệu"],
}


async def handle_message(msg: ChannelMessage, user: User, deps: CoreDeps) -> None:
    """Dispatch a normalized inbound message to the appropriate handler."""
    chat_id = msg.chat_id
    text = msg.text.strip()
    if not text:
        return

    # ── Step 1: try to resolve a pending state first ────────────────────────
    if await _try_resolve_pending(chat_id, text, user, deps):
        return

    # ── Step 2: slash commands (not normalized) ────────────────────────────
    if text == "/start":
        await _cmd_start(chat_id, deps); return
    if text.startswith("/help"):
        group = text[len("/help"):].strip()
        await _cmd_help(chat_id, group, deps); return
    if text == "/cost":
        await _cmd_cost(chat_id, deps); return
    if text == "/test":
        await _cmd_test(chat_id, deps); return
    if text == "/security":
        await _cmd_security(chat_id, deps); return

    # ── Step 2.5: quota enforcement for LLM-heavy operations ──────────────────
    # Non-LLM commands (user management, quota admin) bypass this check.
    _QUOTA_EXEMPT = {
        "THEM_USER", "XEM_DANH_SACH_USER", "XOA_USER", "DOI_ROLE",
        "DAT_BIRTHDATE", "DUYET_BIRTHDATE",
        "DAT_USERNAME", "DUYET_USERNAME",
        "DAT_CHA", "XEM_CHA",
        "XEM_QUOTA", "DAT_QUOTA", "RESET_QUOTA",
        "XEM_AUDIT",
        "XEM_THUNG_RAC", "KHOI_PHUC", "XOA_HAN",
        "CHIA_SE_FILE", "BO_CHIA_SE_FILE",
        "XEM_TRI_NHO", "XEM_HO_SO",
        "DAT_MAT_KHAU", "SUDO", "THOAT_SUDO",
        "XUAT_DU_LIEU_SELF", "XUAT_DU_LIEU_ADMIN",
    }
    _matched = match_command(text, _COMMAND_TABLE)
    if _matched is None or _matched[0] not in _QUOTA_EXEMPT:
        if _is_over_quota(user, deps):
            quota = deps.user_store.get_quota(user.id)
            await deps.channel.send(
                chat_id,
                f"Bạn đã dùng hết quota tháng này ({quota['used_tokens']:,}/{quota['monthly_token_limit']:,} tokens). "
                "Liên hệ admin để được reset hoặc tăng giới hạn.",
                use_markdown=False,
            )
            return

    # ── Step 3: prefix-based dispatch (longest-prefix-first, diacritic-agnostic) ──
    result = match_command(text, _COMMAND_TABLE)
    if result:
        cmd_id, remainder = result

        if cmd_id == "THEM_USER":
            await _cmd_them_user(chat_id, remainder, user, deps); return
        if cmd_id == "XEM_DANH_SACH_USER":
            await _cmd_xem_danh_sach_user(chat_id, user, deps); return
        if cmd_id == "XOA_USER":
            await _cmd_xoa_user(chat_id, remainder, user, deps); return
        if cmd_id == "DOI_ROLE":
            await _cmd_doi_role(chat_id, remainder, user, deps); return
        if cmd_id == "DAT_BIRTHDATE":
            await _cmd_dat_birthdate(chat_id, remainder, user, deps); return
        if cmd_id == "DUYET_BIRTHDATE":
            await _cmd_duyet_birthdate(chat_id, remainder, user, deps); return
        if cmd_id == "DAT_USERNAME":
            await _cmd_dat_username(chat_id, remainder, user, deps); return
        if cmd_id == "DUYET_USERNAME":
            await _cmd_duyet_username(chat_id, remainder, user, deps); return
        if cmd_id == "DAT_CHA":
            await _cmd_dat_cha(chat_id, remainder, user, deps); return
        if cmd_id == "XEM_CHA":
            await _cmd_xem_cha(chat_id, remainder, user, deps); return
        if cmd_id == "XEM_QUOTA":
            await _cmd_xem_quota(chat_id, remainder, user, deps); return
        if cmd_id == "DAT_QUOTA":
            await _cmd_dat_quota(chat_id, remainder, user, deps); return
        if cmd_id == "RESET_QUOTA":
            await _cmd_reset_quota(chat_id, remainder, user, deps); return
        if cmd_id == "XEM_AUDIT":
            await _cmd_xem_audit(chat_id, remainder, user, deps); return
        if cmd_id == "XEM_THUNG_RAC":
            await _cmd_xem_thung_rac(chat_id, user, deps); return
        if cmd_id == "KHOI_PHUC":
            await _cmd_khoi_phuc(chat_id, remainder, user, deps); return
        if cmd_id == "XOA_HAN":
            await _cmd_xoa_han(chat_id, remainder, user, deps); return
        if cmd_id == "CHIA_SE_FILE":
            await _cmd_chia_se(chat_id, remainder, user, deps); return
        if cmd_id == "BO_CHIA_SE_FILE":
            await _cmd_bo_chia_se(chat_id, remainder, user, deps); return
        if cmd_id == "HOI_WIKI":
            await _cmd_wiki_query(chat_id, remainder, user, deps); return
        if cmd_id == "XEM_WIKI_PAGE":
            await _cmd_xem_wiki_page(chat_id, remainder, user, deps); return
        if cmd_id == "XEM_WIKI":
            await _cmd_xem_wiki_list(chat_id, user, deps); return
        if cmd_id == "WIKI":
            await _cmd_wiki_ingest(chat_id, remainder, user, deps); return
        if cmd_id == "GHI_NHO_VAO":
            await _cmd_ghi_nho_vao(chat_id, remainder, deps); return
        if cmd_id == "GHI_NHO":
            await _cmd_ghi_nho(chat_id, remainder, user, deps); return
        if cmd_id == "NHAT_KY":
            await _cmd_nhat_ky(chat_id, remainder, user, deps); return
        if cmd_id == "XEM_NHAT_KY":
            await _cmd_xem_nhat_ky(chat_id, deps); return
        if cmd_id == "LIET_KE":
            await _cmd_liet_ke(chat_id, remainder, user, deps); return
        if cmd_id == "TIM":
            await _cmd_tim(chat_id, remainder, user, deps); return
        if cmd_id == "XEM_SCOPE":
            await _cmd_xem_scope(chat_id, remainder, user, deps); return
        if cmd_id == "XEM":
            await _cmd_xem(chat_id, remainder, user, deps); return
        if cmd_id == "TOI_LA_AI":
            await _cmd_whoami(chat_id, user, deps); return
        if cmd_id == "DAT_MAT_KHAU":
            message_id = msg.raw.get("message_id") if msg.raw else None
            await _cmd_dat_mat_khau(chat_id, remainder, user, message_id, deps); return
        if cmd_id == "DAT_WEB_PASS":
            await _cmd_dat_web_pass(chat_id, remainder, user, deps); return
        if cmd_id == "SUDO":
            message_id = msg.raw.get("message_id") if msg.raw else None
            await _cmd_sudo(chat_id, remainder, user, message_id, deps); return
        if cmd_id == "THOAT_SUDO":
            await _cmd_thoat_sudo(chat_id, user, deps); return
        if cmd_id == "XEM_TRI_NHO":
            await _cmd_xem_tri_nho(chat_id, user, deps); return
        if cmd_id == "XEM_HO_SO":
            await _cmd_xem_ho_so(chat_id, user, deps); return
        if cmd_id == "CAP_NHAT_TRI_NHO":
            await _cmd_cap_nhat_tri_nho(chat_id, user, deps); return
        if cmd_id == "TOM_TAT_TUAN":
            await _cmd_tom_tat_tuan(chat_id, user, deps); return
        if cmd_id == "XUAT_DU_LIEU_ADMIN":
            await _cmd_xuat_du_lieu_admin(chat_id, remainder, user, deps); return
        if cmd_id == "XUAT_DU_LIEU_SELF":
            await _cmd_xuat_du_lieu_self(chat_id, user, deps); return

    # ── Step 4: free-form question → wiki + smart search + Claude ──────────
    await _handle_general_question(chat_id, text, deps, user=user)
