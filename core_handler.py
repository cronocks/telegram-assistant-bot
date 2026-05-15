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
from dataclasses import dataclass

from config import (
    FUZZY_SHOW_LIMIT,
    MAX_WIKI_UPDATES,
    PENDING_CHOICE_TIMEOUT_SEC,
)
from cost_monitor import check_and_alert, get_current_cost, record_usage
from interfaces import ChannelAdapter, ChannelMessage, LLMClient, NoteStore, User, UserStore, WikiStore
from permissions import can_manage, has_role
from text_utils import match_command, normalize_vn, validate_username
from security import get_security_status
from timeutils import current_week_range_str, time_str, today_str


# ═══════════════════════════════════════════════════════════════════════════════
# CoreDeps — dependency bundle injected by main.py
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class CoreDeps:
    """Bundle of adapter instances the core handler depends on."""
    llm: LLMClient
    notes: NoteStore
    wiki: WikiStore
    channel: ChannelAdapter
    user_store: UserStore


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
    chat_id: str, pending: dict, text: str, deps: CoreDeps,
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
            saved_name = deps.notes.save_note(
                title=filename,
                content=content,
                custom_filename=_sanitize_filename(filename),
            )
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
    chat_id: str, text: str, deps: CoreDeps,
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
        return await _resolve_create_new_confirm(chat_id, pending, text, deps)

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
            f"Đã tạo user *{name}* (role: {role}, id: {new_user.id}).\n\n"
            f"Mã mời (hết hạn sau 7 ngày):\n`{code}`\n\n"
            f"Gửi mã này cho {name}, họ dùng lệnh:\n`dang ky: {code}`",
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


async def _cmd_xoa_user(
    chat_id: str, body: str, user: User, deps: CoreDeps,
) -> None:
    """xoa user: <id> — admin soft-deletes a user."""
    if not has_role(user, "admin"):
        await deps.channel.send(chat_id, "Chỉ admin mới có thể xóa user.", use_markdown=False)
        return

    if not body.strip().isdigit():
        await deps.channel.send(
            chat_id, "Cú pháp: xoa user: <id>\nVí dụ: xoa user: 3",
            use_markdown=False,
        )
        return

    target_id = int(body.strip())
    if target_id == user.id:
        await deps.channel.send(
            chat_id, "Không thể tự xóa tài khoản của mình.", use_markdown=False,
        )
        return

    try:
        target = deps.user_store.get_user_by_id(target_id)
        if target is None or not target.is_active:
            await deps.channel.send(
                chat_id, f"Không tìm thấy user id={target_id}.", use_markdown=False,
            )
            return
        deps.user_store.soft_delete_user(target_id)
        await deps.channel.send(
            chat_id, f"Đã vô hiệu hóa user: {target.name} (id={target_id}).",
            use_markdown=False,
        )
    except Exception as e:
        await deps.channel.send(chat_id, f"Lỗi: {str(e)[:400]}", use_markdown=False)


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
    """dat cha: <user_id> <parent_id> — admin/manager sets a parent for a user."""
    if not has_role(user, "admin", "manager"):
        await deps.channel.send(chat_id, "Chỉ admin/manager mới có thể đặt quan hệ cha-con.", use_markdown=False)
        return

    parts = body.strip().split()
    if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
        await deps.channel.send(
            chat_id,
            "Cú pháp: dat cha: <user_id> <parent_id>\n"
            "Ví dụ: dat cha: 3 1 (user 3 có cha là user 1)\n"
            "Dùng: dat cha: <user_id> 0 để xóa quan hệ cha-con.",
            use_markdown=False,
        )
        return

    user_id = int(parts[0])
    parent_id = int(parts[1])

    try:
        if parent_id == 0:
            removed = deps.user_store.remove_parent(user_id, user.id)
            if removed:
                await deps.channel.send(chat_id, f"Đã xóa quan hệ cha-con của user #{user_id}.", use_markdown=False)
            else:
                await deps.channel.send(chat_id, f"User #{user_id} không có quan hệ cha-con nào đang hoạt động.", use_markdown=False)
        else:
            deps.user_store.set_parent(user_id, parent_id, user.id)
            child = deps.user_store.get_user_by_id(user_id)
            parent = deps.user_store.get_user_by_id(parent_id)
            child_name = child.name if child else f"#{user_id}"
            parent_name = parent.name if parent else f"#{parent_id}"
            await deps.channel.send(
                chat_id,
                f"Đã đặt {child_name} (#{user_id}) có cha là {parent_name} (#{parent_id}).",
                use_markdown=False,
            )
    except ValueError as e:
        await deps.channel.send(chat_id, str(e), use_markdown=False)


