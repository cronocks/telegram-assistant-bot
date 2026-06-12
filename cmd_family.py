"""cmd_family.py — FR-11 family tree command handlers.

Telegram commands (Phase A):
  - them nguoi than: <name>[, doi <n>][, sinh <date>][, mat <date>][, gioi tinh nam/nu][, ten goi <alias>][, chi <branch>][, ghi chu <bio>]
  - danh sach nguoi than [doi <n>]
  - xem nguoi than <id | name>
  - sua nguoi than: <id>, <field>=<value>, ...
  - xoa nguoi than: <id>
  - them mo phan: <member_id>, <cemetery>[, dia chi <text>][, gps <lat>,<lng>][, lo <text>][, ghi chu <text>]
  - sua mo phan: <id>, <field>=<value>, ...
  - xoa mo phan: <id>
  - tim mo <name | id>

Date formats: 'am DD/MM/YYYY', 'duong DD/MM/YYYY', 'YYYY' (year only),
'khoang YYYY' (approximate year), optional trailing 'nhuan' for lunar leap
months. Parsing is regex/split fast-path — no LLM.
"""
from __future__ import annotations

import re

from deps import CoreDeps
from text_utils import normalize_vn

DATE_TYPE_MAP = {"am": "lunar", "duong": "solar"}
GENDER_MAP = {"nam": "nam", "nu": "nu"}

_GPS_RE = re.compile(r"gps\s+([^,\s]+)\s*,\s*([^,\s]+)", re.IGNORECASE)
_FULL_DATE_RE = re.compile(r"^(\d{1,2})\s*/\s*(\d{1,2})\s*/\s*(\d{4})$")

# Keyword → field key; multi-word keywords listed before their one-word
# prefixes so ("ghi", "chu") wins over a hypothetical ("ghi",).
_MEMBER_KEYWORDS: list[tuple[tuple[str, ...], str]] = [
    (("gioi", "tinh"), "gender"),
    (("ten", "goi"), "alias_name"),
    (("ghi", "chu"), "bio"),
    (("doi",), "generation"),
    (("sinh",), "birth"),
    (("mat",), "death"),
    (("chi",), "branch"),
]

_BURIAL_KEYWORDS: list[tuple[tuple[str, ...], str]] = [
    (("dia", "chi"), "address"),
    (("ghi", "chu"), "note"),
    (("lo",), "plot_info"),
]

_EDIT_FIELD_MAP = {
    "ten": "full_name",
    "ten goi": "alias_name",
    "doi": "generation",
    "sinh": "birth",
    "mat": "death",
    "gioi tinh": "gender",
    "chi": "branch",
    "ghi chu": "bio",
}


class ParseFamilyError(ValueError):
    pass


def _match_keyword(
    segment: str, keywords: list[tuple[tuple[str, ...], str]],
) -> tuple[str, str] | None:
    """Match a comma-separated segment against keyword word-sequences.

    Returns (field_key, original-case value after the keyword) or None.
    Matching is word-based because normalize_vn collapses whitespace, which
    makes positional slicing of the original string unreliable.
    """
    words = segment.split()
    if not words:
        return None
    norm_words = [normalize_vn(w) for w in words]
    for kw, field in keywords:
        if tuple(norm_words[: len(kw)]) == kw:
            value = " ".join(words[len(kw):]).strip()
            return field, value
    return None


