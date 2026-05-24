# FR-7 — Tasks + Reminders + Daily Summary + Parent Digest — Detailed Implementation Plan

> **Status:** ✅ DONE — 2026-05-24, branch `feature/FR7`, 867 tests passing
> **Created:** 2026-05-23
> **Branch:** `feature/FR7` (branch off từ `main` theo Git workflow Section 3.5)
> **Approach:** 1 PR duy nhất `feature/FR7` → `main`; sub-tasks 7.1 → 7.8 commit tuần tự trên cùng branch

---

## 1. Goal

FR-7 thêm năng lực **task management + reminder engine** cho hệ thống — 4 nhóm tính năng chính:

1. **Tasks CRUD** — user tạo/sửa/xoá/đánh dấu hoàn thành task; có deadline; có category (`task` mặc định + `study` cho lịch học trẻ em); soft-delete để hỗ trợ undo + audit.
2. **Reminder engine tổng quát** — nhắc trước deadline ở nhiều mốc offset (default `2h, 1h, 30m, 15m`); offset configurable per task; hỗ trợ recurring task (vd lịch học tuần); engine thiết kế để FR-8 (anniversary) plug vào dùng lại (Decision #46).
3. **Daily summary** — cuối ngày bot gửi user tổng kết task đã hoàn thành / còn pending hôm nay + dự kiến ngày mai.
4. **Parent digest + Tier-1 mirror** — reminder real-time của con luôn mirror sang parent (Tier 1, không cấu hình, runtime check tuổi 18 theo Decision #22); digest theo `digest_frequency` đã có trong `parent_links` (Tier 2).

**Scope mở rộng theo lựa chọn user (2026-05-23):**
- **Input format = Hybrid**: prefix `tao task:` + LLM Haiku parse phần free-form (title + deadline + recurring).
- **Web UI = Full CRUD**: trang list, create, edit, complete trên web (không phải Telegram-only).
- **Snooze = inline button**: mỗi reminder có inline button "Hoãn 15p / 1h / Done".

**FR-7 KHÔNG bao gồm:**
- Multi-user task assignment (gán task cho người khác) — v1 chỉ task cá nhân.
- Subtask / dependency / project hierarchy.
- Anniversary / lunar calendar (để FR-8 dùng lại engine).
- Calendar view (chỉ list view trong v1).
- Notification time-zone customization (giờ VN cứng — Decision đã chốt từ trước với `VIETNAM_TZ`).
- Voice / image task input.

---

## 2. Context & Decisions

### 2.1 ROADMAP references

| Ref | Nội dung |
|---|---|
| Section 5 — FR-7 | Scope ban đầu: task_store, reminders 2h/1h/30m/15m, daily summary, kids' study, mirror parent, digest |
| Decision #19 | Reminder real-time LUÔN mirror parent, không cấu hình được |
| Decision #20 | Digest frequency: `daily`/`weekly`/`monthly`/`off` |
| Decision #21 | Digest default `daily` 21:00 khi tạo parent link |
| Decision #22 | Auto-off tuổi 18 enforce runtime, KHÔNG mutate DB |
| Decision #30 | FR-7 + FR-8 gộp: kids' study là category trong FR-7 |
| Decision #46 | Reminder engine TỔNG QUÁT — offset cấu hình tùy ý + recurring, KHÔNG hardcode 4 mốc |
| Decision #65 | Notification queue persistent → tái sử dụng `notification_service` |

### 2.2 Quyết định nền tảng (chốt khi soạn plan này — bổ sung Decision Log khi merge)

| # | Quyết định | Rationale |
|---|-----------|-----------|
| D1 | **Tách 2 bảng `tasks` + `task_reminders`** (không nhét offset list vào JSON cột của `tasks`) | Query "reminder nào ready bây giờ" cần index trên `fire_at`; nhét JSON phải scan + parse mỗi tick — quy mô gia đình OK nhưng tách bảng đúng pattern và dễ index |
| D2 | **Reuse `notification_service` cho delivery** | Đã có persistent queue + retry + multi-channel (FR-4); reminder chỉ enqueue payload `{kind: "reminder", task_id, snooze: bool}`, không cần queue riêng |
| D3 | **Hybrid input: prefix `tao task:` + LLM parse phần sau** | Boundary rõ ràng (chỉ gọi LLM khi user explicit muốn); LLM trả về `{title, deadline_iso, recurring_rule}`; fallback prompt user nếu LLM không parse được deadline |
| D4 | **Recurring rule = subset RFC 5545 đơn giản hoá** | Dùng string format `"weekly:MON,WED,FRI@07:00"` hoặc `"daily@21:00"`; KHÔNG dùng `dateutil.rrule` full spec (over-kill); engine tự parse string → next occurrence. Migration sang full rrule sau là additive. |
| D5 | **Default offsets `[2h, 1h, 30m, 15m]`; configurable per task** | Cột `tasks.reminder_offsets` lưu CSV list (`"7200,3600,1800,900"` đơn vị giây); admin có thể edit per task; user setting global để FR sau |
| D6 | **Snooze inline button → tạo notification mới sau N phút; max 3 lần (strict)** | Callback_query handler match `snooze:<task_id>:<minutes>` → insert task_reminders row mới với `fire_at = now + N phút`; không sửa task hay reminder gốc (audit trail). Sau 3 lần liên tiếp → cancel reminder + audit `reminder_abandoned` (Q6) |
| D7 | **Mirror parent runtime check tuổi 18** | Trong `reminder_engine.emit()`: nếu task owner under-18 và có parent_links active → enqueue notification cho cả parent. Check `birthdate` tại runtime (consistent D22), không mutate flag |
| D8 | **Daily summary configurable per user, default 21:00, có thể tắt** | Khi user mới tạo: NULL = dùng default 21:00 giờ VN (consistent với D21); user lệnh `cau hinh tong ket: HH:MM` đổi giờ hoặc `cau hinh tong ket: tat` để tắt; lưu cột `users.daily_summary_time` (Q1) |
| D9 | **Parent digest dùng `parent_links.digest_frequency` / `digest_time` đã có** | Schema sẵn từ FR-2; cron job daily kiểm tra link nào đến hạn; render summary từ `tasks` + `audit_log` của child |
| D10 | **Web CRUD routes follow REST-ish pattern** | `GET /tasks` list, `GET /tasks/new` form, `POST /tasks` create, `GET /tasks/{id}` view, `GET /tasks/{id}/edit` form, `POST /tasks/{id}` update, `POST /tasks/{id}/complete`, `POST /tasks/{id}/delete` |
| D11 | **Recurring expansion = lazy (next occurrence on trigger)** | Khi reminder của recurring task fire → engine tính `next_fire_at` của next occurrence + insert new `task_reminders` rows. Không pre-populate hàng nghìn rows. Restart-safe vì rows tự được tạo khi trigger. |
| D12 | **Past-deadline grace 1 giờ — sau đó skip + audit `reminder_missed`** | Nếu bot down >1h và bỏ lỡ reminder → không spam khi back up; audit log để debug. Reminder trong window 1h vẫn fire (catch-up). |
| D13 | **Task scope = private mặc định** | Tái dùng pattern FR-3 scope (Decision #49); v1 chỉ `private`; `everyone` (family-shared task) để FR sau |
| D14 | **Audit events**: `task_created`, `task_updated`, `task_completed`, `task_deleted`, `task_snoozed`, `reminder_fired`, `reminder_missed`, `daily_summary_sent`, `parent_digest_sent` | Tái dùng `AuditLog` Protocol; payload có `task_id`, `offset_seconds`, `delivery_channels` |
| D15 | **LLM task parser dùng Haiku 4.5** | Cost ~$0.0001/task — nhỏ; pattern giống D72 (FR-5.5 title generation); structured output qua tool-use API |
| D16 | **Default morning time 09:00 cho task không có giờ; configurable** | Khi LLM parse "mai" / "thứ 5 tới" mà không kèm giờ → dùng `users.morning_default_time` (NULL = 09:00); user lệnh `cau hinh gio mac dinh: HH:MM` đổi (Q3) |
| D17 | **Past-deadline task → reject + ask user nhập lại** | Bot reply "Deadline đã qua, vui lòng nhập lại giờ tương lai"; KHÔNG tạo task silently và KHÔNG fire ngay. Tránh spam và nhập nhầm (Q4) |
| D18 | **Recurring shortcut `lich hoc:` song song với LLM parse tự nhiên** | User có thể gõ `tao task: thu 2-6 luc 7h, hoc tieng anh` (LLM parse) hoặc shortcut `lich hoc: <free-form>` (mặc định category=study, prefix gợi ý cấu trúc rõ hơn). Cả 2 cùng đi qua `task_parser.parse()` (Q2) |
| D19 | **LLM parse fail → reply hướng dẫn + audit, KHÔNG pending state** | Reply "Mình chưa rõ deadline — vui lòng gõ lại với giờ cụ thể, vd '5h chiều mai'" + audit `task_parse_failed`; user gõ lại lệnh `tao task` mới. Đơn giản hơn pending state machine (Q9) |

### 2.3 Audit events taxonomy

Mọi event ghi vào `audit_log` (FR-4):

| `action` | `target_type` | `target_id` | `payload` | Khi nào |
|---|---|---|---|---|
| `task_created` | `task` | task_id | `{title, deadline, recurring_rule, offsets, source: "telegram"\|"web"}` | Mỗi lần tạo |
| `task_updated` | `task` | task_id | `{changed_fields: [...]}` | Sửa task |
| `task_completed` | `task` | task_id | `{completed_at, was_late: bool}` | User mark done |
| `task_deleted` | `task` | task_id | `{deleted_by_actor: bool}` | Soft-delete |
| `task_snoozed` | `task` | task_id | `{snooze_minutes, new_fire_at}` | User click snooze |
| `reminder_fired` | `task` | task_id | `{offset_seconds, channels_delivered, mirrored_to_parent: [parent_ids]}` | Reminder gửi thành công |
| `reminder_missed` | `task` | task_id | `{fire_at, missed_seconds}` | Reminder quá hạn grace 1h |
| `daily_summary_sent` | `user` | user_id | `{completed: N, pending: M, due_tomorrow: K}` | Daily summary gửi |
| `parent_digest_sent` | `user` | parent_id | `{child_id, frequency, period, stats: {...}}` | Digest gửi |

---

## 3. Open Questions — ✅ ALL RESOLVED (2026-05-23)

Tất cả 10 câu hỏi đã chốt với user. Kết quả thể hiện trong Decisions D1-D19.

| ID | Câu hỏi | ✅ Resolved |
|----|---------|-------------|
| Q1 | Daily summary time có configurable per user không, hay fix cứng 21:00? | **Configurable + tắt được, default 21:00 khi init** → D8 + migration 020 |
| Q2 | Recurring task UX trên Telegram — syntax thế nào? | **Cả 2: LLM parse tự nhiên + shortcut `lich hoc:`** → D18 |
| Q3 | Reminder time-of-day default cho task không có giờ (vd "mai") | **09:00, configurable per user** → D16 + migration 020 |
| Q4 | Nếu user tạo task quá khứ | **Reject + ask user nhập lại** → D17 |
| Q5 | Parent digest content | **Stats + top 3 pending** + link `/admin/users/{id}/tasks` (Section 11.2) |
| Q6 | Snooze max | **3 lần (strict)** → D6 updated |
| Q7 | Web template style | **Extends `base.html`** (Section 10) |
| Q8 | Multiline payload cho `tao task:` | **Có** — text sau prefix nhiều dòng OK (Section 8) |
| Q9 | LLM parse fail | **Reply hướng dẫn + audit `task_parse_failed`** → D19 |
| Q10 | Recurring task lần fire đầu tiên | **Next occurrence sau khi tạo** → D11 (lazy) consistent |

---

## 4. Scope Breakdown

| Sub | Tên | File chính | Mô tả |
|-----|-----|-----------|-------|
| 7.1 | Schema + Stores | `db/migrations/018_tasks.sql`, `019_task_reminders.sql`, `020_user_task_prefs.sql`, `task_store.py`, `reminder_store.py` | 2 bảng mới + 2 cột user prefs + 2 store class CRUD |
| 7.2 | Reminder engine | `reminder_engine.py` | Scan ready reminders → enqueue notification → handle recurring next-occurrence |
| 7.3 | LLM task parser | `task_parser.py` | Haiku 4.5 structured output: free-form → `{title, deadline_iso, recurring_rule}` |
| 7.4 | Telegram commands | `core_handler.py` (edit) | `tao task:`, `xong task:`, `huy task:`, `danh sach task`, `task <id>` view, `lich hoc:` recurring shortcut |
| 7.5 | Snooze inline button | `channel_telegram.py` (edit), `core_handler.py` (edit) | Callback_query handler `snooze:<task_id>:<minutes>` + done button |
| 7.6 | Web routes + templates | `web_router.py` (edit), `templates/tasks.html`, `task_form.html`, `task_view.html` | Full CRUD: list, new, view, edit, complete, delete |
| 7.7 | Scheduled jobs | `scheduled_jobs.py` (edit) | `scan_reminders` (mỗi 1 phút), `daily_summary` (21:00), `parent_digest` (mỗi giờ check link nào tới hạn) |
| 7.8 | Tests + wiring | `tests/test_task_store.py`, `test_reminder_engine.py`, `test_task_parser.py`, `test_task_routes.py`, `test_task_handlers.py`; `deps.py`, `main.py` (edit) | ~80-100 test cases |

---

## 5. File Changes Summary

### 5.1 New files

| # | File | Purpose |
|---|------|---------|
| 1 | `db/migrations/018_tasks.sql` | Bảng `tasks` (xem Section 6) |
| 2 | `db/migrations/019_task_reminders.sql` | Bảng `task_reminders` |
| 2b | `db/migrations/020_user_task_prefs.sql` | Thêm 2 cột `users.daily_summary_time`, `users.morning_default_time` |
| 3 | `task_store.py` | `SqliteTaskStore` — CRUD task + query (list by user, by status, due range) |
| 4 | `reminder_store.py` | `SqliteReminderStore` — CRUD reminder + query (ready-to-fire, by task) |
| 5 | `reminder_engine.py` | `ReminderEngine` — scan + emit + recurring expansion + mirror to parent |
| 6 | `task_parser.py` | `TaskParser` — LLM Haiku tool-use; fallback errors |
| 7 | `templates/tasks.html` | List view với filter (today/upcoming/completed) |
| 8 | `templates/task_form.html` | Create + edit form (deadline picker, offsets, recurring) |
| 9 | `templates/task_view.html` | Detail view với history reminders + audit timeline |
| 10 | `tests/test_task_store.py` | CRUD + query tests |
| 11 | `tests/test_reminder_engine.py` | Scan + emit + recurring + parent mirror tests |
| 12 | `tests/test_task_parser.py` | LLM parse tests (mock Haiku) |
| 13 | `tests/test_task_routes.py` | HTTP tests cho web CRUD |
| 14 | `tests/test_task_handlers.py` | Telegram command + callback_query tests |

### 5.2 Edited files

| # | File | Change |
|---|------|--------|
| 15 | `interfaces.py` | Thêm Protocol `TaskStore`, `ReminderStore` (nếu cần expose qua CoreDeps) |
| 16 | `deps.py` | Thêm `task_store`, `reminder_store`, `reminder_engine`, `task_parser` vào `CoreDeps` |
| 17 | `main.py` | Wire stores + engine + parser; register scheduled jobs |
| 18 | `core_handler.py` | Dispatch `tao task`, `xong task`, `huy task`, `danh sach task`, `task <id>`, `lich hoc`; help group mới `quan ly task` hoặc gộp vào `cong cu` |
| 19 | `channel_telegram.py` | Handler cho `callback_query` (inline button); method `send_with_inline_keyboard()` |
| 20 | `web_router.py` | 7 routes mới (`GET/POST /tasks`, `GET /tasks/new`, `GET/POST /tasks/{id}`, `GET /tasks/{id}/edit`, `POST /tasks/{id}/complete`, `POST /tasks/{id}/delete`) |
| 21 | `web_channel.py` | (Có thể) thêm Web notification rendering cho task reminder banner trong chat UI — TBD trong 7.6 |
| 22 | `templates/chat.html` | Sidebar link "📋 Task của tôi" → `/tasks` |
| 23 | `scheduled_jobs.py` | 3 jobs mới: `scan_reminders` mỗi 1 phút, `send_daily_summary` 21:00 VN, `send_parent_digest` mỗi giờ |
| 24 | `notification_service.py` | (Có thể) extend `enqueue()` để chấp nhận `payload.kind = "reminder"` với inline button hint cho TelegramAdapter |

---

## 6. Database Schema

### 6.1 Migration `018_tasks.sql`

```sql
CREATE TABLE IF NOT EXISTS tasks (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id             INTEGER NOT NULL REFERENCES users(id),
    title               TEXT NOT NULL,
    description         TEXT,                          -- optional details
    deadline            TEXT NOT NULL,                 -- ISO datetime VN TZ
    category            TEXT NOT NULL DEFAULT 'task',  -- 'task' | 'study' | 'reminder'
    scope               TEXT NOT NULL DEFAULT 'private',  -- D13: 'private' only v1
    recurring_rule      TEXT,                          -- NULL = one-shot; vd 'weekly:MON,WED@07:00'
    reminder_offsets    TEXT NOT NULL DEFAULT '7200,3600,1800,900',  -- CSV seconds (D5)
    status              TEXT NOT NULL DEFAULT 'pending',  -- 'pending' | 'completed' | 'cancelled'
    completed_at        TEXT,                          -- ISO datetime; NULL nếu chưa done
    snooze_count        INTEGER NOT NULL DEFAULT 0,    -- count snooze (Q6 max 5)
    source              TEXT NOT NULL DEFAULT 'telegram',  -- 'telegram' | 'web'
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    deleted_at          TEXT                           -- soft-delete (recycle bin compat FR-4)
);

CREATE INDEX IF NOT EXISTS idx_tasks_user_status ON tasks(user_id, status) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_tasks_deadline ON tasks(deadline) WHERE status = 'pending' AND deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_tasks_recurring ON tasks(recurring_rule) WHERE recurring_rule IS NOT NULL AND deleted_at IS NULL;
```

### 6.2 Migration `019_task_reminders.sql`

```sql
CREATE TABLE IF NOT EXISTS task_reminders (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id         INTEGER NOT NULL REFERENCES tasks(id),
    fire_at         TEXT NOT NULL,                  -- ISO datetime VN TZ
    offset_seconds  INTEGER NOT NULL,               -- distance from deadline (negative = after); 0 nếu snooze
    kind            TEXT NOT NULL DEFAULT 'scheduled',  -- 'scheduled' | 'snoozed'
    status          TEXT NOT NULL DEFAULT 'pending',    -- 'pending' | 'fired' | 'missed' | 'cancelled'
    fired_at        TEXT,
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_reminders_ready ON task_reminders(fire_at, status) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_reminders_task ON task_reminders(task_id);
```

**Notes:**
- `task_reminders` không có cột `user_id` — derive từ `task.user_id` via JOIN. Tránh denormalize.
- Khi task `completed` / `cancelled` / `deleted` → set `status = 'cancelled'` cho tất cả reminders `pending` của task đó.
- Recurring task: khi 1 reminder fire xong và task có `recurring_rule`, engine compute next occurrence và insert N rows mới (1 row per offset).

### 6.3 Migration `020_user_task_prefs.sql`

```sql
ALTER TABLE users ADD COLUMN daily_summary_time TEXT;
-- NULL = default 21:00 VN, 'off' = disabled, 'HH:MM' = custom

ALTER TABLE users ADD COLUMN morning_default_time TEXT;
-- NULL = default 09:00 VN, 'HH:MM' = custom
```

**Notes:**
- Cả 2 cột NULLABLE; semantics NULL = dùng default (D8, D16). Tránh phải UPDATE backfill.
- Validate format `HH:MM` (0-23 : 0-59) trong `user_store.set_daily_summary_time()` / `set_morning_default_time()`.
- Migration đặt riêng (020) thay vì gộp vào 018/019 vì sửa bảng `users` — concern riêng.

---

## 7. Reminder Engine Design

### 7.1 Component diagram

```
┌──────────────────────────────────────────────────────────────────┐
│                     scheduled_jobs.py                            │
│   ┌─────────────────────┐   ┌──────────────────┐                 │
│   │ scan_reminders /1m  │   │ daily_summary 21h│                 │
│   └──────────┬──────────┘   └────────┬─────────┘                 │
└──────────────┼────────────────────────┼──────────────────────────┘
               │                        │
               ▼                        ▼
┌──────────────────────────────────────────────────────────────────┐
│                   reminder_engine.py                             │
│  ReminderEngine.tick() :                                         │
│    1. fetch pending reminders WHERE fire_at <= now()             │
│    2. for each: build payload, enqueue via NotificationService   │
│    3. if recurring: compute next occurrence, insert new rows     │
│    4. mark reminder 'fired'; audit 'reminder_fired'              │
│    5. if owner under-18: also enqueue for active parents (D7)    │
│                                                                  │
│  Late > 1h: mark 'missed', audit 'reminder_missed' (D12)         │
└──────────────────────────────┬───────────────────────────────────┘
                               │
                               ▼
┌──────────────────────────────────────────────────────────────────┐
│             notification_service.py (FR-4)                       │
│  enqueue(user_id, channel, payload) → pending_notifications row  │
│  payload.kind = 'reminder' carries task_id + inline_buttons hint │
└──────────────────────────────┬───────────────────────────────────┘
                               │
                               ▼
┌──────────────────────────────────────────────────────────────────┐
│           flush_pending (mỗi 30s, FR-4 existing job)             │
│  TelegramAdapter.send_with_inline_keyboard(chat_id, text, btns)  │
└──────────────────────────────────────────────────────────────────┘
```

### 7.2 ReminderEngine API

```python
class ReminderEngine:
    def __init__(
        self,
        task_store: SqliteTaskStore,
        reminder_store: SqliteReminderStore,
        user_store: UserStore,
        notification_service: NotificationService,
        audit: AuditLog,
        now_fn: Callable[[], datetime] = lambda: datetime.now(VN_TZ),
    ): ...

    def tick(self) -> dict:
        """Called by APScheduler every 1 minute.
        Returns stats {fired: N, missed: M, recurring_expanded: K}.
        """

    def schedule_for_task(self, task: Task) -> list[int]:
        """Compute fire_at for each offset and insert task_reminders rows.
        Called on task create / edit / recurring next-occurrence.
        Returns list of new reminder IDs.
        """

    def cancel_all_for_task(self, task_id: int) -> int:
        """Mark all pending reminders of a task as 'cancelled'.
        Called on task complete / cancel / delete.
        """

    def snooze(self, task_id: int, minutes: int) -> int:
        """Create a new reminder row at now + minutes; mark task.snooze_count++.
        Returns new reminder ID. Raises if snooze_count >= 5 (Q6).
        """

    def _emit(self, reminder: Reminder, task: Task, owner: User) -> None:
        """Build payload + call notification_service.enqueue().
        Mirror to active parents if owner under-18 (runtime check D7).
        Emit audit 'reminder_fired'.
        """
```

### 7.3 Edge cases

| Case | Handling |
|------|----------|
| Task complete trước reminder fire | `cancel_all_for_task()` set tất cả pending → `cancelled` |
| Task deleted | Same as complete |
| User edit deadline | `cancel_all_for_task()` + `schedule_for_task()` lại |
| Recurring task edit | Cancel pending + lazy compute next on next tick |
| Bot down 6 giờ → bật lại | Tick 1 lần: reminders trong window grace 1h → fire; reminders quá hạn > 1h → audit `reminder_missed` + skip |
| Parent under-18 (đệ tử khoa học)?| Edge case — skip mirror, log warning |
| Mirror nhưng parent đã unbind Telegram | NotificationService retry → max fail → audit `notification_failed` (consistent FR-4) |
| Snooze lúc đã missed | Cho phép — snooze tạo reminder mới từ thời điểm click |

---

## 8. Task Parser Design

### 8.1 Hybrid input flow

```
User types: "tao task: nhac mua sua 5h chieu mai"
            └── prefix ──┘ └─── free-form ──────┘

1. core_handler matches prefix "tao task: " → dispatch _cmd_tao_task
2. _cmd_tao_task calls task_parser.parse(free_form, user, now=now())
3. task_parser:
   a. Build prompt with Haiku 4.5 tool-use:
      Tool "create_task" với schema:
        title: str
        deadline_iso: str (ISO 8601 with TZ)
        recurring_rule: str | null
        category: 'task' | 'study' | 'reminder'
   b. Call AnthropicLLM.ask() — but use cheap Haiku model
   c. Validate tool_use response
   d. Return ParsedTask object hoặc ParseError
4. If parse ok → task_store.create() → reminder_engine.schedule_for_task() →
   reply "Đã tạo task #N: <title>, nhắc trước 2h/1h/30m/15m. Bot sẽ ping lúc <fire_at>."
5. If parse fail → reply Q9 message + audit 'task_parse_failed'
```

### 8.2 LLM prompt sketch

```
SYSTEM: Bạn là task parser cho gia đình. User mô tả task tiếng Việt;
        bạn extract title (ngắn gọn 3-8 từ), deadline (ISO 8601 timezone +07:00),
        recurring (NULL nếu one-shot, hoặc format 'weekly:MON,WED@07:00'),
        category ('task' mặc định, 'study' nếu liên quan học).
        Now là {now_iso}. Deadline phải > now. Nếu user nói "mai" → 9h sáng mai (Q3).

USER (tool_input): {free_form}

[Tool: create_task]
```

### 8.3 Parser tests

- Happy path: "mua sua 5h chieu mai" → title "Mua sữa", deadline tomorrow 17:00
- Recurring: "thu 2-6 luc 7h sang, hoc tieng anh" → weekly:MON,TUE,WED,THU,FRI@07:00, category=study
- Ambiguous: "tuan sau lam bai tap" → ParseError, ask user clarify
- Past time: "5h sang nay" lúc 10h → ParseError "deadline đã qua"

---

## 9. Telegram Command Spec

### 9.1 Command table

| Lệnh | Prefix | Quota exempt? | Handler |
|------|--------|---------------|---------|
| `tao task: <free-form>` | `tao task:`, `tạo task:`, `task:` | No (gọi LLM) | `_cmd_tao_task` |
| `xong task: <id>` | `xong task:`, `done task:` | Yes | `_cmd_xong_task` |
| `huy task: <id>` | `huy task:`, `hủy task:`, `xoa task:` | Yes | `_cmd_huy_task` |
| `danh sach task` | `danh sach task`, `danh sách task`, `list task` | Yes | `_cmd_danh_sach_task` |
| `task <id>` | `task ` (space then digits) | Yes | `_cmd_xem_task` |
| `lich hoc: <free-form>` | `lich hoc:`, `lịch học:` | No (LLM) | `_cmd_lich_hoc` (shorthand cho recurring + category=study) — D18 |
| `hoan task: <id>, <minutes>` | `hoan task:`, `hoãn task:`, `snooze:` | Yes | `_cmd_hoan_task` (text version, complement inline button) |
| `cau hinh tong ket: <HH:MM\|tat>` | `cau hinh tong ket:`, `cấu hình tổng kết:` | Yes | `_cmd_cau_hinh_tong_ket` — đổi giờ daily summary (D8/Q1) |
| `cau hinh gio mac dinh: <HH:MM>` | `cau hinh gio mac dinh:`, `cấu hình giờ mặc định:` | Yes | `_cmd_cau_hinh_gio_mac_dinh` — đổi giờ default cho "mai" (D16/Q3) |

### 9.2 Inline button callback_data format

```
done:<task_id>           → mark task completed
snooze:<task_id>:15      → snooze 15 phút
snooze:<task_id>:60      → snooze 1 giờ
view:<task_id>           → reply detail view
```

`channel_telegram.py` thêm method:

```python
async def send_with_inline_keyboard(
    self,
    chat_id: str,
    text: str,
    buttons: list[list[dict]],   # [[{text, callback_data}], ...]
    use_markdown: bool = False,
) -> dict:
    """Same as send() but with reply_markup inline_keyboard."""

async def handle_callback_query(
    self,
    callback_query: dict,
) -> None:
    """Dispatch to core_handler with synthetic ChannelMessage having
    raw['callback_data'] = '<action>:<args>'. Answer callback_query (loading
    spinner stops) regardless of handler result."""
```

`core_handler.handle_message()` detect `msg.raw.get('callback_data')` → route tới `_handle_callback`.

---

## 10. Web Routes Spec

| Method | Path | Purpose | Template | Auth |
|--------|------|---------|----------|------|
| GET | `/tasks` | List với filter `?status=pending\|completed\|all` | `tasks.html` | Logged-in user (self) |
| GET | `/tasks/new` | Create form | `task_form.html` | Same |
| POST | `/tasks` | Create — form fields `title, deadline (datetime-local), recurring_rule, offsets, category` | redirect to `/tasks/{id}` | Same |
| GET | `/tasks/{id}` | Detail + history reminders | `task_view.html` | Owner |
| GET | `/tasks/{id}/edit` | Edit form | `task_form.html` | Owner |
| POST | `/tasks/{id}` | Update | redirect to view | Owner |
| POST | `/tasks/{id}/complete` | Mark done | redirect to list | Owner |
| POST | `/tasks/{id}/delete` | Soft-delete | redirect to list | Owner |
| GET | `/admin/users/{id}/tasks` | Admin xem task của child under-18 (stealth-read FR-4) | `tasks.html` (admin context) | Admin + target under-18 |

**Sidebar link** trong `chat.html`: `📋 Task của tôi` → `/tasks?status=pending`.

---

## 11. Daily Summary + Parent Digest Spec

### 11.1 Daily summary (configurable per user — D8/Q1)

**Scheduling model:** Job `send_daily_summary` chạy mỗi 1 phút (hoặc mỗi 5 phút) → query users có `daily_summary_time` matching `now.HH:MM` → gửi summary. Lý do: tránh tạo N cron jobs riêng cho N user.

Per user logic:
1. Đọc `users.daily_summary_time`:
   - `'off'` → skip
   - `NULL` → dùng default `21:00`
   - `'HH:MM'` → dùng giờ user chọn
2. Match với `now` VN (cùng giờ + cùng phút trong window 1 phút).
3. Query: completed today + pending due today + pending due tomorrow.
4. Render message (Vietnamese, no markdown):
   ```
   Tong ket hom nay [23/05]:
   ✅ Da xong: 3 task
   ⏰ Con lai: 2 task (qua han: 1)
   📅 Ngay mai: 4 task
   
   Goi 'danh sach task' de xem chi tiet.
   ```
5. `notification_service.enqueue()` qua telegram channel.
6. Audit `daily_summary_sent`.
7. Skip nếu user không có active telegram binding.
8. Skip nếu user không có task nào (tránh spam).

### 11.2 Parent digest (`send_parent_digest` mỗi giờ kiểm tra)

For each `parent_links` active:
1. Compute `due_now` từ `digest_frequency` + `digest_time`:
   - `daily` 21:00 → fire khi giờ hiện tại = 21 + last_sent < today
   - `weekly` 'SUN 20:00' → fire chủ nhật 20h nếu last_sent < 7 days
   - `monthly` '1 20:00' → fire ngày 1 nếu last_sent < 30 days
   - `off` → skip
2. Runtime check tuổi 18 (D7/D22): nếu child >= 18 và `adult_optin_enabled = false` → skip + audit one-time `digest_disabled_at_18` nếu chưa gửi.
3. Render digest: stats child's tasks (completed/pending/missed) + top 3 pending; period theo frequency.
4. Enqueue notification + audit `parent_digest_sent`.

---

## 12. Test Plan

### 12.1 Coverage targets

| File | Test cases | Coverage focus |
|------|-----------|----------------|
| `test_task_store.py` | ~20 | CRUD, soft-delete, query by status/range, recurring filter |
| `test_reminder_engine.py` | ~25 | tick(), schedule_for_task, snooze, recurring expansion, grace window, parent mirror runtime, audit emission |
| `test_task_parser.py` | ~15 | Mock Haiku response: title extraction, recurring parsing, ambiguous input, past deadline rejection |
| `test_task_routes.py` | ~20 | All 9 routes; auth (owner/admin); form validation; redirect targets |
| `test_task_handlers.py` | ~15 | Telegram commands dispatch; callback_query routing; snooze max enforcement |

**Total target:** ~95 test cases.

### 12.2 Integration test scenarios

1. Tạo task qua Telegram → reminder schedule → mock APScheduler tick → notification queued → flush_pending send.
2. Recurring task lifecycle: tạo weekly Mon 7h → tick Mon 4:59 → fire 4 reminders → tick lại → next Mon rows inserted.
3. Under-18 child task → parent mirror → cả 2 nhận notification.
4. Snooze 5 lần → lần 6 reject.
5. Bot down 6h → tick catch up: < 1h reminders fire, > 1h marked missed.

---

## 13. Implementation Order

Đề xuất tuần tự (commit per sub-task trên `feature/FR7`):

1. **7.1** Schema migrations (018 tasks, 019 task_reminders, 020 user_task_prefs) + `task_store.py` + `reminder_store.py` + `user_store` extensions (`set_daily_summary_time`, `set_morning_default_time`) + unit tests stores
2. **7.2** `reminder_engine.py` + unit tests engine (without parser yet — manual task fixtures)
3. **7.3** `task_parser.py` + unit tests parser (mock LLM)
4. **7.4** Telegram commands trong `core_handler.py` + handler tests
5. **7.5** Callback_query trong `channel_telegram.py` + integration với core_handler
6. **7.6** Web routes + templates + route tests
7. **7.7** Scheduled jobs trong `scheduled_jobs.py` + integration test catch-up
8. **7.8** Wiring `deps.py` + `main.py`; chạy full test suite; staging deploy + manual smoke test

**Ước lượng effort:** ~4-6 ngày dev (1 dev), bao gồm test + staging.

---

## 14. Rollout & Migration

- **No data migration** — bảng mới, không ảnh hưởng data hiện có.
- **Feature flag không cần** — task feature additive, không phá flow cũ.
- **Staging test checklist** (sẽ chi tiết khi merge):
  - Tạo task qua Telegram (one-shot + recurring) → reminder fire đúng giờ
  - Snooze inline button → reminder mới ở đúng thời điểm
  - Web CRUD đầy đủ (create, edit, complete, delete)
  - Parent under-18: mirror reminder nhận được bên parent
  - Daily summary đúng 21:00 → nội dung khớp DB
  - Parent digest theo `digest_frequency` chính xác
  - Tuổi 18 birthday: mirror auto-off + one-time notify (D22)
- **Rollback plan:** revert migrations + restart; data tasks/reminders mất nhưng không ảnh hưởng feature khác (additive).

---

## 15. Dependencies

- `apscheduler` (đã có từ FR-4) — thêm 3 jobs
- `dateutil` (đã có gián tiếp) — KHÔNG dùng `rrule` full spec (D4)
- Haiku 4.5 endpoint qua `AnthropicLLM` (đã có từ FR-5.5 title gen)
- `notification_service` + `notification_store` (đã có từ FR-4) — extend payload kind

**KHÔNG cần thêm package mới.**

---

## 16. Future Work (FR-7.5 / FR-8)

- **FR-7.5** (nếu cần tách): User-configurable daily summary time + per-user reminder offset defaults
- **FR-8** Anniversary: tái dùng `reminder_engine.py` (D46); thêm `anniversaries` table; lunar→solar lib; recurring annual với recompute mỗi năm
- Calendar view trên web (month grid)
- Task assignment cho người khác trong gia đình
- Voice input (Telegram voice message → Whisper → parser)

---

**End of plan.** Chờ user review + chốt Open Questions (Section 3) trước khi bắt đầu sub-task 7.1.

---

## 17. Implementation Notes (post-completion)

### Deviations from original plan

| Item | Plan gốc | Thực tế |
|------|----------|---------|
| Study schedule commands | Chỉ `lich hoc: <mo ta>` để tạo | Bổ sung thêm `danh sach lich hoc`, `sua lich hoc: <id> <mo ta moi>`, `huy lich hoc: <id>` (Decision #84) |
| `core_handler.py` | Sửa thêm dispatch | Refactor toàn bộ → 7 cmd_* modules; `core_handler.py` còn ~600 dòng (dispatcher + help) (Decision #83) |
| `WebChannelAdapter` | TBD trong 7.6 | Thêm `send_with_inline_keyboard` fallback (gọi `send()`, bỏ buttons) để fix crash production khi tạo task qua web |
| `task_parser.py` system prompt | Prompt sketch cơ bản | Bổ sung bảng quy đổi buổi → giờ đầy đủ + ví dụ phong phú cho `10h tối`, `22h`, `chiều thứ 3` (Decision #85) |
| `task_store.list_for_user` | `status` filter | Thêm `category` filter để `danh sach lich hoc` query đúng `category='study'` |

### Files thực tế tạo/sửa (ngoài plan Section 5)

| File | Ghi chú |
|------|---------|
| `cmd_utils.py` | Mới — pending state, ACL helpers, parsing utilities (tách từ core_handler) |
| `cmd_user.py` | Mới — user management handlers |
| `cmd_audit.py` | Mới — audit + recycle bin handlers |
| `cmd_notes.py` | Mới — note/journal handlers |
| `cmd_sudo.py` | Mới — sudo handlers |
| `cmd_wiki.py` | Mới — wiki + memory handlers |
| `cmd_task.py` | Mới — task handlers + study schedule + callback dispatcher |
| `web_channel.py` | Sửa — thêm `send_with_inline_keyboard` fallback |

### Test count thực tế
867 tests passing (mục tiêu plan ~95 test cases mới cho FR-7; tổng cộng tích lũy từ FR-1 → FR-7).
