"""cmd_task.py — FR-7 task command handlers + daily summary + callback dispatcher.

Covers: tao_task, xong_task, huy_task, danh_sach_task, xem_task, lich_hoc,
hoan_task, tom_tat_hom_nay, cau_hinh_tong_ket, cau_hinh_gio_mac_dinh,
plus _handle_callback for inline keyboard callback_query dispatch.
"""
from cmd_utils import _norm
from deps import CoreDeps
from interfaces import ChannelMessage, User
from task_parser import ParseError


def _parse_task_id(body: str) -> int | None:
    """Parse the first token of body as a positive integer task id."""
    token = body.strip().split()[0] if body.strip() else ""
    if token.isdigit():
        return int(token)
    return None


def _format_task_list(tasks: list[dict]) -> str:
    """Format a list of pending tasks for display."""
    if not tasks:
        return "Không có task nào đang chờ."
    lines = ["📋 *Danh sách task:*"]
    for t in tasks:
        dl = t.get("deadline", "")[:16].replace("T", " ")
        cat_icon = {"study": "📚", "reminder": "🔔"}.get(t.get("category", "task"), "✅")
        lines.append(f"{cat_icon} #{t['id']} {t['title']} — {dl}")
    return "\n".join(lines)


def _format_task_detail(task: dict) -> str:
    """Format a single task for display."""
    dl = task.get("deadline", "")[:16].replace("T", " ")
    cat_icon = {"study": "📚", "reminder": "🔔"}.get(task.get("category", "task"), "✅")
    lines = [
        f"{cat_icon} *Task #{task['id']}*",
        f"📝 {task['title']}",
        f"📅 Deadline: {dl}",
        f"🏷 Category: {task.get('category', 'task')}",
        f"⚡ Status: {task.get('status', '?')}",
    ]
    if task.get("recurring_rule"):
        lines.append(f"🔁 Lặp lại: {task['recurring_rule']}")
    if task.get("snooze_count"):
        lines.append(f"😴 Đã hoãn: {task['snooze_count']} lần")
    return "\n".join(lines)


async def _cmd_tao_task(
    chat_id: str, body: str, user, deps: CoreDeps,
) -> None:
    """tao task: <free-form> — parse and create a new task via LLM."""
    if deps.task_parser is None or deps.task_store is None:
        await deps.channel.send(
            chat_id, "Tính năng task chưa được kích hoạt.", use_markdown=False,
        )
        return
    if not body:
        await deps.channel.send(
            chat_id,
            "Vui lòng mô tả task sau lệnh, ví dụ:\n"
            "  task: mua sữa 5h chiều mai",
            use_markdown=False,
        )
        return

    try:
        from datetime import datetime
        from timeutils import VIETNAM_TZ
        parsed = deps.task_parser.parse(body, now=datetime.now(VIETNAM_TZ))
    except ParseError as e:
        await deps.channel.send(chat_id, str(e), use_markdown=False)
        return

    morning_default = deps.user_store.get_morning_default_time(user.id) or "09:00"
    # Re-parse with the user's personal morning default if available.
    try:
        from datetime import datetime
        from timeutils import VIETNAM_TZ
        parsed = deps.task_parser.parse(
            body, morning_default=morning_default, now=datetime.now(VIETNAM_TZ),
        )
    except ParseError as e:
        await deps.channel.send(chat_id, str(e), use_markdown=False)
        return

    task = deps.task_store.create_task(
        user_id=user.id,
        title=parsed.title,
        deadline=parsed.deadline_iso,
        category=parsed.category,
        recurring_rule=parsed.recurring_rule,
    )

    if deps.reminder_engine is not None:
        deps.reminder_engine.schedule_for_task(task)

    dl_display = parsed.deadline_iso[:16].replace("T", " ")
    reply_lines = [f"✅ Đã tạo task #{task['id']}: *{parsed.title}*", f"📅 {dl_display}"]
    if parsed.recurring_rule:
        reply_lines.append(f"🔁 {parsed.recurring_rule}")
    buttons = [[
        {"text": "✅ Xong", "callback_data": f"done:{task['id']}"},
        {"text": "😴 Hoãn 15p", "callback_data": f"snooze:{task['id']}:15"},
        {"text": "⏰ Hoãn 1h", "callback_data": f"snooze:{task['id']}:60"},
    ]]
    await deps.channel.send_with_inline_keyboard(chat_id, "\n".join(reply_lines), buttons)