def _parse_partial_date(text: str, prefix: str) -> dict:
    """Parse one date expression into {prefix}_* store fields.

    Accepts: 'am DD/MM/YYYY', 'duong DD/MM/YYYY' (optional trailing 'nhuan'),
    'YYYY', 'khoang YYYY'.
    """
    norm = normalize_vn(text).strip()
    if not norm:
        raise ParseFamilyError(f"Thiếu giá trị ngày sau '{prefix}'.")
    tokens = norm.split()

    approx = 0
    if tokens[0] == "khoang":
        approx = 1
        tokens = tokens[1:]

    # Year only: '1880' or 'khoang 1900'.
    if len(tokens) == 1 and re.fullmatch(r"\d{4}", tokens[0]):
        result = {f"{prefix}_year": int(tokens[0])}
        if approx:
            result[f"{prefix}_approx"] = 1
        return result

    # Full date: 'am DD/MM/YYYY [nhuan]'.
    if len(tokens) >= 2 and tokens[0] in DATE_TYPE_MAP:
        is_leap = 1 if "nhuan" in tokens[2:] else 0
        m = _FULL_DATE_RE.match(tokens[1])
        if not m:
            raise ParseFamilyError(
                f"Định dạng ngày sai: {text!r}. Dùng 'am DD/MM/YYYY', 'duong DD/MM/YYYY', "
                "'YYYY' hoặc 'khoang YYYY'."
            )
        day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if not (1 <= month <= 12) or not (1 <= day <= 31):
            raise ParseFamilyError(f"Ngày/tháng ngoài phạm vi: {text!r}")
        result = {
            f"{prefix}_date_type": DATE_TYPE_MAP[tokens[0]],
            f"{prefix}_day": day,
            f"{prefix}_month": month,
            f"{prefix}_year": year,
        }
        if is_leap:
            result[f"{prefix}_leap"] = 1
        if approx:
            result[f"{prefix}_approx"] = 1
        return result

    raise ParseFamilyError(
        f"Định dạng ngày sai: {text!r}. Dùng 'am DD/MM/YYYY', 'duong DD/MM/YYYY', "
        "'YYYY' hoặc 'khoang YYYY'."
    )


def _parse_gender(value: str) -> str:
    key = normalize_vn(value).strip()
    if key not in GENDER_MAP:
        raise ParseFamilyError(f"Giới tính {value!r} không hợp lệ. Dùng 'nam' hoặc 'nữ'.")
    return GENDER_MAP[key]


def _parse_generation(value: str) -> int:
    if not value.strip().isdigit():
        raise ParseFamilyError(f"Đời phải là số, nhận được: {value!r}")
    return int(value.strip())


def parse_member_input(body: str) -> dict:
    """Parse `them nguoi than:` body into kwargs for FamilyStore.create_member."""
    parts = [p.strip() for p in body.split(",")]
    name = parts[0]
    if not name:
        raise ParseFamilyError(
            "Tên không được để trống. Cú pháp: them nguoi than: <tên>[, doi <n>]"
            "[, sinh <ngày>][, mat <ngày>][, gioi tinh nam/nu][, ghi chu <text>]"
        )
    result: dict = {"full_name": name}
    for segment in parts[1:]:
        if not segment:
            continue
        matched = _match_keyword(segment, _MEMBER_KEYWORDS)
        if matched is None:
            raise ParseFamilyError(
                f"Không hiểu phần {segment!r}. Các mục hợp lệ: doi, sinh, mat, "
                "gioi tinh, ten goi, chi, ghi chu."
            )
        field, value = matched
        if not value:
            raise ParseFamilyError(f"Thiếu giá trị cho mục {segment!r}.")
        if field == "generation":
            result["generation"] = _parse_generation(value)
        elif field in ("birth", "death"):
            result.update(_parse_partial_date(value, field))
        elif field == "gender":
            result["gender"] = _parse_gender(value)
        else:  # alias_name, branch, bio — free text
            result[field] = value
    return result


