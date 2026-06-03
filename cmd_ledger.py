"""cmd_ledger.py — FR-9 ledger command handlers.

Commands handled:
  Entry:    chi:, thu:, ghi chep: <id>, danh sach ghi chep,
            sua ghi chep: <id> ..., huy ghi chep: <id>
  Category: xem danh muc, them danh muc: ..., xoa danh muc: <id>,
            sua danh muc: <id> <name>
  Report:   bao cao thang [YYYY-MM], bao cao nam, xem chi tieu
  Budget:   dat han muc chi: <amount>, dat muc tieu tiet kiem: <amount>,
            xem han muc
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

from deps import CoreDeps
from ledger_parser import parse_amount
from timeutils import VIETNAM_TZ

_NOT_ENABLED = "Tính năng ghi chép chi tiêu chưa được kích hoạt."
_ADMIN_ROLES = {"admin", "manager"}


def _format_vnd(amount: int) -> str:
    """Format integer VND with dot thousand separators: 1500000 → '1.500.000'."""
    return f"{amount:,.0f}".replace(",", ".")


def _current_month() -> str:
    return datetime.now(VIETNAM_TZ).strftime("%Y-%m")


def _current_year() -> str:
    return datetime.now(VIETNAM_TZ).strftime("%Y")


def _7days_ago() -> str:
    dt = datetime.now(VIETNAM_TZ) - timedelta(days=7)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _check_enabled(deps: CoreDeps) -> bool:
    return (
        deps.ledger_store is not None
        and deps.category_store is not None
        and deps.budget_store is not None
        and deps.ledger_parser is not None
        and deps.ledger_reports is not None
        and deps.credit_card_store is not None
    )


# ── Entry handlers ────────────────────────────────────────────────────────────


async def _cmd_chi(chat_id, body, user, deps: CoreDeps) -> None:
    if not _check_enabled(deps):
        await deps.channel.send(chat_id, _NOT_ENABLED, use_markdown=False)
        return
    body = body.strip()
    if not body:
        await deps.channel.send(
            chat_id,
            "⚠️ Cú pháp: chi: <số> <mô tả>\nVí dụ: chi: 50k ăn trưa",
            use_markdown=False,
        )
        return
    await _add_entry(chat_id, f"chi: {body}", user, deps)


async def _cmd_thu(chat_id, body, user, deps: CoreDeps) -> None:
    if not _check_enabled(deps):
        await deps.channel.send(chat_id, _NOT_ENABLED, use_markdown=False)
        return
    body = body.strip()
    if not body:
        await deps.channel.send(
            chat_id,
            "⚠️ Cú pháp: thu: <số> <mô tả>\nVí dụ: thu: 5tr lương",
            use_markdown=False,
        )
        return
    await _add_entry(chat_id, f"thu: {body}", user, deps)


async def _add_entry(chat_id, text, user, deps: CoreDeps, *, credit_card_id=None) -> None:
    categories = deps.category_store.list_for_user(user.id)
    try:
        parsed = deps.ledger_parser.parse_command(text, categories)
    except ValueError as e:
        await deps.channel.send(chat_id, f"⚠️ {e}", use_markdown=False)
        return

    now_vn = datetime.now(VIETNAM_TZ).strftime("%Y-%m-%d %H:%M:%S")
    entry = deps.ledger_store.add_entry(
        user.id,
        parsed["kind"],
        parsed["amount"],
        now_vn,
        category_id=parsed["category_id"],
        note=parsed["description"] or None,
        source="telegram",
        credit_card_id=credit_card_id,
    )
    deps.audit.log(
        actor_user_id=user.id,
        action="ledger_created",
        target_type="ledger_entry",
        target_id=entry["id"],
        payload={"kind": entry["kind"], "amount": entry["amount"]},
    )

    kind_label = "Chi" if parsed["kind"] == "expense" else "Thu"
    await deps.channel.send(
        chat_id,
        f"✅ Đã ghi #{entry['id']}: {kind_label} {_format_vnd(entry['amount'])} đ"
        + (f" — {parsed['description']}" if parsed["description"] else ""),
        use_markdown=False,
    )

    # Threshold alert after expense
    if parsed["kind"] == "expense":
        month = now_vn[:7]
        threshold = deps.ledger_reports.check_threshold(user.id, month)
        if threshold is not None:
            deps.budget_store.mark_alert_sent(user.id, month, threshold)
            pct = "80%" if threshold == "80" else "100%"
            if deps.notification_service:
                deps.notification_service.enqueue(
                    user_id=user.id,
                    chat_id=chat_id,
                    message=f"⚠️ Chi tiêu tháng này đã đạt {pct} hạn mức!",
                )


async def _cmd_ghi_chep_xem(chat_id, body, user, deps: CoreDeps) -> None:
    if not _check_enabled(deps):
        await deps.channel.send(chat_id, _NOT_ENABLED, use_markdown=False)
        return
    token = body.strip().split()[0] if body.strip() else ""
    if not token.isdigit():
        await deps.channel.send(chat_id, "⚠️ Cú pháp: ghi chep: <id>", use_markdown=False)
        return
    entry = deps.ledger_store.get_entry(int(token))
    if entry is None or entry["user_id"] != user.id or entry["voided_at"] is not None:
        await deps.channel.send(chat_id, "Không tìm thấy bút toán.", use_markdown=False)
        return
    kind_label = "Chi" if entry["kind"] == "expense" else "Thu"
    text = (
        f"#{entry['id']} {kind_label} {_format_vnd(entry['amount'])} đ\n"
        f"Ngày: {entry['occurred_at'][:10]}\n"
        + (f"Ghi chú: {entry['note']}" if entry["note"] else "")
    )
    await deps.channel.send(chat_id, text, use_markdown=False)


async def _cmd_danh_sach_ghi_chep(chat_id, user, deps: CoreDeps) -> None:
    if not _check_enabled(deps):
        await deps.channel.send(chat_id, _NOT_ENABLED, use_markdown=False)
        return
    entries = deps.ledger_store.list_for_user(user.id, limit=20)
    if not entries:
        await deps.channel.send(chat_id, "Chưa có bút toán nào.", use_markdown=False)
        return
    lines = ["📋 Ghi chép gần đây:"]
    for e in entries:
        sign = "−" if e["kind"] == "expense" else "+"
        lines.append(f"  #{e['id']} {sign}{_format_vnd(e['amount'])} — {e['occurred_at'][:10]}")
    await deps.channel.send(chat_id, "\n".join(lines), use_markdown=False)


async def _cmd_sua_ghi_chep(chat_id, body, user, deps: CoreDeps) -> None:
    if not _check_enabled(deps):
        await deps.channel.send(chat_id, _NOT_ENABLED, use_markdown=False)
        return
    # Format: "<id>, so=<amount>[, mo ta=<text>][, danh muc=<id>]"
    parts = [p.strip() for p in body.split(",", 1)]
    if not parts[0].isdigit():
        await deps.channel.send(
            chat_id, "⚠️ Cú pháp: sua ghi chep: <id>, so=<số>[, mo ta=<text>][, danh muc=<id>]",
            use_markdown=False,
        )
        return
    entry_id = int(parts[0])
    entry = deps.ledger_store.get_entry(entry_id)
    if entry is None or entry["user_id"] != user.id:
        await deps.channel.send(chat_id, "Không tìm thấy bút toán.", use_markdown=False)
        return

    updates: dict = {}
    if len(parts) > 1:
        for kv in parts[1].split(","):
            kv = kv.strip()
            if "=" not in kv:
                continue
            key, val = kv.split("=", 1)
            key, val = key.strip().lower(), val.strip()
            if key == "so":
                try:
                    updates["amount"] = parse_amount(val)
                except ValueError as e:
                    await deps.channel.send(chat_id, f"⚠️ {e}", use_markdown=False)
                    return
            elif key in ("mo ta", "ghi chu"):
                updates["note"] = val
            elif key == "danh muc":
                if val.isdigit():
                    updates["category_id"] = int(val)

    deps.ledger_store.update_entry(entry_id, **updates)
    deps.audit.log(
        actor_user_id=user.id,
        action="ledger_updated",
        target_type="ledger_entry",
        target_id=entry_id,
        payload=updates,
    )
    await deps.channel.send(chat_id, f"✅ Đã cập nhật bút toán #{entry_id}.", use_markdown=False)


async def _cmd_huy_ghi_chep(chat_id, body, user, deps: CoreDeps) -> None:
    if not _check_enabled(deps):
        await deps.channel.send(chat_id, _NOT_ENABLED, use_markdown=False)
        return
    token = body.strip().split()[0] if body.strip() else ""
    if not token.isdigit():
        await deps.channel.send(chat_id, "⚠️ Cú pháp: huy ghi chep: <id>", use_markdown=False)
        return
    entry_id = int(token)
    entry = deps.ledger_store.get_entry(entry_id)
    if entry is None or entry["user_id"] != user.id:
        await deps.channel.send(chat_id, "Không tìm thấy bút toán.", use_markdown=False)
        return
    deps.ledger_store.void_entry(entry_id)
    deps.audit.log(
        actor_user_id=user.id,
        action="ledger_voided",
        target_type="ledger_entry",
        target_id=entry_id,
        payload={},
    )
    await deps.channel.send(chat_id, f"✅ Đã hủy bút toán #{entry_id}.", use_markdown=False)


# ── Category handlers ─────────────────────────────────────────────────────────


async def _cmd_xem_danh_muc(chat_id, user, deps: CoreDeps) -> None:
    if not _check_enabled(deps):
        await deps.channel.send(chat_id, _NOT_ENABLED, use_markdown=False)
        return
    cats = deps.category_store.list_for_user(user.id)
    if not cats:
        await deps.channel.send(chat_id, "Chưa có danh mục nào.", use_markdown=False)
        return
    lines = ["📂 Danh mục:"]
    for c in cats:
        shared = " (chung)" if c["user_id"] is None else ""
        kind_label = "Chi" if c["kind"] == "expense" else "Thu"
        lines.append(f"  #{c['id']} [{kind_label}] {c['name']}{shared}")
    await deps.channel.send(chat_id, "\n".join(lines), use_markdown=False)


async def _cmd_them_danh_muc(chat_id, body, user, deps: CoreDeps) -> None:
    if not _check_enabled(deps):
        await deps.channel.send(chat_id, _NOT_ENABLED, use_markdown=False)
        return
    parts = [p.strip() for p in body.split(",")]
    if len(parts) < 2:
        await deps.channel.send(
            chat_id,
            "⚠️ Cú pháp: them danh muc: <tên>, chi|thu[, chung]",
            use_markdown=False,
        )
        return

    name = parts[0]
    kind_raw = parts[1].strip().lower()
    kind_map = {"chi": "expense", "thu": "income", "expense": "expense", "income": "income"}
    kind = kind_map.get(kind_raw)
    if kind is None:
        await deps.channel.send(
            chat_id, "⚠️ Loại danh mục phải là 'chi' hoặc 'thu'.", use_markdown=False,
        )
        return

    is_shared = len(parts) >= 3 and parts[2].strip().lower() in ("chung", "shared")
    if is_shared and getattr(user, "role", None) not in _ADMIN_ROLES:
        await deps.channel.send(
            chat_id,
            "⚠️ Bạn không có quyền tạo danh mục chung. Chỉ admin/manager mới tạo được.",
            use_markdown=False,
        )
        return

    cat = deps.category_store.create_category(
        name, kind, user_id=None if is_shared else user.id,
    )
    deps.audit.log(
        actor_user_id=user.id,
        action="category_created",
        target_type="category",
        target_id=cat["id"],
        payload={"name": name, "kind": kind, "shared": is_shared},
    )
    await deps.channel.send(
        chat_id, f"✅ Đã thêm danh mục #{cat['id']}: {cat['name']}.", use_markdown=False,
    )


async def _cmd_xoa_danh_muc(chat_id, body, user, deps: CoreDeps) -> None:
    if not _check_enabled(deps):
        await deps.channel.send(chat_id, _NOT_ENABLED, use_markdown=False)
        return
    token = body.strip().split()[0] if body.strip() else ""
    if not token.isdigit():
        await deps.channel.send(chat_id, "⚠️ Cú pháp: xoa danh muc: <id>", use_markdown=False)
        return
    cat_id = int(token)
    cat = deps.category_store.get_category(cat_id)
    is_admin = getattr(user, "role", None) in _ADMIN_ROLES
    if cat is None or cat["deleted_at"] is not None:
        await deps.channel.send(chat_id, "Không tìm thấy danh mục.", use_markdown=False)
        return
    if cat["user_id"] != user.id and not is_admin:
        await deps.channel.send(chat_id, "⚠️ Bạn không có quyền xóa danh mục này.", use_markdown=False)
        return
    deps.category_store.soft_delete_category(cat_id)
    deps.audit.log(
        actor_user_id=user.id,
        action="category_deleted",
        target_type="category",
        target_id=cat_id,
        payload={},
    )
    await deps.channel.send(chat_id, f"✅ Đã xóa danh mục #{cat_id}.", use_markdown=False)


async def _cmd_sua_danh_muc(chat_id, body, user, deps: CoreDeps) -> None:
    if not _check_enabled(deps):
        await deps.channel.send(chat_id, _NOT_ENABLED, use_markdown=False)
        return
    parts = body.strip().split(None, 1)
    if len(parts) < 2 or not parts[0].isdigit():
        await deps.channel.send(
            chat_id, "⚠️ Cú pháp: sua danh muc: <id> <tên mới>", use_markdown=False,
        )
        return
    cat_id = int(parts[0])
    new_name = parts[1].strip()
    cat = deps.category_store.get_category(cat_id)
    is_admin = getattr(user, "role", None) in _ADMIN_ROLES
    if cat is None or cat["deleted_at"] is not None:
        await deps.channel.send(chat_id, "Không tìm thấy danh mục.", use_markdown=False)
        return
    if cat["user_id"] != user.id and not is_admin:
        await deps.channel.send(chat_id, "⚠️ Bạn không có quyền sửa danh mục này.", use_markdown=False)
        return
    deps.category_store.update_category(cat_id, name=new_name)
    deps.audit.log(
        actor_user_id=user.id,
        action="category_updated",
        target_type="category",
        target_id=cat_id,
        payload={"name": new_name},
    )
    await deps.channel.send(chat_id, f"✅ Đã đổi tên danh mục #{cat_id} → {new_name}.", use_markdown=False)


# ── Report handlers ───────────────────────────────────────────────────────────


async def _cmd_bao_cao_thang(chat_id, body, user, deps: CoreDeps) -> None:
    if not _check_enabled(deps):
        await deps.channel.send(chat_id, _NOT_ENABLED, use_markdown=False)
        return
    month = body.strip() if re.match(r"^\d{4}-\d{2}$", body.strip()) else _current_month()
    summary = deps.ledger_reports.monthly_summary(user.id, month)

    lines = [
        f"📊 Tháng {month}",
        "─────────────────────",
        f"💰 Thu:    {_format_vnd(summary['income'])}",
        f"💸 Chi:    {_format_vnd(summary['expense'])}",
        f"💵 Tiết kiệm: {_format_vnd(summary['savings'])}",
    ]
    if summary["expense_budget"]:
        pct = summary["budget_pct"] or 0
        lines.append(f"\nHạn mức chi: {_format_vnd(summary['expense'])} / {_format_vnd(summary['expense_budget'])} ({pct}%)")

    expense_rows = [r for r in summary["by_category"] if r["kind"] == "expense"]
    if expense_rows:
        cat_map = {
            c["id"]: c["name"]
            for c in deps.category_store.list_for_user(user.id)
        }
        total_expense = summary["expense"] or 1  # guard against zero division
        lines.append("\nTheo danh mục (chi):")
        for row in expense_rows:
            name = cat_map.get(row["category_id"], "Chưa phân loại") if row["category_id"] else "Chưa phân loại"
            pct = int(row["total"] / total_expense * 100)
            lines.append(f"  {name}  {_format_vnd(row['total'])}  ({pct}%)")

    await deps.channel.send(chat_id, "\n".join(lines), use_markdown=False)


async def _cmd_bao_cao_nam(chat_id, user, deps: CoreDeps) -> None:
    if not _check_enabled(deps):
        await deps.channel.send(chat_id, _NOT_ENABLED, use_markdown=False)
        return
    year = _current_year()
    rows = deps.ledger_reports.yearly_breakdown(user.id, year)
    lines = [f"📊 Năm {year}", "─────────────────────"]
    for r in rows:
        lines.append(
            f"  {r['month']}  Thu {_format_vnd(r['income'])}  Chi {_format_vnd(r['expense'])}"
        )
    await deps.channel.send(chat_id, "\n".join(lines), use_markdown=False)


async def _cmd_xem_chi_tieu(chat_id, user, deps: CoreDeps) -> None:
    if not _check_enabled(deps):
        await deps.channel.send(chat_id, _NOT_ENABLED, use_markdown=False)
        return
    since = _7days_ago()
    result = deps.ledger_reports.last_7_days(user.id, since)
    cat_map = {
        c["id"]: c["name"]
        for c in deps.category_store.list_for_user(user.id)
    }
    _KIND_LABEL = {"expense": "Chi", "income": "Thu", "cc_payment": "↩ Trả thẻ"}
    lines = [
        "📋 Chi tiêu 7 ngày qua:",
        f"💸 Tổng chi: {_format_vnd(result['total_expense'])}",
        f"💰 Tổng thu: {_format_vnd(result['total_income'])}",
        "─────────────────────",
    ]
    for e in result["entries"]:
        sign = "+" if e["kind"] == "income" else "−"
        label = _KIND_LABEL.get(e["kind"], e["kind"])
        cat_name = cat_map.get(e["category_id"], "") if e.get("category_id") else ""
        note = e["note"] or ""
        detail = "  —  ".join(filter(None, [note, f"[{cat_name}]" if cat_name else ""]))
        line = f"  {e['occurred_at'][:10]}  {label}  {sign}{_format_vnd(e['amount'])}"
        if detail:
            line += f"  {detail}"
        lines.append(line)
    await deps.channel.send(chat_id, "\n".join(lines), use_markdown=False)


# ── Budget handlers ───────────────────────────────────────────────────────────


async def _cmd_dat_han_muc_chi(chat_id, body, user, deps: CoreDeps) -> None:
    if not _check_enabled(deps):
        await deps.channel.send(chat_id, _NOT_ENABLED, use_markdown=False)
        return
    try:
        amount = parse_amount(body.strip())
    except ValueError as e:
        await deps.channel.send(chat_id, f"⚠️ {e}", use_markdown=False)
        return
    month = _current_month()
    deps.budget_store.upsert_budget(user.id, month, expense_budget=amount)
    await deps.channel.send(
        chat_id,
        f"✅ Đã đặt hạn mức chi tháng {month}: {_format_vnd(amount)} đ.",
        use_markdown=False,
    )


async def _cmd_dat_muc_tieu_tiet_kiem(chat_id, body, user, deps: CoreDeps) -> None:
    if not _check_enabled(deps):
        await deps.channel.send(chat_id, _NOT_ENABLED, use_markdown=False)
        return
    try:
        amount = parse_amount(body.strip())
    except ValueError as e:
        await deps.channel.send(chat_id, f"⚠️ {e}", use_markdown=False)
        return
    month = _current_month()
    deps.budget_store.upsert_budget(user.id, month, savings_target=amount)
    await deps.channel.send(
        chat_id,
        f"✅ Đã đặt mục tiêu tiết kiệm tháng {month}: {_format_vnd(amount)} đ.",
        use_markdown=False,
    )


async def _cmd_xem_han_muc(chat_id, user, deps: CoreDeps) -> None:
    if not _check_enabled(deps):
        await deps.channel.send(chat_id, _NOT_ENABLED, use_markdown=False)
        return
    month = _current_month()
    budget = deps.budget_store.get_budget(user.id, month)
    summary = deps.ledger_reports.monthly_summary(user.id, month)

    lines = [f"💰 Hạn mức tháng {month}:"]
    if budget and budget["expense_budget"]:
        pct = summary["budget_pct"] or 0
        lines.append(
            f"  Chi: {_format_vnd(summary['expense'])} / {_format_vnd(budget['expense_budget'])} ({pct}%)"
        )
    else:
        lines.append("  Chưa đặt hạn mức chi.")

    if budget and budget["savings_target"]:
        lines.append(
            f"  Tiết kiệm: {_format_vnd(summary['savings'])} / {_format_vnd(budget['savings_target'])}"
        )
    else:
        lines.append("  Chưa đặt mục tiêu tiết kiệm.")

    await deps.channel.send(chat_id, "\n".join(lines), use_markdown=False)


# ── Credit card handlers ──────────────────────────────────────────────────────


async def _cmd_them_the(chat_id, body, user, deps: CoreDeps) -> None:
    if not _check_enabled(deps):
        await deps.channel.send(chat_id, _NOT_ENABLED, use_markdown=False)
        return
    name = body.strip()
    if not name:
        await deps.channel.send(
            chat_id,
            "⚠️ Cú pháp: them the: <tên thẻ>\nVí dụ: them the: Visa ABC",
            use_markdown=False,
        )
        return
    card = deps.credit_card_store.create_card(name, user_id=user.id)
    deps.audit.log(
        actor_user_id=user.id,
        action="credit_card_created",
        target_type="credit_card",
        target_id=card["id"],
        payload={"name": card["name"]},
    )
    await deps.channel.send(
        chat_id, f"✅ Đã thêm thẻ #{card['id']}: {card['name']}.", use_markdown=False,
    )


async def _cmd_xem_the(chat_id, user, deps: CoreDeps) -> None:
    if not _check_enabled(deps):
        await deps.channel.send(chat_id, _NOT_ENABLED, use_markdown=False)
        return
    cards = deps.credit_card_store.list_for_user(user.id)
    if not cards:
        await deps.channel.send(
            chat_id,
            "Chưa có thẻ tín dụng nào. Thêm bằng: them the: <tên thẻ>",
            use_markdown=False,
        )
        return
    outstanding = deps.ledger_store.all_card_outstanding(user.id)
    lines = ["💳 Thẻ tín dụng:"]
    for c in cards:
        shared = " (chung)" if c["user_id"] is None else ""
        due = outstanding.get(c["id"], 0)
        lines.append(f"  #{c['id']} {c['name']}{shared} — dư nợ: {_format_vnd(due)} đ")
    await deps.channel.send(chat_id, "\n".join(lines), use_markdown=False)


async def _cmd_xoa_the(chat_id, body, user, deps: CoreDeps) -> None:
    if not _check_enabled(deps):
        await deps.channel.send(chat_id, _NOT_ENABLED, use_markdown=False)
        return
    token = body.strip().split()[0] if body.strip() else ""
    if not token.isdigit():
        await deps.channel.send(chat_id, "⚠️ Cú pháp: xoa the: <id>", use_markdown=False)
        return
    card_id = int(token)
    card = deps.credit_card_store.get_card(card_id)
    is_admin = getattr(user, "role", None) in _ADMIN_ROLES
    if card is None or card["deleted_at"] is not None:
        await deps.channel.send(chat_id, "Không tìm thấy thẻ.", use_markdown=False)
        return
    if card["user_id"] != user.id and not is_admin:
        await deps.channel.send(chat_id, "⚠️ Bạn không có quyền xóa thẻ này.", use_markdown=False)
        return
    deps.credit_card_store.soft_delete_card(card_id)
    deps.audit.log(
        actor_user_id=user.id,
        action="credit_card_deleted",
        target_type="credit_card",
        target_id=card_id,
        payload={},
    )
    await deps.channel.send(chat_id, f"✅ Đã xóa thẻ #{card_id}.", use_markdown=False)


async def _cmd_chi_the(chat_id, body, user, deps: CoreDeps) -> None:
    if not _check_enabled(deps):
        await deps.channel.send(chat_id, _NOT_ENABLED, use_markdown=False)
        return
    if ":" not in body:
        await deps.channel.send(
            chat_id,
            "⚠️ Cú pháp: chi the <tên thẻ>: <số> <mô tả>\nVí dụ: chi the Visa: 50k ăn trưa",
            use_markdown=False,
        )
        return
    card_name, rest = body.split(":", 1)
    card_name, rest = card_name.strip(), rest.strip()
    card = deps.credit_card_store.get_card_by_name(user.id, card_name)
    if card is None:
        await deps.channel.send(
            chat_id,
            f"⚠️ Không tìm thấy thẻ '{card_name}'. Thêm bằng: them the: {card_name}",
            use_markdown=False,
        )
        return
    await _add_entry(chat_id, f"chi: {rest}", user, deps, credit_card_id=card["id"])


async def _cmd_tra_the(chat_id, body, user, deps: CoreDeps) -> None:
    if not _check_enabled(deps):
        await deps.channel.send(chat_id, _NOT_ENABLED, use_markdown=False)
        return
    if ":" not in body:
        await deps.channel.send(
            chat_id,
            "⚠️ Cú pháp: tra the <tên thẻ>: <số>\nVí dụ: tra the Visa: 5tr",
            use_markdown=False,
        )
        return
    card_name, amount_str = body.split(":", 1)
    card_name, amount_str = card_name.strip(), amount_str.strip()
    card = deps.credit_card_store.get_card_by_name(user.id, card_name)
    if card is None:
        await deps.channel.send(
            chat_id,
            f"⚠️ Không tìm thấy thẻ '{card_name}'. Thêm bằng: them the: {card_name}",
            use_markdown=False,
        )
        return
    try:
        amount = parse_amount(amount_str)
    except ValueError as e:
        await deps.channel.send(chat_id, f"⚠️ {e}", use_markdown=False)
        return

    now_vn = datetime.now(VIETNAM_TZ).strftime("%Y-%m-%d %H:%M:%S")
    entry = deps.ledger_store.add_entry(
        user.id,
        "cc_payment",
        amount,
        now_vn,
        note=f"Trả thẻ {card['name']}",
        source="telegram",
        credit_card_id=card["id"],
    )
    deps.audit.log(
        actor_user_id=user.id,
        action="ledger_created",
        target_type="ledger_entry",
        target_id=entry["id"],
        payload={"kind": "cc_payment", "amount": amount, "credit_card_id": card["id"]},
    )
    due = deps.ledger_store.card_outstanding(user.id, card["id"])
    await deps.channel.send(
        chat_id,
        f"✅ Đã trả thẻ {card['name']}: {_format_vnd(amount)} đ.\n"
        f"Dư nợ còn lại: {_format_vnd(due)} đ.",
        use_markdown=False,
    )
