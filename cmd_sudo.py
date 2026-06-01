"""cmd_sudo.py — Sudo elevation and password command handlers.

Covers: dat_mat_khau (admin password), dat_web_pass (admin sets web password
for others), doi_web_pass_self (self-service web password), sudo, thoat_sudo.
"""
import config
from deps import CoreDeps
from interfaces import User


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
            "Chỉ tài khoản admin (không qua sudo) mới có thể đặt mật khẩu.",
            use_markdown=False,
        )
        return

    # Reject if the caller is only admin via elevation — they're not natively-admin.
    if deps.elevation_store.get_active_session("telegram", chat_id) is not None:
        await deps.channel.send(
            chat_id,
            "Lệnh này không dùng khi đang sudo. Hãy dùng từ tài khoản admin gốc.",
            use_markdown=False,
        )
        return

    password = password.strip()
    if len(password) < 8:
        await deps.channel.send(
            chat_id, "Mật khẩu phải dài ít nhất 8 ký tự.", use_markdown=False,
        )
        return

    try:
        deps.user_store.set_password(base.id, password)
    except Exception as e:
        await deps.channel.send(
            chat_id, f"Lỗi khi đặt mật khẩu: {str(e)[:200]}", use_markdown=False,
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
        "Đã đặt mật khẩu admin. Tin nhắn chứa mật khẩu đã bị xoá khỏi chat.",
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
            chat_id, "Chỉ admin mới có thể đặt mật khẩu web cho người khác.", use_markdown=False,
        )
        return

    parts = remainder.split(",", 1)
    if len(parts) != 2:
        await deps.channel.send(
            chat_id,
            "Cú pháp: dat web pass: <tên_user>, <mật_khẩu>",
            use_markdown=False,
        )
        return

    target_name = parts[0].strip()
    password = parts[1].strip()

    if len(password) < 8:
        await deps.channel.send(
            chat_id, "Mật khẩu phải có ít nhất 8 ký tự.", use_markdown=False,
        )
        return

    target = deps.user_store.find_by_username_or_name(target_name)
    if target is None or not target.is_active:
        await deps.channel.send(
            chat_id, f"Không tìm thấy user: {target_name}", use_markdown=False,
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
        f"Đã đặt mật khẩu web cho {target.name}. Họ sẽ phải đổi mật khẩu khi đăng nhập lần đầu.",
        use_markdown=False,
    )


async def _cmd_doi_web_pass_self(
    chat_id: str,
    password: str,
    user: User,
    message_id: int | None,
    deps: CoreDeps,
) -> None:
    """doi web pass: <mat_khau> — self-service web password set via Telegram.

    Available to all authenticated users. Telegram channel binding is the
    identity proof — no current-password check needed. Sets must_change_password=False
    so the user can log in directly without a force-reset.
    Auto-deletes the message containing the password.
    """
    password = password.strip()
    if len(password) < 8:
        await deps.channel.send(
            chat_id, "Mật khẩu phải có ít nhất 8 ký tự.", use_markdown=False,
        )
        return

    try:
        deps.user_store.set_password(user.id, password)
        deps.user_store.set_must_change_password(user.id, False)
    except Exception as e:
        await deps.channel.send(
            chat_id, f"Lỗi khi đặt mật khẩu: {str(e)[:200]}", use_markdown=False,
        )
        return

    # Auto-delete the message containing the password.
    if message_id is not None:
        try:
            await deps.channel.delete_message(chat_id, message_id)
        except Exception as e:
            print(f"[doi_web_pass] delete_message error (non-fatal): {e}")

    deps.audit.log(
        actor_user_id=user.id,
        action="web_password_set_self",
        target_type="user",
        target_id=user.id,
        payload={"channel": "telegram"},
    )
    await deps.channel.send(
        chat_id,
        "Đã đặt mật khẩu web thành công. Bạn có thể đăng nhập tại web bằng username và mật khẩu này.\n"
        "Tin nhắn chứa mật khẩu đã bị xoá khỏi chat.",
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
            "Bạn đã là admin, không cần sudo.",
            use_markdown=False,
        )
        return

    if base.role != "manager":
        await deps.channel.send(
            chat_id,
            "Chỉ role manager mới được dùng sudo.",
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
            f"Đã bị khoá do nhập sai quá nhiều. Thử lại sau (mở khoá lúc {locked_until} UTC).",
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
            chat_id, "Cú pháp: sudo: <mật khẩu>", use_markdown=False,
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
                f"Mật khẩu sai. Đã bị khoá đến {state['locked_until']} UTC.",
                use_markdown=False,
            )
        else:
            remaining = config.SUDO_MAX_FAILS - state["failed_count"]
            await deps.channel.send(
                chat_id,
                f"Mật khẩu sai. Còn {remaining} lần thử trước khi bị khoá.",
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
        f"Đã nâng quyền admin trong {config.SUDO_TTL_MINUTES} phút. "
        f"Dùng 'thoat sudo' để hạ quyền sớm.",
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
            chat_id, "Đã hạ quyền admin.", use_markdown=False,
        )
    else:
        await deps.channel.send(
            chat_id, "Bạn không đang trong phiên sudo.", use_markdown=False,
        )
