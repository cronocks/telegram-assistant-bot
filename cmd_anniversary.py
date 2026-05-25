"""cmd_anniversary.py — FR-8 anniversary command handlers.

Telegram commands:
  - them ky niem: <name>, âm/dương DD/MM[, <category>]
  - danh sach ky niem
  - ky niem <id>
  - xoa ky niem: <id>
  - sua ky niem: <id>, ten=<>, ngay=<âm/dương DD/MM>, loai=<>

Parsing uses a simple regex/split rather than LLM — anniversary input is
structured enough that an LLM call would be wasteful.
"""
from __future__ import annotations

import re
from datetime import datetime

from deps import CoreDeps
from text_utils import normalize_vn
from timeutils import VIETNAM_TZ

CATEGORY_MAP = {
    "gio": "gio", "giỗ": "gio",
    "cuoi": "cuoi", "cưới": "cuoi",
    "khac": "khac", "khác": "khac",
}

DATE_TYPE_MAP = {
    "am": "lunar", "âm": "lunar",
    "duong": "solar", "dương": "solar",
}


class ParseAnniversaryError(ValueError):
    pass


def _normalize_token(s: str) -> str:
    """Lowercase + strip diacritics for matching keywords."""
    return normalize_vn(s).lower().strip()


def parse_anniversary_input(body: str) -> dict:
    """Parse `name, âm/dương DD/MM[, category]` into a kwargs dict for create_anniversary."""
    parts = [p.strip() for p in body.split(",")]
    if len(parts) < 2:
        raise ParseAnniversaryError(
            "Cần ít nhất tên và ngày, vd: 'Giỗ ông nội, âm 10/3, giỗ'"
        )
    name = parts[0]
    if not name:
        raise ParseAnniversaryError("Tên kỷ niệm không được để trống.")

    date_part = parts[1].strip()
    # Optional trailing "nhuận"/"nhuan" after the date numbers.
    m = re.match(r"^(\S+)\s+(\d{1,2})\s*/\s*(\d{1,2})(?:\s+\S+)?$", date_part)
    if not m:
        raise ParseAnniversaryError(
            f"Định dạng ngày sai: {date_part!r}. Dùng 'âm DD/MM' hoặc 'dương DD/MM'."
        )
    dt_key = _normalize_token(m.group(1))
    if dt_key not in DATE_TYPE_MAP:
        raise ParseAnniversaryError(
            f"Loại ngày {m.group(1)!r} không hợp lệ. Dùng 'âm' hoặc 'dương'."
        )
    date_type = DATE_TYPE_MAP[dt_key]
    try:
        day = int(m.group(2))
        month = int(m.group(3))
    except ValueError:
        raise ParseAnniversaryError(f"Ngày/tháng sai: {date_part!r}")
    if not (1 <= month <= 12) or not (1 <= day <= 31):
        raise ParseAnniversaryError(f"Ngày/tháng ngoài phạm vi: {date_part!r}")

    # "nhuận"/"nhuan" after the date numbers marks a lunar leap month.
    # Only meaningful for lunar dates; silently ignored for solar.
    is_leap_month = (
        1 if date_type == "lunar" and "nhuan" in normalize_vn(date_part).lower() else 0
    )

    category = "khac"
    if len(parts) >= 3 and parts[2]:
        cat_key = _normalize_token(parts[2])
        if cat_key not in CATEGORY_MAP:
            raise ParseAnniversaryError(
                f"Loại {parts[2]!r} không hợp lệ. Dùng 'giỗ', 'cưới', hoặc 'khác'."
            )
        category = CATEGORY_MAP[cat_key]

    return {
        "name": name, "date_type": date_type,
        "day": day, "month": month, "is_leap_month": is_leap_month,
        "category": category,
    }


def parse_anniversary_id(body: str) -> int | None:
    token = body.strip().split()[0] if body.strip() else ""
    if token.isdigit():
        return int(token)
    return None


def _format_date_human(row: dict) -> str:
    label = "Âm" if row["date_type"] == "lunar" else "Dương"
    return f"{label} {row['day']:02d}/{row['month']:02d}"


def _format_list(rows: list[dict]) -> str:
    if not rows:
        return "Bạn chưa có kỷ niệm nào."
    lines = ["📅 *Danh sách kỷ niệm:*"]
    icon = {"gio": "🕯", "cuoi": "💐", "khac": "📅"}
    for r in rows:
        em = icon.get(r["category"], "📅")
        status = "" if r["enabled"] else " (tắt)"
        lines.append(
            f"{em} #{r['id']} {r['name']} — {_format_date_human(r)}{status}"
        )
    return "\n".join(lines)


