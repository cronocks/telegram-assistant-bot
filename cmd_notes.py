"""cmd_notes.py — Notes, journal, scope, and whoami command handlers.

Covers: ghi_nho, ghi_nho_vao, nhat_ky, xem_nhat_ky, xem, liet_ke, tim,
chia_se, bo_chia_se, xem_scope, toi_la_ai.
"""
import traceback

import acl as acl_mod
from cmd_utils import (
    _acl_filter_notes,
    _elevation_remaining_minutes,
    _register_note,
    _set_pending,
    _visible_notes_with_meta,
)
from config import FUZZY_SHOW_LIMIT, PENDING_CHOICE_TIMEOUT_SEC
from cost_monitor import check_and_alert, record_usage
from deps import CoreDeps
from interfaces import User
from timeutils import time_str, today_str


_LIET_KE_PAGE_SIZE = 20


async def _cmd_ghi_nho(chat_id: str, content: str, user: User, deps: CoreDeps) -> None:
    """ghi nhớ <content> → create a new file with a Claude-generated title."""
    if not content:
        await deps.channel.send(chat_id, "Vui lòng nhập nội dung cần ghi nhớ.")
        return
    await deps.channel.send(chat_id, "Đang lưu...")
    try:
        title, tokens = deps.llm.ask(
            f"Tao tieu de ngan (toi da 6 tu) cho ghi chu sau, chi tra ve tieu de: {content}"
        )
        record_usage(tokens // 2, tokens // 2)
        filename, file_id = deps.notes.save_note(title.strip(), content)
        _register_note(file_id, user.id, "note", filename, deps)
        await deps.channel.send(chat_id, f"Đã lưu: {filename}", use_markdown=False)
    except PermissionError as e:
        traceback.print_exc()
        await deps.channel.send(
            chat_id, f"Từ chối vì lý do bảo mật: {str(e)[:400]}", use_markdown=False,
        )
    except Exception as e:
        traceback.print_exc()
        await deps.channel.send(
            chat_id, f"Lỗi khi lưu: {str(e)[:500]}", use_markdown=False,
        )


async def _cmd_ghi_nho_vao(chat_id: str, body: str, deps: CoreDeps) -> None:
    """ghi nhớ vào <name>: <content> — split on the first ':' and append."""
    if ":" not in body:
        await deps.channel.send(
            chat_id,
            "Cú pháp: ghi nhớ vào <tên-file>: <nội dung>\n"
            "Ví dụ: ghi nhớ vào kiểm tra: thêm câu hỏi mới",
            use_markdown=False,
        )
        return

    name_part, content = body.split(":", 1)
    name_part = name_part.strip()
    content = content.strip()

    if not name_part:
        await deps.channel.send(chat_id, "Thiếu tên file.", use_markdown=False)
        return
    if not content:
        await deps.channel.send(chat_id, "Thiếu nội dung.", use_markdown=False)
        return

    await deps.channel.send(
        chat_id, f"Đang tìm file '{name_part}'...", use_markdown=False,
    )

    try:
        matches = deps.notes.find_files_fuzzy(name_part)
    except Exception as e:
        traceback.print_exc()
        await deps.channel.send(
            chat_id, f"Lỗi khi tìm: {str(e)[:400]}", use_markdown=False,
        )
        return

    # Case 1: exactly one match → append directly.
    if len(matches) == 1:
        chosen = matches[0]
        try:
            entry = f"\n## {time_str()}\n{content}\n"
            filename = deps.notes.append_to_file(chosen["id"], entry)
            await deps.channel.send(
                chat_id, f"Đã thêm vào: {filename}", use_markdown=False,
            )
        except Exception as e:
            traceback.print_exc()
            await deps.channel.send(
                chat_id, f"Lỗi khi thêm: {str(e)[:400]}", use_markdown=False,
            )
        return

    # Case 2: multiple matches → ask user to pick.
    if len(matches) > 1:
        shown = matches[:FUZZY_SHOW_LIMIT]
        msg_lines = [f"Tìm thấy {len(matches)} file khớp với '{name_part}':"]
        for i, f in enumerate(shown, 1):
            msg_lines.append(f"{i}. {f['name']}")
        if len(matches) > FUZZY_SHOW_LIMIT:
            msg_lines.append(f"... ({len(matches) - FUZZY_SHOW_LIMIT} file khác)")
        msg_lines.append(f"\nTrả lời 1-{len(shown)} để chọn, hoặc 'huỷ'.")
        msg_lines.append(f"(Hết hạn sau {PENDING_CHOICE_TIMEOUT_SEC}s)")

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
        f"Không tìm thấy file '{name_part}'.\n"
        f"Tạo file mới với tên đó? (yes/no)\n"
        f"(Hết hạn sau {PENDING_CHOICE_TIMEOUT_SEC}s)",
        use_markdown=False,
    )