def parse_burial_input(body: str) -> tuple[int, str, dict]:
    """Parse `them mo phan:` body → (member_id, cemetery_name, extra fields).

    GPS is extracted by regex before the comma split because its value itself
    contains a comma ('gps 20.94,105.82').
    """
    fields: dict = {}
    gps_match = _GPS_RE.search(body)
    if gps_match:
        try:
            lat, lng = float(gps_match.group(1)), float(gps_match.group(2))
        except ValueError:
            raise ParseFamilyError(
                f"Tọa độ GPS sai: {gps_match.group(0)!r}. Dùng 'gps <lat>,<lng>' (số thập phân)."
            )
        fields["lat"] = lat
        fields["lng"] = lng
        body = body[: gps_match.start()] + body[gps_match.end():]
    elif normalize_vn(body).find("gps") >= 0:
        raise ParseFamilyError("Tọa độ GPS sai. Dùng 'gps <lat>,<lng>' (số thập phân).")

    parts = [p.strip() for p in body.split(",") if p.strip()]
    if not parts or not parts[0].isdigit():
        raise ParseFamilyError(
            "Cú pháp: them mo phan: <id người thân>, <nghĩa trang>[, dia chi <text>]"
            "[, gps <lat>,<lng>][, lo <text>][, ghi chu <text>]"
        )
    member_id = int(parts[0])
    if len(parts) < 2:
        raise ParseFamilyError("Thiếu tên nghĩa trang / khu mộ.")
    cemetery_name = parts[1]

    for segment in parts[2:]:
        matched = _match_keyword(segment, _BURIAL_KEYWORDS)
        if matched is None:
            raise ParseFamilyError(
                f"Không hiểu phần {segment!r}. Các mục hợp lệ: dia chi, gps, lo, ghi chu."
            )
        field, value = matched
        if not value:
            raise ParseFamilyError(f"Thiếu giá trị cho mục {segment!r}.")
        fields[field] = value
    return member_id, cemetery_name, fields


def parse_edit_pairs(body: str) -> tuple[int, dict]:
    """Parse `sua nguoi than:` body `<id>, <field>=<value>, ...` → (id, store updates)."""
    parts = [p.strip() for p in body.split(",") if p.strip()]
    if not parts or not parts[0].isdigit():
        raise ParseFamilyError("Cú pháp: sua nguoi than: <id>, <mục>=<giá trị>, ...")
    target_id = int(parts[0])
    if len(parts) < 2:
        raise ParseFamilyError("Cần ít nhất một mục cần sửa, vd: doi=4")

    updates: dict = {}
    for segment in parts[1:]:
        if "=" not in segment:
            raise ParseFamilyError(f"Mục {segment!r} sai cú pháp. Dùng <mục>=<giá trị>.")
        raw_key, value = segment.split("=", 1)
        key = normalize_vn(raw_key).strip()
        value = value.strip()
        if key not in _EDIT_FIELD_MAP:
            raise ParseFamilyError(
                f"Mục {raw_key.strip()!r} không hợp lệ. "
                f"Các mục: {', '.join(sorted(_EDIT_FIELD_MAP))}."
            )
        if not value:
            raise ParseFamilyError(f"Thiếu giá trị cho mục {raw_key.strip()!r}.")
        field = _EDIT_FIELD_MAP[key]
        if field == "generation":
            updates["generation"] = _parse_generation(value)
        elif field in ("birth", "death"):
            updates.update(_parse_partial_date(value, field))
        elif field == "gender":
            updates["gender"] = _parse_gender(value)
        else:
            updates[field] = value
    return target_id, updates


# ── Formatting helpers ────────────────────────────────────────────────────────

_NOT_ENABLED = "Tính năng gia phả chưa được kích hoạt."
_NO_PERMISSION = "⚠️ Bạn không có quyền thực hiện thao tác này. Chỉ admin/manager mới sửa được gia phả."
_ADMIN_ROLES = {"admin", "manager"}


def _is_admin(user) -> bool:
    return getattr(user, "role", None) in _ADMIN_ROLES


def _maps_link(row: dict) -> str | None:
    if row.get("lat") is None or row.get("lng") is None:
        return None
    return f"https://maps.google.com/?q={row['lat']},{row['lng']}"


def _format_partial_date(row: dict, prefix: str) -> str | None:
    year = row.get(f"{prefix}_year")
    if year is None:
        return None
    approx = "khoảng " if row.get(f"{prefix}_approx") else ""
    month = row.get(f"{prefix}_month")
    day = row.get(f"{prefix}_day")
    if month is None or day is None:
        return f"{approx}{year}"
    label = "Âm" if row.get(f"{prefix}_date_type") == "lunar" else "Dương"
    leap = " (nhuận)" if row.get(f"{prefix}_leap") else ""
    return f"{approx}{label} {day:02d}/{month:02d}/{year}{leap}"