def _format_detail(row: dict) -> str:
    icon = {"gio": "🕯", "cuoi": "💐", "khac": "📅"}.get(row["category"], "📅")
    lines = [
        f"{icon} *Kỷ niệm #{row['id']}*",
        f"📝 {row['name']}",
        f"📅 Ngày: {_format_date_human(row)}",
        f"🏷 Loại: {row['category']}",
        f"⏰ Nhắc trước: {row['reminder_offsets']} ngày",
    ]
    if not row["enabled"]:
        lines.append("⚪ Trạng thái: đang tắt")
    if row.get("note"):
        lines.append(f"💬 {row['note']}")
    return "\n".join(lines)


# ── Command handlers ──────────────────────────────────────────────────────────


async def _cmd_them_ky_niem(chat_id, body, user, deps: CoreDeps) -> None:
    if deps.anniversary_store is None or deps.anniversary_engine is None:
        await deps.channel.send(
            chat_id, "Tính năng kỷ niệm chưa được kích hoạt.", use_markdown=False,
        )
        return
    body = body.strip()
    if not body:
        await deps.channel.send(
            chat_id,
            "Vui lòng nhập theo định dạng:\n"
            "  them ky niem: <tên>, âm/dương DD/MM, <loại>\n"
            "Ví dụ: them ky niem: Giỗ ông nội, âm 10/3, giỗ",
            use_markdown=False,
        )
        return

    try:
        parsed = parse_anniversary_input(body)
    except ParseAnniversaryError as e:
        await deps.channel.send(chat_id, f"⚠️ {e}", use_markdown=False)
        return

    try:
        row = deps.anniversary_store.create_anniversary(
            user_id=user.id, **parsed,
        )
    except ValueError as e:
        await deps.channel.send(chat_id, f"⚠️ {e}", use_markdown=False)
        return

    # Schedule reminders for current year (and next year if anniversary in current year has passed).
    current_year = datetime.now(VIETNAM_TZ).year
    deps.anniversary_engine.compute_year(current_year)

    deps.audit.log(
        actor_user_id=user.id,
        action="anniversary_created",
        target_type="anniversary",
        target_id=row["id"],
        payload={"name": row["name"], "date_type": row["date_type"]},
    )
    await deps.channel.send(
        chat_id,
        f"✅ Đã thêm kỷ niệm #{row['id']}: {row['name']} ({_format_date_human(row)})",
        use_markdown=False,
    )


async def _cmd_danh_sach_ky_niem(chat_id, user, deps: CoreDeps) -> None:
    if deps.anniversary_store is None:
        await deps.channel.send(
            chat_id, "Tính năng kỷ niệm chưa được kích hoạt.", use_markdown=False,
        )
        return
    rows = deps.anniversary_store.list_for_user(user.id)
    await deps.channel.send(chat_id, _format_list(rows), use_markdown=True)


async def _cmd_xem_ky_niem(chat_id, body, user, deps: CoreDeps) -> None:
    if deps.anniversary_store is None:
        await deps.channel.send(
            chat_id, "Tính năng kỷ niệm chưa được kích hoạt.", use_markdown=False,
        )
        return
    aid = parse_anniversary_id(body)
    if aid is None:
        await deps.channel.send(
            chat_id, "Cú pháp: ky niem <id>", use_markdown=False,
        )
        return
    row = deps.anniversary_store.get_anniversary(aid)
    if row is None or row["user_id"] != user.id or row["deleted_at"] is not None:
        await deps.channel.send(
            chat_id, "Không tìm thấy kỷ niệm.", use_markdown=False,
        )
        return
    await deps.channel.send(chat_id, _format_detail(row), use_markdown=True)


async def _cmd_xoa_ky_niem(chat_id, body, user, deps: CoreDeps) -> None:
    if deps.anniversary_store is None or deps.anniversary_engine is None:
        await deps.channel.send(
            chat_id, "Tính năng kỷ niệm chưa được kích hoạt.", use_markdown=False,
        )
        return
    aid = parse_anniversary_id(body)
    if aid is None:
        await deps.channel.send(
            chat_id, "Cú pháp: xoa ky niem: <id>", use_markdown=False,
        )
        return
    row = deps.anniversary_store.get_anniversary(aid)
    if row is None or row["user_id"] != user.id or row["deleted_at"] is not None:
        await deps.channel.send(
            chat_id, "Không tìm thấy kỷ niệm.", use_markdown=False,
        )
        return
    deps.anniversary_store.soft_delete_anniversary(aid)
    deps.anniversary_engine.cancel_all_for_anniversary(aid)
    deps.audit.log(
        actor_user_id=user.id,
        action="anniversary_deleted",
        target_type="anniversary",
        target_id=aid,
        payload={"name": row["name"]},
    )
    await deps.channel.send(
        chat_id, f"🗑 Đã xoá kỷ niệm #{aid}.", use_markdown=False,
    )