async def _cmd_xem_cha(
    chat_id: str, body: str, user: User, deps: CoreDeps,
) -> None:
    """xem cha: <user_id> — show parent and children of a user."""
    if not has_role(user, "admin", "manager"):
        await deps.channel.send(chat_id, "Chỉ admin/manager mới có thể xem quan hệ cha-con.", use_markdown=False)
        return

    parts = body.strip().split()
    if not parts or not parts[0].isdigit():
        await deps.channel.send(
            chat_id, "Cú pháp: xem cha: <user_id>", use_markdown=False,
        )
        return

    target_id = int(parts[0])
    target = deps.user_store.get_user_by_id(target_id)
    if target is None:
        await deps.channel.send(chat_id, f"Không tìm thấy user #{target_id}.", use_markdown=False)
        return

    lines = [f"Quan hệ của {target.name} (#{target_id}):"]

    parent = deps.user_store.get_parent(target_id)
    if parent:
        lines.append(f"• Cha: {parent.name} (#{parent.id})")
    else:
        lines.append("• Cha: (chưa có)")

    children = deps.user_store.get_children(target_id)
    if children:
        child_list = ", ".join(f"{c.name} (#{c.id})" for c in children)
        lines.append(f"• Con: {child_list}")
    else:
        lines.append("• Con: (chưa có)")

    await deps.channel.send(chat_id, "\n".join(lines), use_markdown=False)