async def _cmd_xong_task(
    chat_id: str, body: str, user, deps: CoreDeps,
) -> None:
    """xong task: <id> — mark a task as completed."""
    if deps.task_store is None:
        await deps.channel.send(
            chat_id, "Tính năng task chưa được kích hoạt.", use_markdown=False,
        )
        return

    task_id = _parse_task_id(body)
    if task_id is None:
        await deps.channel.send(
            chat_id, "Cú pháp: xong task: <id>  ví dụ: xong task: 5", use_markdown=False,
        )
        return

    task = deps.task_store.get_task(task_id)
    if task is None or task.get("user_id") != user.id:
        await deps.channel.send(
            chat_id, f"Không tìm thấy task #{task_id}.", use_markdown=False,
        )
        return

    from datetime import datetime
    from timeutils import VIETNAM_TZ
    now_iso = datetime.now(VIETNAM_TZ).strftime("%Y-%m-%d %H:%M:%S")
    deps.task_store.complete_task(task_id, completed_at=now_iso)
    if deps.reminder_engine is not None:
        deps.reminder_engine.cancel_all_for_task(task_id)

    await deps.channel.send(
        chat_id, f"✅ Đã hoàn thành task #{task_id}: {task['title']}", use_markdown=False,
    )


async def _cmd_huy_task(
    chat_id: str, body: str, user, deps: CoreDeps,
) -> None:
    """huy task: <id> — cancel a pending task."""
    if deps.task_store is None:
        await deps.channel.send(
            chat_id, "Tính năng task chưa được kích hoạt.", use_markdown=False,
        )
        return

    task_id = _parse_task_id(body)
    if task_id is None:
        await deps.channel.send(
            chat_id, "Cú pháp: huy task: <id>  ví dụ: huy task: 5", use_markdown=False,
        )
        return

    task = deps.task_store.get_task(task_id)
    if task is None or task.get("user_id") != user.id:
        await deps.channel.send(
            chat_id, f"Không tìm thấy task #{task_id}.", use_markdown=False,
        )
        return

    deps.task_store.cancel_task(task_id)
    if deps.reminder_engine is not None:
        deps.reminder_engine.cancel_all_for_task(task_id)

    await deps.channel.send(
        chat_id, f"🚫 Đã hủy task #{task_id}: {task['title']}", use_markdown=False,
    )


async def _cmd_danh_sach_task(
    chat_id: str, user, deps: CoreDeps,
) -> None:
    """danh sach task — list all pending tasks for the current user."""
    if deps.task_store is None:
        await deps.channel.send(
            chat_id, "Tính năng task chưa được kích hoạt.", use_markdown=False,
        )
        return

    tasks = deps.task_store.list_for_user(user.id, status="pending")
    await deps.channel.send(chat_id, _format_task_list(tasks))


async def _cmd_xem_task(
    chat_id: str, body: str, user, deps: CoreDeps,
) -> None:
    """task <id> — show task detail."""
    if deps.task_store is None:
        await deps.channel.send(
            chat_id, "Tính năng task chưa được kích hoạt.", use_markdown=False,
        )
        return

    task_id = _parse_task_id(body)
    if task_id is None:
        await deps.channel.send(
            chat_id, "Cú pháp: task <id>  ví dụ: task 5", use_markdown=False,
        )
        return

    task = deps.task_store.get_task(task_id)
    if task is None or task.get("user_id") != user.id:
        await deps.channel.send(
            chat_id, f"Không tìm thấy task #{task_id}.", use_markdown=False,
        )
        return

    await deps.channel.send(chat_id, _format_task_detail(task))