async def _cmd_nhat_ky(chat_id: str, content: str, user: User, deps: CoreDeps) -> None:
    """nhật ký <content> → append to today's journal."""
    if not content:
        await deps.channel.send(chat_id, "Vui lòng nhập nội dung.", use_markdown=False)
        return

    await deps.channel.send(chat_id, "Đang ghi nhật ký...")
    try:
        filename, action, file_id = deps.notes.add_to_daily_journal(content)
        if action == "created":
            _register_note(file_id, user.id, "journal", filename, deps)
        else:
            deps.note_index.touch_note(file_id)
        verb = "Đã tạo mới" if action == "created" else "Đã thêm vào"
        await deps.channel.send(
            chat_id, f"{verb}: {filename}", use_markdown=False,
        )
    except PermissionError as e:
        traceback.print_exc()
        await deps.channel.send(
            chat_id, f"Từ chối vì lý do bảo mật: {str(e)[:400]}", use_markdown=False,
        )
    except Exception as e:
        traceback.print_exc()
        await deps.channel.send(chat_id, f"Lỗi: {str(e)[:500]}", use_markdown=False)


async def _cmd_xem_nhat_ky(chat_id: str, deps: CoreDeps) -> None:
    """xem nhật ký → read today's journal."""
    try:
        journal = deps.notes.get_today_journal()
        if not journal:
            await deps.channel.send(
                chat_id,
                f"Chưa có nhật ký cho ngày {today_str()}. "
                f"Hãy tạo bằng lệnh: nhat ky <noi dung>",
                use_markdown=False,
            )
            return
        content = journal["content"]
        if len(content) > 3500:
            content = content[:3500] + "\n\n[...] (quá dài, đã cắt)"
        await deps.channel.send(
            chat_id, f"=== {journal['name']} ===\n\n{content}", use_markdown=False,
        )
    except Exception as e:
        traceback.print_exc()
        await deps.channel.send(chat_id, f"Lỗi: {str(e)[:500]}", use_markdown=False)