def _format_member_line(row: dict) -> str:
    gen = f" — đời {row['generation']}" if row.get("generation") else ""
    birth = _format_partial_date(row, "birth")
    death = _format_partial_date(row, "death")
    years = ""
    if birth or death:
        years = f" ({birth or '?'} → {death or 'nay'})"
    return f"👤 #{row['id']} {row['full_name']}{gen}{years}"


def _format_burial(record: dict) -> list[str]:
    lines = [f"🪦 Mộ phần: {record['cemetery_name']}"]
    if record.get("address"):
        lines.append(f"📍 Địa chỉ: {record['address']}")
    if record.get("plot_info"):
        lines.append(f"🧭 Vị trí: {record['plot_info']}")
    link = _maps_link(record)
    if link:
        lines.append(f"🗺 {link}")
    if record.get("note"):
        lines.append(f"💬 {record['note']}")
    return lines


def _format_member_detail(row: dict, burial: dict | None) -> str:
    lines = [f"👤 *Người thân #{row['id']}*", f"📝 {row['full_name']}"]
    if row.get("alias_name"):
        lines.append(f"🏷 Tên gọi: {row['alias_name']}")
    if row.get("generation"):
        gen = f"🧬 Đời: {row['generation']}"
        if row.get("branch"):
            gen += f" — Chi: {row['branch']}"
        lines.append(gen)
    elif row.get("branch"):
        lines.append(f"🧬 Chi: {row['branch']}")
    if row.get("gender"):
        lines.append(f"⚧ Giới tính: {'nam' if row['gender'] == 'nam' else 'nữ'}")
    birth = _format_partial_date(row, "birth")
    if birth:
        lines.append(f"🎂 Sinh: {birth}")
    death = _format_partial_date(row, "death")
    if death:
        lines.append(f"🕯 Mất: {death}")
    if row.get("bio"):
        lines.append(f"💬 {row['bio']}")
    if burial:
        lines.append("")
        lines.extend(_format_burial(burial))
    return "\n".join(lines)


def _resolve_member(body: str, deps: CoreDeps) -> tuple[dict | None, list[dict]]:
    """Resolve a member reference (numeric id or name) → (member, candidates).

    Returns (member, []) for a unique match, (None, candidates) when the name
    matches several members, (None, []) when nothing matches.
    """
    token = body.strip()
    if token.isdigit():
        row = deps.family_store.get_member(int(token))
        if row is None or row["deleted_at"] is not None:
            return None, []
        return row, []
    matches = deps.family_store.search_by_name(token)
    if len(matches) == 1:
        return matches[0], []
    return None, matches


# ── Command handlers ──────────────────────────────────────────────────────────


async def _cmd_them_nguoi_than(chat_id, body, user, deps: CoreDeps) -> None:
    if deps.family_store is None:
        await deps.channel.send(chat_id, _NOT_ENABLED, use_markdown=False)
        return
    if not _is_admin(user):
        await deps.channel.send(chat_id, _NO_PERMISSION, use_markdown=False)
        return
    body = body.strip()
    if not body:
        await deps.channel.send(
            chat_id,
            "Vui lòng nhập theo định dạng:\n"
            "  them nguoi than: <tên>[, doi <n>][, sinh <ngày>][, mat <ngày>]"
            "[, gioi tinh nam/nu][, ten goi <tên gọi>][, chi <chi>][, ghi chu <text>]\n"
            "Ngày: 'am DD/MM/YYYY', 'duong DD/MM/YYYY', 'YYYY' hoặc 'khoang YYYY'.\n"
            "Ví dụ: them nguoi than: Nguyễn Văn A, doi 3, sinh am 10/2/1920, mat am 15/7/1990",
            use_markdown=False,
        )
        return

    try:
        parsed = parse_member_input(body)
        row = deps.family_store.create_member(created_by=user.id, **parsed)
    except ValueError as e:
        await deps.channel.send(chat_id, f"⚠️ {e}", use_markdown=False)
        return

    deps.audit.log(
        actor_user_id=user.id,
        action="family_member_created",
        target_type="family_member",
        target_id=row["id"],
        payload={"full_name": row["full_name"]},
    )
    await deps.channel.send(
        chat_id,
        f"✅ Đã thêm người thân #{row['id']}: {row['full_name']}",
        use_markdown=False,
    )