async def _cmd_lich_hoc(
    chat_id: str, body: str, user, deps: CoreDeps,
) -> None:
    """lich hoc: <free-form> — shortcut to create a study task (category forced to 'study')."""
    if deps.task_parser is None or deps.task_store is None:
        await deps.channel.send(
            chat_id, "Tính năng task chưa được kích hoạt.", use_markdown=False,
        )
        return
    if not body:
        await deps.channel.send(
            chat_id,
            "Vui lòng mô tả lịch học sau lệnh, ví dụ:\n"
            "  lich hoc: toán thứ 2-6 lúc 7h",
            use_markdown=False,
        )
        return

    morning_default = deps.user_store.get_morning_default_time(user.id) or "09:00"
    try:
        from datetime import datetime
        from timeutils import VIETNAM_TZ
        parsed = deps.task_parser.parse(
            body, morning_default=morning_default, now=datetime.now(VIETNAM_TZ),
        )
    except ParseError as e:
        await deps.channel.send(chat_id, str(e), use_markdown=False)
        return

    task = deps.task_store.create_task(
        user_id=user.id,
        title=parsed.title,
        deadline=parsed.deadline_iso,
        category="study",  # forced regardless of LLM choice
        recurring_rule=parsed.recurring_rule,
    )

    if deps.reminder_engine is not None:
        deps.reminder_engine.schedule_for_task(task)

    dl_display = parsed.deadline_iso[:16].replace("T", " ")
    reply_lines = [f"📚 Đã thêm lịch học #{task['id']}: *{parsed.title}*", f"📅 {dl_display}"]
    if parsed.recurring_rule:
        reply_lines.append(f"🔁 {parsed.recurring_rule}")
    buttons = [[
        {"text": "✅ Xong", "callback_data": f"done:{task['id']}"},
        {"text": "😴 Hoãn 15p", "callback_data": f"snooze:{task['id']}:15"},
        {"text": "⏰ Hoãn 1h", "callback_data": f"snooze:{task['id']}:60"},
    ]]
    await deps.channel.send_with_inline_keyboard(chat_id, "\n".join(reply_lines), buttons)


async def _cmd_hoan_task(
    chat_id: str, body: str, user, deps: CoreDeps,
) -> None:
    """hoan task: <id> <minutes> — snooze a task by N minutes."""
    if deps.task_store is None or deps.reminder_engine is None:
        await deps.channel.send(
            chat_id, "Tính năng task chưa được kích hoạt.", use_markdown=False,
        )
        return

    parts = body.strip().split()
    if len(parts) < 2 or not parts[0].isdigit() or not parts[1].isdigit():
        await deps.channel.send(
            chat_id,
            "Cú pháp: hoan task: <id> <phút>  ví dụ: hoan task: 5 30",
            use_markdown=False,
        )
        return

    task_id = int(parts[0])
    minutes = int(parts[1])

    task = deps.task_store.get_task(task_id)
    if task is None or task.get("user_id") != user.id:
        await deps.channel.send(
            chat_id, f"Không tìm thấy task #{task_id}.", use_markdown=False,
        )
        return

    try:
        deps.reminder_engine.snooze(task_id, minutes)
    except ValueError as e:
        await deps.channel.send(chat_id, str(e), use_markdown=False)
        return

    await deps.channel.send(
        chat_id,
        f"😴 Đã hoãn task #{task_id}: {task['title']} thêm {minutes} phút.",
        use_markdown=False,
    )