async def _cmd_xem_quota(
    chat_id: str, body: str, user: User, deps: CoreDeps,
) -> None:
    """xem quota [user_id] — show token quota. Admin can view any user; others see own."""
    target_id = user.id
    if body.strip() and has_role(user, "admin", "manager"):
        parts = body.strip().split()
        if parts[0].isdigit():
            target_id = int(parts[0])

    target = deps.user_store.get_user_by_id(target_id)
    if target is None:
        await deps.channel.send(chat_id, f"Không tìm thấy user #{target_id}.", use_markdown=False)
        return

    quota = deps.user_store.get_quota(target_id)
    if quota is None or quota["monthly_token_limit"] == 0:
        limit_str = "không giới hạn"
    else:
        limit_str = f"{quota['monthly_token_limit']:,} tokens/tháng"

    used = quota["used_tokens"] if quota else 0
    month = quota["month"] if quota else "N/A"

    lines = [
        f"Quota của {target.name} (#{target_id}):",
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
    """dat quota: <user_id> <tokens> — admin sets monthly token limit (0 = unlimited)."""
    if not has_role(user, "admin"):
        await deps.channel.send(chat_id, "Chỉ admin mới có thể đặt quota.", use_markdown=False)
        return

    parts = body.strip().split()
    if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
        await deps.channel.send(
            chat_id,
            "Cú pháp: dat quota: <user_id> <tokens>\n"
            "Ví dụ: dat quota: 3 100000 (100k tokens/tháng)\n"
            "Dùng 0 để bỏ giới hạn.",
            use_markdown=False,
        )
        return

    target_id = int(parts[0])
    limit = int(parts[1])

    target = deps.user_store.get_user_by_id(target_id)
    if target is None:
        await deps.channel.send(chat_id, f"Không tìm thấy user #{target_id}.", use_markdown=False)
        return

    deps.user_store.set_quota(target_id, limit)
    if limit == 0:
        await deps.channel.send(chat_id, f"Đã bỏ giới hạn quota cho {target.name} (#{target_id}).", use_markdown=False)
    else:
        await deps.channel.send(
            chat_id,
            f"Đã đặt quota cho {target.name} (#{target_id}): {limit:,} tokens/tháng.",
            use_markdown=False,
        )


async def _cmd_reset_quota(
    chat_id: str, body: str, user: User, deps: CoreDeps,
) -> None:
    """reset quota: <user_id> — admin resets a user's current-month usage to 0."""
    if not has_role(user, "admin"):
        await deps.channel.send(chat_id, "Chỉ admin mới có thể reset quota.", use_markdown=False)
        return

    parts = body.strip().split()
    if not parts or not parts[0].isdigit():
        await deps.channel.send(chat_id, "Cú pháp: reset quota: <user_id>", use_markdown=False)
        return

    target_id = int(parts[0])
    target = deps.user_store.get_user_by_id(target_id)
    if target is None:
        await deps.channel.send(chat_id, f"Không tìm thấy user #{target_id}.", use_markdown=False)
        return

    deps.user_store.reset_usage(target_id)
    await deps.channel.send(chat_id, f"Đã reset usage của {target.name} (#{target_id}).", use_markdown=False)


async def _cmd_start(chat_id: str, deps: CoreDeps) -> None:
    await deps.channel.send(chat_id, (
        "Xin chao! Toi la Claude Bot.\n\n"
        "*LENH GHI CHU:*\n"
        "`ghi nho [noi dung]` — Tao file moi (Claude tu dat ten)\n"
        "`ghi nho vao [ten]: [noi dung]` — Them vao file co san (fuzzy match)\n"
        "`nhat ky [noi dung]` — Them vao file nhat ky hom nay (GMT+7)\n\n"
        "*LENH WIKI (LLM Wiki):*\n"
        "`wiki [noi dung]` — Ingest vao wiki (Claude tu to chuc theo topic)\n"
        "`hoi wiki [cau hoi]` — Hoi truc tiep tu wiki\n"
        "`xem wiki` — Liet ke tat ca wiki pages\n"
        "`xem wiki [topic]` — Doc 1 wiki page\n\n"
        "*LENH XEM:*\n"
        "`xem nhat ky` — Doc nhat ky hom nay\n"
        "`xem [ten]` — Doc 1 file (fuzzy match)\n"
        "`liet ke` — Liet ke 10 file gan nhat\n"
        "`tim [tu khoa]` — Tim trong noi dung file\n"
        "`tom tat tuan nay` — Tom tat ghi chu 7 ngay\n\n"
        "*LENH HE THONG:*\n"
        "`/cost` — Chi phi thang\n"
        "`/test` — Kiem tra Drive\n"
        "`/security` — Cau hinh bao mat\n\n"
        "*HOI DAP:*\n"
        "Cau hoi tu nhien — bot tim wiki + vault roi tra loi"
    ))


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


async def _cmd_ghi_nho(chat_id: str, content: str, deps: CoreDeps) -> None:
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
        filename = deps.notes.save_note(title.strip(), content)
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


async def _cmd_nhat_ky(chat_id: str, content: str, deps: CoreDeps) -> None:
    """nhật ký <content> → append to today's journal."""
    if not content:
        await deps.channel.send(chat_id, "Vui long nhap noi dung.", use_markdown=False)
        return

    await deps.channel.send(chat_id, "Dang ghi nhat ky...")
    try:
        filename, action = deps.notes.add_to_daily_journal(content)
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


async def _cmd_xem(chat_id: str, name_query: str, deps: CoreDeps) -> None:
    """xem <name> → read a file (fuzzy match)."""
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


async def _cmd_liet_ke(chat_id: str, deps: CoreDeps) -> None:
    """liệt kê → list 10 most recent files."""
    try:
        files = deps.notes.list_recent_files()
        if not files:
            await deps.channel.send(
                chat_id, "Vault trong, chua co ghi chu nao.", use_markdown=False,
            )
            return
        msg_lines = ["10 file gan nhat:"]
        for i, f in enumerate(files, 1):
            modified = f.get("modifiedTime", "")[:10]
            msg_lines.append(f"{i}. {f['name']}  ({modified})")
        await deps.channel.send(chat_id, "\n".join(msg_lines), use_markdown=False)
    except Exception as e:
        traceback.print_exc()
        await deps.channel.send(chat_id, f"Loi: {str(e)[:500]}", use_markdown=False)


async def _cmd_tim(chat_id: str, keyword: str, deps: CoreDeps) -> None:
    if not keyword:
        await deps.channel.send(chat_id, "Vui long nhap tu khoa.")
        return
    await deps.channel.send(
        chat_id, f"Dang tim '{keyword}'...", use_markdown=False,
    )
    try:
        notes = deps.notes.search_notes(keyword)
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


async def _cmd_wiki_ingest(chat_id: str, content: str, deps: CoreDeps) -> None:
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
                        results.append(f"Cap nhat: {filename}")
                    else:
                        # Fall back to create + index update.
                        page_content = deps.wiki.build_new_page(
                            topic, topic_type, content_to_add,
                        )
                        filename = deps.wiki.save_page(topic, page_content)
                        _update_index_after_create(
                            topic, filename, topic_type, content_to_add, deps,
                        )
                        results.append(f"Tao moi: {filename}")
                else:
                    page_content = deps.wiki.build_new_page(
                        topic, topic_type, content_to_add,
                    )
                    filename = deps.wiki.save_page(topic, page_content)
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


async def _cmd_wiki_query(chat_id: str, question: str, deps: CoreDeps) -> None:
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
        wiki_pages = deps.wiki.retrieve_pages(question, keywords)

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


async def _cmd_xem_wiki_list(chat_id: str, deps: CoreDeps) -> None:
    """xem wiki — list all wiki pages."""
    try:
        pages = deps.wiki.list_pages()
        if not pages:
            await deps.channel.send(
                chat_id,
                "Wiki chua co trang nao. Hay ingest bang lenh: wiki <noi dung>",
                use_markdown=False,
            )
            return
        lines = [f"Wiki ({len(pages)} trang):"]
        for i, p in enumerate(pages, 1):
            modified = p.get("modifiedTime", "")[:10]
            topic = p["name"].replace(".md", "").replace("_", " ")
            lines.append(f"{i}. {topic}  ({modified})")
        await deps.channel.send(chat_id, "\n".join(lines), use_markdown=False)
    except Exception as e:
        traceback.print_exc()
        await deps.channel.send(chat_id, f"Loi: {str(e)[:400]}", use_markdown=False)


async def _cmd_xem_wiki_page(
    chat_id: str, topic_query: str, deps: CoreDeps,
) -> None:
    """xem wiki <topic> — read one wiki page."""
    if not topic_query:
        await _cmd_xem_wiki_list(chat_id, deps)
        return
    try:
        page = deps.wiki.find_page(topic_query)
        if not page:
            await deps.channel.send(
                chat_id,
                f"Khong tim thay wiki page cho '{topic_query}'.\n"
                f"Xem danh sach: xem wiki",
                use_markdown=False,
            )
            return
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


async def _cmd_tom_tat_tuan(chat_id: str, deps: CoreDeps) -> None:
    week_range = current_week_range_str()
    await deps.channel.send(
        chat_id,
        f"Dang doc ghi chu tuan nay ({week_range})...",
        use_markdown=False,
    )
    try:
        notes = deps.notes.get_current_week_notes(max_results=20)
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

        # Step 2: wiki pages via index.
        if intent.get("needs_search"):
            try:
                wiki_pages = deps.wiki.retrieve_pages(text, intent.get("keywords", []))
                if wiki_pages:
                    wiki_block = "\n\n".join(
                        f"[Wiki: {p['name'].replace('.md', '')}]\n{p['content']}"
                        for p in wiki_pages
                    )
                    context_parts.append(wiki_block)
            except Exception as e:
                print(f"[core] Wiki search error (non-fatal): {e}")

        # Step 3: smart search raw notes.
        if intent.get("needs_search") and intent.get("keywords"):
            try:
                notes = deps.notes.smart_search(
                    keywords=intent["keywords"],
                    days_back=intent.get("days_back", 0) or 0,
                )
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
                    if fallback_notes:
                        context_parts.append("\n\n".join(
                            [f"[{n['name']}]\n{n['content']}" for n in fallback_notes]
                        ))
                except Exception:
                    pass

        notes_context = "\n\n".join(context_parts)

        # Step 4: ask Claude.
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
    "DAT_BIRTHDATE":      ["dat birthdate: ", "đặt birthdate: ", "set birthdate: "],
    "DUYET_BIRTHDATE":    ["duyet birthdate", "duyệt birthdate", "approve birthdate"],
    "DAT_USERNAME":       ["dat username: ", "đặt username: ", "set username: "],
    "DUYET_USERNAME":     ["duyet username", "duyệt username", "approve username"],
    "DAT_CHA":            ["dat cha: ", "đặt cha: ", "set parent: "],
    "XEM_CHA":            ["xem cha: ", "view parent: "],
    "XEM_QUOTA":          ["xem quota", "view quota"],
    "DAT_QUOTA":          ["dat quota: ", "đặt quota: ", "set quota: "],
    "RESET_QUOTA":        ["reset quota: "],
    "HOI_WIKI":           ["hỏi wiki ", "hoi wiki ", "ask wiki "],
    "XEM_WIKI_PAGE": ["xem wiki "],
    "XEM_WIKI":      ["xem wiki"],
    "WIKI":          ["wiki "],
    "GHI_NHO_VAO":   ["ghi nhớ vào ", "ghi nho vao "],
    "GHI_NHO":       ["ghi nhớ ", "ghi nho "],
    "NHAT_KY":       ["nhật ký ", "nhat ky "],
    "XEM_NHAT_KY":   ["xem nhật ký", "xem nhat ky"],
    "LIET_KE":       ["liệt kê", "liet ke"],
    "TIM":           ["tìm ", "tim ", "search "],
    "XEM":           ["xem "],
    "TOM_TAT_TUAN":  ["tóm tắt tuần này", "tom tat tuan nay", "tóm tắt tuần", "tom tat tuan"],
}