async def _cmd_danh_sach_nguoi_than(chat_id, body, user, deps: CoreDeps) -> None:
    if deps.family_store is None:
        await deps.channel.send(chat_id, _NOT_ENABLED, use_markdown=False)
        return
    generation = None
    norm = normalize_vn(body).strip()
    m = re.fullmatch(r"doi\s+(\d+)", norm)
    if m:
        generation = int(m.group(1))
    rows = deps.family_store.list_members(generation=generation)
    if not rows:
        await deps.channel.send(
            chat_id, "Gia phả chưa có người thân nào.", use_markdown=False,
        )
        return
    lines = ["👨‍👩‍👧‍👦 *Danh sách người thân:*"]
    lines.extend(_format_member_line(r) for r in rows)
    await deps.channel.send(chat_id, "\n".join(lines), use_markdown=True)


async def _cmd_xem_nguoi_than(chat_id, body, user, deps: CoreDeps) -> None:
    if deps.family_store is None or deps.burial_store is None:
        await deps.channel.send(chat_id, _NOT_ENABLED, use_markdown=False)
        return
    body = body.strip()
    if not body:
        await deps.channel.send(
            chat_id, "Cú pháp: xem nguoi than <id hoặc tên>", use_markdown=False,
        )
        return
    member, candidates = _resolve_member(body, deps)
    if member is None:
        if candidates:
            lines = ["Có nhiều người trùng tên, chọn theo id:"]
            lines.extend(_format_member_line(r) for r in candidates)
            await deps.channel.send(chat_id, "\n".join(lines), use_markdown=False)
        else:
            await deps.channel.send(chat_id, "Không tìm thấy người thân.", use_markdown=False)
        return
    burial = deps.burial_store.get_current_for_member(member["id"])
    await deps.channel.send(
        chat_id, _format_member_detail(member, burial), use_markdown=True,
    )


async def _cmd_sua_nguoi_than(chat_id, body, user, deps: CoreDeps) -> None:
    if deps.family_store is None:
        await deps.channel.send(chat_id, _NOT_ENABLED, use_markdown=False)
        return
    if not _is_admin(user):
        await deps.channel.send(chat_id, _NO_PERMISSION, use_markdown=False)
        return
    try:
        member_id, updates = parse_edit_pairs(body)
        row = deps.family_store.update_member(member_id, **updates)
    except ValueError as e:
        await deps.channel.send(chat_id, f"⚠️ {e}", use_markdown=False)
        return
    if row is None or row["deleted_at"] is not None:
        await deps.channel.send(chat_id, "Không tìm thấy người thân.", use_markdown=False)
        return
    deps.audit.log(
        actor_user_id=user.id,
        action="family_member_updated",
        target_type="family_member",
        target_id=member_id,
        payload={"fields": sorted(updates)},
    )
    await deps.channel.send(
        chat_id, f"✅ Đã cập nhật người thân #{member_id}.", use_markdown=False,
    )