async def _cmd_tom_tat_hom_nay(chat_id: str, user, deps: CoreDeps) -> None:
    """tom tat hom nay — on-demand daily task summary for the current user."""
    if deps.task_store is None:
        await deps.channel.send(chat_id, "Tính năng task chưa được kích hoạt.", use_markdown=False)
        return

    from datetime import datetime
    from timeutils import VIETNAM_TZ
    now = datetime.now(VIETNAM_TZ)
    today_str = now.strftime("%Y-%m-%d")
    today_end = f"{today_str}T23:59:59+07:00"

    completed = deps.task_store.list_completed_on(user.id, today_str)
    pending = deps.task_store.list_pending_due(today_end, user_id=user.id)

    date_display = now.strftime("%d/%m")
    lines = [
        f"Tổng kết hôm nay [{date_display}]:",
        f"✅ Đã xong: {len(completed)} task",
        f"⏰ Còn lại hôm nay: {len(pending)} task",
        "",
        "Gõ 'danh sach task' để xem chi tiết.",
    ]
    await deps.channel.send(chat_id, "\n".join(lines), use_markdown=False)


async def _cmd_cau_hinh_tong_ket(chat_id: str, body: str, user, deps: CoreDeps) -> None:
    """cau hinh tong ket: <HH:MM|tắt> — configure or disable daily summary time."""
    value = body.strip()
    if not value:
        await deps.channel.send(
            chat_id,
            "Cú pháp:\n  cấu hình tổng kết: 21:00\n  cấu hình tổng kết: tắt",
            use_markdown=False,
        )
        return

    if _norm(value) in {"tắt", "tat", "off", "disable"}:
        deps.user_store.set_daily_summary_time(user.id, "off")
        await deps.channel.send(chat_id, "Đã tắt tổng kết hàng ngày.", use_markdown=False)
        return

    try:
        deps.user_store.set_daily_summary_time(user.id, value)
        await deps.channel.send(
            chat_id, f"Đã đặt giờ tổng kết hàng ngày: {value}.", use_markdown=False,
        )
    except ValueError:
        await deps.channel.send(
            chat_id,
            "Giờ không hợp lệ. Dùng định dạng HH:MM (ví dụ: 21:00) hoặc 'tắt' để tắt.",
            use_markdown=False,
        )


