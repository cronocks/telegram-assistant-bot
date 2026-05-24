"""cmd_user.py — User management command handlers.

Covers: user creation/deletion, role changes, birthdate, username,
parent-child relationships, and quota management.
"""
from datetime import date

from deps import CoreDeps
from interfaces import User
from permissions import can_manage, has_role
from text_utils import validate_username


_VALID_ROLES = {"admin", "manager", "member", "readonly"}

_MIN_BIRTHDATE = "1900-01-01"


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