async def _cmd_xoa_nguoi_than(chat_id, body, user, deps: CoreDeps) -> None:
    if deps.family_store is None or deps.burial_store is None:
        await deps.channel.send(chat_id, _NOT_ENABLED, use_markdown=False)
        return
    if not _is_admin(user):
        await deps.channel.send(chat_id, _NO_PERMISSION, use_markdown=False)
        return
    token = body.strip()
    if not token.isdigit():
        await deps.channel.send(chat_id, "Cú pháp: xoa nguoi than: <id>", use_markdown=False)
        return
    member_id = int(token)
    row = deps.family_store.get_member(member_id)
    if row is None or row["deleted_at"] is not None:
        await deps.channel.send(chat_id, "Không tìm thấy người thân.", use_markdown=False)
        return
    if deps.burial_store.list_for_member(member_id):
        await deps.channel.send(
            chat_id,
            "⚠️ Người thân này còn bản ghi mộ phần. Hãy xóa mộ phần trước (xoa mo phan: <id>).",
            use_markdown=False,
        )
        return
    deps.family_store.soft_delete_member(member_id)
    deps.audit.log(
        actor_user_id=user.id,
        action="family_member_deleted",
        target_type="family_member",
        target_id=member_id,
        payload={"full_name": row["full_name"]},
    )
    await deps.channel.send(
        chat_id, f"✅ Đã xóa người thân #{member_id}: {row['full_name']}", use_markdown=False,
    )


async def _cmd_them_mo_phan(chat_id, body, user, deps: CoreDeps) -> None:
    if deps.family_store is None or deps.burial_store is None:
        await deps.channel.send(chat_id, _NOT_ENABLED, use_markdown=False)
        return
    if not _is_admin(user):
        await deps.channel.send(chat_id, _NO_PERMISSION, use_markdown=False)
        return
    body = body.strip()
    if not body:
        await deps.channel.send(
            chat_id,
            "Vui lòng nhập theo định dạng:\n"
            "  them mo phan: <id người thân>, <nghĩa trang>[, dia chi <text>]"
            "[, gps <lat>,<lng>][, lo <text>][, ghi chu <text>]\n"
            "Ví dụ: them mo phan: 5, Nghĩa trang Văn Điển, gps 20.9456,105.8231, lo B3 hàng 12",
            use_markdown=False,
        )
        return
    try:
        member_id, cemetery_name, fields = parse_burial_input(body)
        record = deps.burial_store.create_record(
            created_by=user.id, member_id=member_id,
            cemetery_name=cemetery_name, **fields,
        )
    except ValueError as e:
        await deps.channel.send(chat_id, f"⚠️ {e}", use_markdown=False)
        return
    deps.audit.log(
        actor_user_id=user.id,
        action="burial_record_created",
        target_type="burial_record",
        target_id=record["id"],
        payload={"member_id": member_id, "cemetery_name": cemetery_name},
    )
    await deps.channel.send(
        chat_id,
        f"✅ Đã thêm mộ phần #{record['id']} cho người thân #{member_id}: {cemetery_name}",
        use_markdown=False,
    )


_BURIAL_EDIT_FIELD_MAP = {
    "nghia trang": "cemetery_name",
    "dia chi": "address",
    "lo": "plot_info",
    "ghi chu": "note",
}


