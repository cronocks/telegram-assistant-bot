"""cmd_audit.py — Audit log viewer + recycle bin commands.

Covers: `xem audit`, `xem thung rac`, `khoi phuc`, `xoa han`.
"""
from deps import CoreDeps
from interfaces import User
from permissions import has_role


_AUDIT_PAGE_SIZE = 20

_RECYCLE_KINDS = ("user", "note", "wiki")


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
            chat_id, "Thùng rác trống.", use_markdown=False,
        )
        return

    lines = [f"Thùng rác ({total} mục):"]
    if deleted_users:
        lines.append("\n— Users —")
        for u in deleted_users:
            del_at = u.deleted_at.strftime("%Y-%m-%d") if u.deleted_at else "?"
            lines.append(f"• [user {u.id}] {u.name} (role={u.role}) — đã xoá {del_at}")
    if deleted_notes:
        lines.append("\n— Notes —")
        for n in deleted_notes:
            title = n.get("title") or "(no title)"
            del_at = (n.get("deleted_at") or "")[:10]
            lines.append(f"• [note {n['id']}] {title} (owner={n['owner_user_id']}) — đã xoá {del_at}")
    if deleted_wikis:
        lines.append("\n— Wiki —")
        for w in deleted_wikis:
            del_at = (w.get("deleted_at") or "")[:10]
            lines.append(f"• [wiki {w['id']}] {w['topic']} (owner={w['owner_user_id']}) — đã xoá {del_at}")

    lines.append(
        "\nKhôi phục: `khoi phuc: <kind> <id>` (vd `khoi phuc: user 3`)"
        "\nXoá hẳn:   `xoa han: <kind> <id>`"
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
            "Cú pháp: khoi phuc: <kind> <id>\n"
            "Kind hợp lệ: user, note, wiki\n"
            "Ví dụ: khoi phuc: user 3",
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
            f"Không tìm thấy {label} trong thùng rác (hoặc đã khôi phục).",
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
        chat_id, f"Đã khôi phục {label}.", use_markdown=False,
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
            "Cú pháp: xoa han: <kind> <id>\n"
            "Kind hợp lệ: user, note, wiki\n"
            "Ví dụ: xoa han: note 12",
            use_markdown=False,
        )
        return

    kind, target_id = parsed

    if kind == "user":
        ok = deps.user_store.hard_delete_user(target_id)
        if not ok:
            await deps.channel.send(
                chat_id,
                f"Không thể xoá hẳn user #{target_id}. "
                "Có thể user không tồn tại, hoặc còn dữ liệu tham chiếu "
                "(channel_bindings, notes, parent_links...). "
                "Hãy thử khôi phục + cleanup tay nếu cần.",
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
            chat_id, f"Đã xoá hẳn user #{target_id}.", use_markdown=False,
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
            chat_id, f"Không tìm thấy {kind} #{target_id}.", use_markdown=False,
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
        chat_id, f"Đã xoá hẳn {kind} #{target_id}.{suffix}", use_markdown=False,
    )