async def _cmd_cau_hinh_gio_mac_dinh(chat_id: str, body: str, user, deps: CoreDeps) -> None:
    """cau hinh gio mac dinh: <HH:MM> — configure morning default time for deadline-less tasks."""
    value = body.strip()
    if not value:
        await deps.channel.send(
            chat_id, "Cú pháp: cấu hình giờ mặc định: 09:00", use_markdown=False,
        )
        return

    try:
        deps.user_store.set_morning_default_time(user.id, value)
        await deps.channel.send(
            chat_id, f"Đã đặt giờ mặc định buổi sáng: {value}.", use_markdown=False,
        )
    except ValueError:
        await deps.channel.send(
            chat_id,
            "Giờ không hợp lệ. Dùng định dạng HH:MM (ví dụ: 09:00).",
            use_markdown=False,
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Study schedule commands
# ═══════════════════════════════════════════════════════════════════════════════

_DAY_VI = {
    "MON": "Thứ 2", "TUE": "Thứ 3", "WED": "Thứ 4",
    "THU": "Thứ 5", "FRI": "Thứ 6", "SAT": "Thứ 7", "SUN": "CN",
}


def _format_recurring_rule(rule: str | None) -> str:
    """Convert 'weekly:MON,WED@17:30' or 'daily@21:00' to a readable Vietnamese string."""
    if not rule:
        return "một lần"
    if rule.startswith("daily@"):
        time_part = rule.split("@", 1)[1]
        return f"Hàng ngày {time_part}"
    if rule.startswith("weekly:"):
        rest = rule[len("weekly:"):]
        days_part, _, time_part = rest.partition("@")
        days_vi = ", ".join(_DAY_VI.get(d.strip(), d.strip()) for d in days_part.split(","))
        return f"{days_vi} {time_part}"
    return rule


async def _cmd_danh_sach_lich_hoc(
    chat_id: str, user, deps: CoreDeps,
) -> None:
    """danh sach lich hoc — list all pending study-category tasks for the user."""
    if deps.task_store is None:
        await deps.channel.send(
            chat_id, "Tính năng task chưa được kích hoạt.", use_markdown=False,
        )
        return

    tasks = deps.task_store.list_for_user(user.id, status="pending", category="study")
    if not tasks:
        await deps.channel.send(
            chat_id, "Không có lịch học nào đang hoạt động.", use_markdown=False,
        )
        return

    lines = ["📚 *Lịch học:*"]
    for t in tasks:
        schedule = _format_recurring_rule(t.get("recurring_rule"))
        lines.append(f"#{t['id']} *{t['title']}* — {schedule}")
    lines.append("\nDùng `sửa lịch học: [id] [mô tả mới]` hoặc `hủy lịch học: [id]` để thay đổi.")
    await deps.channel.send(chat_id, "\n".join(lines))


async def _cmd_huy_lich_hoc(
    chat_id: str, body: str, user, deps: CoreDeps,
) -> None:
    """huy lich hoc: <id> — cancel a study schedule task."""
    if deps.task_store is None:
        await deps.channel.send(
            chat_id, "Tính năng task chưa được kích hoạt.", use_markdown=False,
        )
        return

    task_id = _parse_task_id(body)
    if task_id is None:
        await deps.channel.send(
            chat_id,
            "Cú pháp: hủy lịch học: <id>  ví dụ: hủy lịch học: 4",
            use_markdown=False,
        )
        return

    task = deps.task_store.get_task(task_id)
    if task is None or task.get("user_id") != user.id:
        await deps.channel.send(
            chat_id, f"Không tìm thấy lịch học #{task_id}.", use_markdown=False,
        )
        return
    if task.get("category") != "study":
        await deps.channel.send(
            chat_id,
            f"Task #{task_id} không phải lịch học. Dùng `hủy task: {task_id}` để hủy task thường.",
            use_markdown=False,
        )
        return

    deps.task_store.cancel_task(task_id)
    if deps.reminder_engine is not None:
        deps.reminder_engine.cancel_all_for_task(task_id)
    await deps.channel.send(
        chat_id,
        f"🗑 Đã hủy lịch học #{task_id}: {task['title']}.",
        use_markdown=False,
    )


async def _cmd_sua_lich_hoc(
    chat_id: str, body: str, user, deps: CoreDeps,
) -> None:
    """sua lich hoc: <id> <mo ta moi> — re-parse and update an existing study schedule."""
    if deps.task_store is None or deps.task_parser is None:
        await deps.channel.send(
            chat_id, "Tính năng task chưa được kích hoạt.", use_markdown=False,
        )
        return

    parts = body.strip().split(None, 1)
    if len(parts) < 2 or not parts[0].isdigit():
        await deps.channel.send(
            chat_id,
            "Cú pháp: sửa lịch học: <id> <mô tả mới>\n"
            "Ví dụ: sửa lịch học: 4 Kẹo học toán 6h tối thứ 5 hàng tuần",
            use_markdown=False,
        )
        return

    task_id = int(parts[0])
    new_description = parts[1].strip()

    task = deps.task_store.get_task(task_id)
    if task is None or task.get("user_id") != user.id:
        await deps.channel.send(
            chat_id, f"Không tìm thấy lịch học #{task_id}.", use_markdown=False,
        )
        return
    if task.get("category") != "study":
        await deps.channel.send(
            chat_id,
            f"Task #{task_id} không phải lịch học.",
            use_markdown=False,
        )
        return

    try:
        from datetime import datetime
        from timeutils import VIETNAM_TZ
        morning_default = deps.user_store.get_morning_default_time(user.id) or "09:00"
        parsed = deps.task_parser.parse(
            new_description, morning_default=morning_default, now=datetime.now(VIETNAM_TZ),
        )
    except Exception as e:
        await deps.channel.send(chat_id, str(e), use_markdown=False)
        return

    deps.task_store.update_task(
        task_id,
        title=parsed.title,
        deadline=parsed.deadline_iso,
        recurring_rule=parsed.recurring_rule,
    )
    if deps.reminder_engine is not None:
        deps.reminder_engine.cancel_all_for_task(task_id)
        updated = deps.task_store.get_task(task_id)
        if updated:
            deps.reminder_engine.schedule_for_task(updated)

    dl_display = parsed.deadline_iso[:16].replace("T", " ")
    lines = [
        f"✏️ Đã cập nhật lịch học #{task_id}: *{parsed.title}*",
        f"📅 {dl_display}",
    ]
    if parsed.recurring_rule:
        lines.append(f"🔁 {_format_recurring_rule(parsed.recurring_rule)}")
    await deps.channel.send(chat_id, "\n".join(lines))


# ═══════════════════════════════════════════════════════════════════════════════
# Inline keyboard callback dispatcher
# ═══════════════════════════════════════════════════════════════════════════════


async def _handle_callback(msg: ChannelMessage, user: User, deps: CoreDeps) -> None:
    """Dispatch an inline keyboard callback_query to the appropriate handler.

    callback_data formats:
      done:<task_id>           — mark task completed
      snooze:<task_id>:<min>   — snooze by <min> minutes
      view:<task_id>           — show task detail
    """
    chat_id = msg.chat_id
    callback_data = msg.raw.get("callback_data", "")
    cq_id = msg.raw.get("callback_query_id", "")

    # Stop the loading spinner — Telegram-specific, guard with hasattr.
    if hasattr(deps.channel, "answer_callback_query"):
        await deps.channel.answer_callback_query(cq_id)

    if deps.task_store is None:
        await deps.channel.send(chat_id, "Tính năng task chưa được kích hoạt.", use_markdown=False)
        return

    parts = callback_data.split(":")
    action = parts[0] if parts else ""

    if action == "done" and len(parts) >= 2:
        try:
            task_id = int(parts[1])
        except ValueError:
            await deps.channel.send(chat_id, "Lệnh không hợp lệ.", use_markdown=False)
            return
        task = deps.task_store.get_task(task_id)
        if task is None or task.get("user_id") != user.id:
            await deps.channel.send(chat_id, f"Không tìm thấy task #{task_id}.", use_markdown=False)
            return
        from datetime import datetime
        from timeutils import VIETNAM_TZ
        deps.task_store.complete_task(task_id, completed_at=datetime.now(VIETNAM_TZ).strftime("%Y-%m-%d %H:%M:%S"))
        if deps.reminder_engine is not None:
            deps.reminder_engine.cancel_all_for_task(task_id)
        await deps.channel.send(
            chat_id, f"✅ Đã hoàn thành task #{task_id}: {task['title']}", use_markdown=False,
        )

    elif action == "snooze" and len(parts) >= 3:
        try:
            task_id = int(parts[1])
            minutes = int(parts[2])
        except ValueError:
            await deps.channel.send(chat_id, "Lệnh không hợp lệ.", use_markdown=False)
            return
        task = deps.task_store.get_task(task_id)
        if task is None or task.get("user_id") != user.id:
            await deps.channel.send(chat_id, f"Không tìm thấy task #{task_id}.", use_markdown=False)
            return
        if deps.reminder_engine is None:
            await deps.channel.send(chat_id, "Tính năng task chưa được kích hoạt.", use_markdown=False)
            return
        try:
            deps.reminder_engine.snooze(task_id, minutes)
        except ValueError as e:
            await deps.channel.send(chat_id, str(e), use_markdown=False)
            return
        await deps.channel.send(
            chat_id,
            f"😴 Đã hoãn task #{task_id}: {task['title']} thêm {minutes} phút.",
            use_markdown=False,
        )

    elif action == "view" and len(parts) >= 2:
        try:
            task_id = int(parts[1])
        except ValueError:
            await deps.channel.send(chat_id, "Lệnh không hợp lệ.", use_markdown=False)
            return
        task = deps.task_store.get_task(task_id)
        if task is None or task.get("user_id") != user.id:
            await deps.channel.send(chat_id, f"Không tìm thấy task #{task_id}.", use_markdown=False)
            return
        await deps.channel.send(chat_id, _format_task_detail(task))

    else:
        await deps.channel.send(
            chat_id, f"Lệnh không hợp lệ: {callback_data!r}.", use_markdown=False,
        )