async def _cmd_sua_mo_phan(chat_id, body, user, deps: CoreDeps) -> None:
    if deps.burial_store is None:
        await deps.channel.send(chat_id, _NOT_ENABLED, use_markdown=False)
        return
    if not _is_admin(user):
        await deps.channel.send(chat_id, _NO_PERMISSION, use_markdown=False)
        return
    # GPS first — its value contains a comma, same handling as parse_burial_input.
    gps_fields: dict = {}
    gps_match = _GPS_RE.search(body)
    if gps_match:
        try:
            gps_fields = {
                "lat": float(gps_match.group(1)),
                "lng": float(gps_match.group(2)),
            }
        except ValueError:
            await deps.channel.send(
                chat_id, "⚠️ Tọa độ GPS sai. Dùng 'gps <lat>,<lng>'.", use_markdown=False,
            )
            return
        body = body[: gps_match.start()] + body[gps_match.end():]

    parts = [p.strip() for p in body.split(",") if p.strip()]
    if not parts or not parts[0].isdigit():
        await deps.channel.send(
            chat_id,
            "Cú pháp: sua mo phan: <id>[, nghia trang=<text>][, dia chi=<text>]"
            "[, lo=<text>][, ghi chu=<text>][, gps <lat>,<lng>]",
            use_markdown=False,
        )
        return
    record_id = int(parts[0])
    updates: dict = dict(gps_fields)
    for segment in parts[1:]:
        if "=" not in segment:
            await deps.channel.send(
                chat_id, f"⚠️ Mục {segment!r} sai cú pháp. Dùng <mục>=<giá trị>.", use_markdown=False,
            )
            return
        raw_key, value = segment.split("=", 1)
        key = normalize_vn(raw_key).strip()
        if key not in _BURIAL_EDIT_FIELD_MAP:
            await deps.channel.send(
                chat_id,
                f"⚠️ Mục {raw_key.strip()!r} không hợp lệ. "
                f"Các mục: {', '.join(sorted(_BURIAL_EDIT_FIELD_MAP))}, gps.",
                use_markdown=False,
            )
            return
        updates[_BURIAL_EDIT_FIELD_MAP[key]] = value.strip()
    if not updates:
        await deps.channel.send(chat_id, "Cần ít nhất một mục cần sửa.", use_markdown=False)
        return
    try:
        record = deps.burial_store.update_record(record_id, **updates)
    except ValueError as e:
        await deps.channel.send(chat_id, f"⚠️ {e}", use_markdown=False)
        return
    if record is None or record["deleted_at"] is not None:
        await deps.channel.send(chat_id, "Không tìm thấy bản ghi mộ phần.", use_markdown=False)
        return
    deps.audit.log(
        actor_user_id=user.id,
        action="burial_record_updated",
        target_type="burial_record",
        target_id=record_id,
        payload={"fields": sorted(updates)},
    )
    await deps.channel.send(chat_id, f"✅ Đã cập nhật mộ phần #{record_id}.", use_markdown=False)


async def _cmd_xoa_mo_phan(chat_id, body, user, deps: CoreDeps) -> None:
    if deps.burial_store is None:
        await deps.channel.send(chat_id, _NOT_ENABLED, use_markdown=False)
        return
    if not _is_admin(user):
        await deps.channel.send(chat_id, _NO_PERMISSION, use_markdown=False)
        return
    token = body.strip()
    if not token.isdigit():
        await deps.channel.send(chat_id, "Cú pháp: xoa mo phan: <id>", use_markdown=False)
        return
    record_id = int(token)
    record = deps.burial_store.get_record(record_id)
    if record is None or record["deleted_at"] is not None:
        await deps.channel.send(chat_id, "Không tìm thấy bản ghi mộ phần.", use_markdown=False)
        return
    deps.burial_store.soft_delete_record(record_id)
    deps.audit.log(
        actor_user_id=user.id,
        action="burial_record_deleted",
        target_type="burial_record",
        target_id=record_id,
        payload={"member_id": record["member_id"]},
    )
    await deps.channel.send(chat_id, f"✅ Đã xóa mộ phần #{record_id}.", use_markdown=False)


async def _cmd_tim_mo(chat_id, body, user, deps: CoreDeps) -> None:
    if deps.family_store is None or deps.burial_store is None:
        await deps.channel.send(chat_id, _NOT_ENABLED, use_markdown=False)
        return
    body = body.strip()
    if not body:
        await deps.channel.send(chat_id, "Cú pháp: tim mo <tên hoặc id>", use_markdown=False)
        return
    member, candidates = _resolve_member(body, deps)
    if member is None:
        if candidates:
            lines = ["Có nhiều người trùng tên, chọn theo id rồi gõ 'tim mo <id>':"]
            lines.extend(_format_member_line(r) for r in candidates)
            await deps.channel.send(chat_id, "\n".join(lines), use_markdown=False)
        else:
            await deps.channel.send(chat_id, "Không tìm thấy người thân.", use_markdown=False)
        return
    burial = deps.burial_store.get_current_for_member(member["id"])
    if burial is None:
        await deps.channel.send(
            chat_id,
            f"👤 {member['full_name']} (#{member['id']}) chưa có thông tin mộ phần.",
            use_markdown=False,
        )
        return
    lines = [f"👤 {member['full_name']} (#{member['id']})"]
    lines.extend(_format_burial(burial))
    await deps.channel.send(chat_id, "\n".join(lines), use_markdown=False)