async def _cmd_sua_ky_niem(chat_id, body, user, deps: CoreDeps) -> None:
    """Edit fields: ten=<>, ngay=<âm/dương DD/MM>, loai=<>, nhac=<csv>, bat/tat."""
    if deps.anniversary_store is None or deps.anniversary_engine is None:
        await deps.channel.send(
            chat_id, "Tính năng kỷ niệm chưa được kích hoạt.", use_markdown=False,
        )
        return
    parts = [p.strip() for p in body.split(",")]
    if len(parts) < 2:
        await deps.channel.send(
            chat_id,
            "Cú pháp: sua ky niem: <id>, ten=<>, ngay=<âm/dương DD/MM>, loai=<>",
            use_markdown=False,
        )
        return
    aid = parse_anniversary_id(parts[0])
    if aid is None:
        await deps.channel.send(chat_id, "ID không hợp lệ.", use_markdown=False)
        return
    row = deps.anniversary_store.get_anniversary(aid)
    if row is None or row["user_id"] != user.id or row["deleted_at"] is not None:
        await deps.channel.send(chat_id, "Không tìm thấy kỷ niệm.", use_markdown=False)
        return

    updates: dict = {}
    for token in parts[1:]:
        if "=" not in token:
            continue
        key, val = [x.strip() for x in token.split("=", 1)]
        key = _normalize_token(key)
        if key in ("ten", "name"):
            updates["name"] = val
        elif key in ("ngay", "date"):
            m = re.match(r"^(\S+)\s+(\d{1,2})\s*/\s*(\d{1,2})(?:\s+\S+)?$", val)
            if not m:
                await deps.channel.send(
                    chat_id, f"Định dạng ngày sai: {val!r}", use_markdown=False,
                )
                return
            dt_key = _normalize_token(m.group(1))
            if dt_key not in DATE_TYPE_MAP:
                await deps.channel.send(
                    chat_id, f"Loại ngày sai: {m.group(1)!r}", use_markdown=False,
                )
                return
            new_date_type = DATE_TYPE_MAP[dt_key]
            updates["date_type"] = new_date_type
            updates["day"] = int(m.group(2))
            updates["month"] = int(m.group(3))
            updates["is_leap_month"] = (
                1 if new_date_type == "lunar" and "nhuan" in normalize_vn(val).lower() else 0
            )
        elif key in ("loai", "category"):
            cat_key = _normalize_token(val)
            if cat_key not in CATEGORY_MAP:
                await deps.channel.send(
                    chat_id, f"Loại sai: {val!r}", use_markdown=False,
                )
                return
            updates["category"] = CATEGORY_MAP[cat_key]
        elif key in ("nhac", "offsets"):
            updates["reminder_offsets"] = val
        elif key in ("bat", "tat"):
            updates["enabled"] = 1 if key == "bat" else 0

    if not updates:
        await deps.channel.send(chat_id, "Không có thay đổi.", use_markdown=False)
        return

    try:
        new_row = deps.anniversary_store.update_anniversary(aid, **updates)
    except ValueError as e:
        await deps.channel.send(chat_id, f"⚠️ {e}", use_markdown=False)
        return

    # Recompute reminders if the date changed.
    if "date_type" in updates or "month" in updates or "day" in updates or "reminder_offsets" in updates:
        deps.anniversary_engine.cancel_all_for_anniversary(aid)
        deps.anniversary_engine.compute_year(datetime.now(VIETNAM_TZ).year)

    deps.audit.log(
        actor_user_id=user.id,
        action="anniversary_updated",
        target_type="anniversary",
        target_id=aid,
        payload={"changed_fields": list(updates.keys())},
    )
    await deps.channel.send(
        chat_id,
        f"✅ Đã cập nhật kỷ niệm #{aid}: {new_row['name']} ({_format_date_human(new_row)})",
        use_markdown=False,
    )