async def handle_message(msg: ChannelMessage, user: User, deps: CoreDeps) -> None:
    """Dispatch a normalized inbound message to the appropriate handler."""
    chat_id = msg.chat_id
    text = msg.text.strip()
    if not text:
        return

    # ── Step 1: try to resolve a pending state first ────────────────────────
    if await _try_resolve_pending(chat_id, text, deps):
        return

    # ── Step 2: slash commands (not normalized) ────────────────────────────
    if text == "/start":
        await _cmd_start(chat_id, deps); return
    if text == "/cost":
        await _cmd_cost(chat_id, deps); return
    if text == "/test":
        await _cmd_test(chat_id, deps); return
    if text == "/security":
        await _cmd_security(chat_id, deps); return

    # ── Step 2.5: quota enforcement for LLM-heavy operations ──────────────────
    # Non-LLM commands (user management, quota admin) bypass this check.
    _QUOTA_EXEMPT = {
        "THEM_USER", "XEM_DANH_SACH_USER", "XOA_USER",
        "DAT_BIRTHDATE", "DUYET_BIRTHDATE",
        "DAT_USERNAME", "DUYET_USERNAME",
        "DAT_CHA", "XEM_CHA",
        "XEM_QUOTA", "DAT_QUOTA", "RESET_QUOTA",
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
        if cmd_id == "HOI_WIKI":
            await _cmd_wiki_query(chat_id, remainder, deps); return
        if cmd_id == "XEM_WIKI_PAGE":
            await _cmd_xem_wiki_page(chat_id, remainder, deps); return
        if cmd_id == "XEM_WIKI":
            await _cmd_xem_wiki_list(chat_id, deps); return
        if cmd_id == "WIKI":
            await _cmd_wiki_ingest(chat_id, remainder, deps); return
        if cmd_id == "GHI_NHO_VAO":
            await _cmd_ghi_nho_vao(chat_id, remainder, deps); return
        if cmd_id == "GHI_NHO":
            await _cmd_ghi_nho(chat_id, remainder, deps); return
        if cmd_id == "NHAT_KY":
            await _cmd_nhat_ky(chat_id, remainder, deps); return
        if cmd_id == "XEM_NHAT_KY":
            await _cmd_xem_nhat_ky(chat_id, deps); return
        if cmd_id == "LIET_KE":
            await _cmd_liet_ke(chat_id, deps); return
        if cmd_id == "TIM":
            await _cmd_tim(chat_id, remainder, deps); return
        if cmd_id == "XEM":
            await _cmd_xem(chat_id, remainder, deps); return
        if cmd_id == "TOM_TAT_TUAN":
            await _cmd_tom_tat_tuan(chat_id, deps); return

    # ── Step 4: free-form question → wiki + smart search + Claude ──────────
    await _handle_general_question(chat_id, text, deps, user=user)