async def _cmd_xem(chat_id: str, name_query: str, user: User, deps: CoreDeps) -> None:
    """xem <name> → read a file (fuzzy match, ACL-filtered)."""
    if not name_query:
        await deps.channel.send(chat_id, "Cú pháp: xem <tên-file>", use_markdown=False)
        return

    try:
        matches = deps.notes.find_files_fuzzy(name_query)
    except Exception as e:
        traceback.print_exc()
        await deps.channel.send(
            chat_id, f"Lỗi khi tìm: {str(e)[:400]}", use_markdown=False,
        )
        return

    matches, _ = _visible_notes_with_meta(matches, user, deps)

    if not matches:
        await deps.channel.send(
            chat_id,
            f"Không tìm thấy file nào khớp với '{name_query}'.",
            use_markdown=False,
        )
        return

    if len(matches) == 1:
        chosen = matches[0]
        try:
            file_data = deps.notes.read_file_by_id(chosen["id"])
            content = file_data["content"]
            if len(content) > 3500:
                content = content[:3500] + "\n\n[...] (quá dài, đã cắt)"
            await deps.channel.send(
                chat_id, f"=== {file_data['name']} ===\n\n{content}",
                use_markdown=False,
            )
        except Exception as e:
            traceback.print_exc()
            await deps.channel.send(
                chat_id, f"Lỗi khi đọc: {str(e)[:400]}", use_markdown=False,
            )
        return

    # Multiple matches → ask user to pick.
    shown = matches[:FUZZY_SHOW_LIMIT]
    msg_lines = [f"Tìm thấy {len(matches)} file khớp với '{name_query}':"]
    for i, f in enumerate(shown, 1):
        msg_lines.append(f"{i}. {f['name']}")
    if len(matches) > FUZZY_SHOW_LIMIT:
        msg_lines.append(f"... ({len(matches) - FUZZY_SHOW_LIMIT} file khác)")
    msg_lines.append(f"\nTrả lời 1-{len(shown)} để chọn, hoặc 'huỷ'.")

    _set_pending(chat_id, "fuzzy_view", {"matches": shown})
    await deps.channel.send(chat_id, "\n".join(msg_lines), use_markdown=False)


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
        await deps.channel.send(chat_id, f"Lỗi: {str(e)[:500]}", use_markdown=False)
        return

    visible, metas = _visible_notes_with_meta(files, user, deps)
    if not visible:
        await deps.channel.send(
            chat_id, "Vault trống, chưa có ghi chú nào.", use_markdown=False,
        )
        return

    total = len(visible)
    total_pages = (total + _LIET_KE_PAGE_SIZE - 1) // _LIET_KE_PAGE_SIZE
    page = min(page, total_pages)
    start = (page - 1) * _LIET_KE_PAGE_SIZE
    chunk = visible[start:start + _LIET_KE_PAGE_SIZE]

    lines = [f"Tất cả file ({total} file) - Trang {page}/{total_pages}", ""]
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
        await deps.channel.send(chat_id, "Vui lòng nhập từ khoá.")
        return
    await deps.channel.send(
        chat_id, f"Đang tìm '{keyword}'...", use_markdown=False,
    )
    try:
        notes = deps.notes.search_notes(keyword)
        notes = _acl_filter_notes(notes, user, deps)
        if not notes:
            await deps.channel.send(
                chat_id, "Không tìm thấy ghi chú nào.", use_markdown=False,
            )
            return
        summary, tokens = deps.llm.summarize_notes(notes)
        record_usage(tokens // 2, tokens // 2)
        check_and_alert()
        await deps.channel.send(
            chat_id, f"Kết quả:\n\n{summary}", use_markdown=False,
        )
    except Exception as e:
        traceback.print_exc()
        await deps.channel.send(
            chat_id, f"Lỗi khi tìm: {str(e)[:500]}", use_markdown=False,
        )


async def _cmd_set_scope(
    chat_id: str, name: str, new_scope: str, user: User, deps: CoreDeps
) -> None:
    """Shared logic for chia se / bo chia se — change scope of a note or wiki page."""
    if not name:
        verb = "chia se" if new_scope == "everyone" else "bo chia se"
        await deps.channel.send(
            chat_id, f"Cú pháp: {verb} <tên-file>", use_markdown=False,
        )
        return

    # 1. Search notes folder first.
    try:
        matches = deps.notes.find_files_fuzzy(name)
    except Exception as e:
        await deps.channel.send(chat_id, f"Lỗi khi tìm: {str(e)[:400]}", use_markdown=False)
        return

    if len(matches) > 1:
        names = "\n".join(f"- {m['name']}" for m in matches[:5])
        await deps.channel.send(
            chat_id,
            f"Tìm thấy {len(matches)} file khớp với '{name}':\n{names}\n\nVui lòng nhập tên cụ thể hơn.",
            use_markdown=False,
        )
        return

    if len(matches) == 1:
        file_id = matches[0]["id"]
        meta = deps.note_index.get_note_meta(file_id)
        if meta is None:
            await deps.channel.send(
                chat_id,
                "File này chưa được index. Vui lòng liên hệ admin để backfill.",
                use_markdown=False,
            )
            return
        if meta["owner_user_id"] != user.id:
            await deps.channel.send(
                chat_id, "Bạn không phải chủ file này.", use_markdown=False,
            )
            return
        ok = deps.note_index.set_note_scope(file_id, new_scope, user.id)
        if ok:
            label = "chia sẻ với mọi người" if new_scope == "everyone" else "riêng tư"
            await deps.channel.send(
                chat_id,
                f"Đã đổi '{matches[0]['name']}' thành {label}.",
                use_markdown=False,
            )
        else:
            await deps.channel.send(chat_id, "Không thể đổi scope.", use_markdown=False)
        return

    # 2. No note match — try wiki.
    try:
        page = deps.wiki.find_page(name)
    except Exception as e:
        await deps.channel.send(chat_id, f"Lỗi khi tìm wiki: {str(e)[:400]}", use_markdown=False)
        return

    if page:
        file_id = page["id"]
        meta = deps.note_index.get_wiki_meta(file_id)
        if meta is None:
            await deps.channel.send(
                chat_id,
                "Trang wiki này chưa được index. Vui lòng liên hệ admin để backfill.",
                use_markdown=False,
            )
            return
        if meta["owner_user_id"] != user.id:
            await deps.channel.send(
                chat_id, "Bạn không phải chủ trang wiki này.", use_markdown=False,
            )
            return
        ok = deps.note_index.set_wiki_scope(file_id, new_scope, user.id)
        if ok:
            label = "chia sẻ với mọi người" if new_scope == "everyone" else "riêng tư"
            await deps.channel.send(
                chat_id,
                f"Đã đổi wiki '{page['name'].removesuffix('.md')}' thành {label}.",
                use_markdown=False,
            )
        else:
            await deps.channel.send(chat_id, "Không thể đổi scope.", use_markdown=False)
        return

    await deps.channel.send(
        chat_id, f"Không tìm thấy file '{name}' trong ghi chú hoặc wiki.", use_markdown=False,
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
        scope_str = "🌐 chia sẻ với mọi người"
    else:
        scope_str = "🔒 riêng tư"
    kind = "wiki" if is_wiki else meta.get("kind", "note")
    created = (meta.get("created_at") or "")[:10]
    lines = [
        f"📄 {filename}",
        f"   Scope: {scope_str}",
        f"   Owner: {owner_str}",
        f"   Loại:  {kind}",
        f"   Ngày:  {created}",
    ]
    await deps.channel.send(chat_id, "\n".join(lines), use_markdown=False)


async def _cmd_xem_scope(
    chat_id: str, name: str, user: User, deps: CoreDeps
) -> None:
    """xem scope <ten-file> — show scope/owner/kind of a note or wiki page."""
    if not name:
        await deps.channel.send(
            chat_id, "Cú pháp: xem scope <tên-file>", use_markdown=False,
        )
        return

    # 1. Search notes folder first.
    try:
        matches = deps.notes.find_files_fuzzy(name)
    except Exception as e:
        await deps.channel.send(chat_id, f"Lỗi khi tìm: {str(e)[:400]}", use_markdown=False)
        return

    if len(matches) > 1:
        names = "\n".join(f"- {m['name']}" for m in matches[:FUZZY_SHOW_LIMIT])
        await deps.channel.send(
            chat_id,
            f"Tìm thấy {len(matches)} file khớp với '{name}':\n{names}\n\n"
            f"Vui lòng nhập tên cụ thể hơn.",
            use_markdown=False,
        )
        return

    if len(matches) == 1:
        meta = deps.note_index.get_note_meta(matches[0]["id"])
        if meta is None:
            await deps.channel.send(
                chat_id, "File này chưa được index.", use_markdown=False,
            )
            return
        allowed, is_stealth = acl_mod.can_read(
            user, meta["scope"], meta["owner_user_id"], user_store=deps.user_store,
        )
        if not allowed:
            await deps.channel.send(
                chat_id, f"Không tìm thấy file '{name}'.", use_markdown=False,
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
        await deps.channel.send(chat_id, f"Lỗi khi tìm wiki: {str(e)[:400]}", use_markdown=False)
        return

    if page:
        meta = deps.note_index.get_wiki_meta(page["id"])
        if meta is None:
            await deps.channel.send(
                chat_id, "Trang wiki này chưa được index.", use_markdown=False,
            )
            return
        allowed, is_stealth = acl_mod.can_read(
            user, meta["scope"], meta["owner_user_id"], user_store=deps.user_store,
        )
        if not allowed:
            await deps.channel.send(
                chat_id, f"Không tìm thấy file '{name}'.", use_markdown=False,
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
        chat_id, f"Không tìm thấy file '{name}' trong ghi chú hoặc wiki.", use_markdown=False,
    )


async def _cmd_whoami(chat_id: str, user: User, deps: CoreDeps) -> None:
    """toi la ai — show the user bound to this chat, plus any active elevation."""
    role_labels = {
        "admin": "Quản trị viên",
        "manager": "Người quản lý",
        "member": "Thành viên",
        "readonly": "Chỉ đọc",
    }
    # Resolve base role from DB so elevation override doesn't mask it.
    base = deps.user_store.get_user_by_id(user.id) or user
    session = deps.elevation_store.get_active_session("telegram", chat_id)

    username = base.username or "(chưa đặt)"
    lines = [
        "👤 Tài khoản hiện tại:",
        f"   Tên:      {base.name}",
        f"   Username: {username}",
        f"   Vai trò:  {role_labels.get(base.role, base.role)}",
        f"   User ID:  #{base.id}",
    ]
    if session is not None:
        remaining = _elevation_remaining_minutes(session["expires_at"])
        if remaining > 0:
            lines.append(f"   Sudo:     đang nâng quyền admin (còn ~{remaining} phút)")
    await deps.channel.send(chat_id, "\n".join(lines), use_markdown=False)
