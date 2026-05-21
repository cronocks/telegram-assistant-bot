# FR-4 — Audit + Under-18 Stealth-read + Recycle Bin + Notifications — Detailed Implementation Plan

> **Status:** DRAFT — chờ review trước khi execute
> **Created:** 2026-05-21
> **Branch:** `feature/FR4` (branch off từ `main` theo Git workflow Section 3.5)
> **Approach:** 1 PR duy nhất `feature/FR4` → `main` (umbrella); 5 sub-feature merge nội bộ trên cùng branch.

---

## 1. Goal

FR-4 hoàn thiện tầng quản trị và quan sát của hệ thống — gồm 5 sub-feature đan vào nhau:

1. **Audit log** — bảng append-only ghi mọi sự kiện có ý nghĩa pháp lý/quản trị (stealth-read, sudo, recycle ops, password set, role change, scope change, ...).
2. **Under-18 stealth-read** — admin được đọc note `private` của con dưới 18 tuổi; mọi lần đọc đều ghi audit; owner KHÔNG nhận thông báo.
3. **Recycle bin** — soft-delete đã có (`deleted_at`); FR-4 bổ sung lệnh admin xem/khôi phục/xóa hẳn + scheduled job purge sau 180 ngày.
4. **Auto-purge ở tuổi 18** — khi con tròn 18 hôm trước, mọi entry recycle bin của con bị purge vĩnh viễn; live data KHÔNG bị mutate.
5. **Notification framework** — plumbing tối thiểu để bất kỳ module nào (FR-7 sẽ là consumer chính) enqueue thông báo gửi qua `ChannelAdapter`, có retry/backoff persistent qua SQLite.

FR-4 KHÔNG bao gồm: reminder scheduling (FR-7), digest cha mẹ (FR-7), notification mirror tier 1/2 (FR-7), under-18 mirror reminder (FR-7).

---

## 2. Context & Decisions

### 2.1 Decision references

| ROADMAP ref | Nội dung |
|---|---|
| Decision #5 | Admin đọc journal của under-18 |
| Decision #6 | Stealth-read silent, có audit log (Option A) |
| Decision #14 | Recycle bin disclosed, 180 ngày, admin-only |
| Decision #22 | Auto-off ở 18 enforce runtime, KHÔNG mutate DB (live data) — auto-purge recycle bin là ngoại lệ vì purge soft-deleted data, không phải live |
| Decision #52 | FR-3 ACL strict: admin KHÔNG đọc private của người khác → stealth-read dời sang FR-4 |
| Section 4.4 | Auto-off + auto-purge ở 18 (recycle bin entries của child bị xóa) |
| Section 4.5 | Adult opt-in post-18 (chia sẻ với cha mẹ: bật/tắt) — FR-4 KHÔNG implement, chỉ chuẩn bị audit |

### 2.2 Quyết định nền tảng (chốt khi soạn plan này — bổ sung vào Decision Log khi merge)

| # | Quyết định |
|---|-----------|
| D1 | Audit log = 1 row / event, immutable. Chỉ INSERT, không UPDATE/DELETE. Schema chuẩn hoá metadata; nội dung chi tiết lưu trong cột `payload` JSON text. |
| D2 | Audit KHÔNG lưu nội dung note (content body) — chỉ lưu identifier (drive_file_id / note id / wiki_page id) + action + actor + timestamp. Tránh nhân đôi dung lượng và risk leak qua audit dump. |
| D3 | Stealth-read trigger điều kiện: `reader.role == 'admin'` AND `owner` là child của ai đó (qua `parent_links`) AND `age(owner) < 18`. Không phụ thuộc reader có phải parent của owner hay không — admin là role gia đình, không cần là cha mẹ trực tiếp. |
| D4 | Recycle bin = view trên soft-deleted rows hiện có (`deleted_at IS NOT NULL`), KHÔNG tạo bảng riêng. Lợi: không double-write; bất lợi: không reset `deleted_at` khi entity được tạo lại cùng tên — chấp nhận. |
| D5 | Auto-purge at 18: chỉ purge soft-deleted entries (`deleted_at < birthday_18`) thuộc owner vừa tròn 18 hôm trước. Live data (chưa soft-delete) giữ nguyên — owner vẫn dùng tài khoản bình thường, chỉ admin mất quyền stealth-read tự động (Decision #22 đã enforce runtime). |
| D6 | Notification queue persistent (SQLite) thay vì in-memory để survive restart. Render free tier restart container theo lịch, queue in-memory sẽ mất event. |
| D7 | Notification retry: exponential backoff `2^attempt` phút (1, 2, 4, 8, 16); max 5 attempts → status `failed`, audit row `notification_failed`. Không có alert tự động; admin tự kiểm qua `xem audit`. |
| D8 | Sudo events (`sudo_elevate` / `sudo_drop` / `sudo_fail` / `sudo_locked` / `password_set`) di chuyển từ stdout (FR-3.5) sang audit log table. Stdout giữ song song trong giai đoạn đầu để debug. |
| D9 | Audit log retention = unbounded trong FR-4. Cleanup job sẽ thêm ở FR sau nếu kích thước table thành vấn đề. |

### 2.3 Audit event taxonomy

| `action` | `target_type` | Khi nào |
|---|---|---|
| `stealth_read_note` | `note` | Admin đọc private note của child <18 |
| `stealth_read_wiki` | `wiki_page` | Admin đọc private wiki của child <18 |
| `recycle_view` | `-` | Admin chạy `xem thung rac` |
| `recycle_restore` | `note` / `wiki_page` / `user` | Admin khôi phục item |
| `recycle_purge` | `note` / `wiki_page` / `user` | Hard delete (manual hoặc auto) |
| `auto_purge_18` | `user` | Daily job phát hiện user vừa tròn 18 |
| `sudo_elevate` / `sudo_drop` / `sudo_fail` / `sudo_locked` | `-` | Migrate từ FR-3.5 stdout |
| `password_set` | `user` | Migrate từ FR-3.5 stdout |
| `role_change` | `user` | Admin đổi role qua `doi role` |
| `scope_change` | `note` / `wiki_page` | `chia se` / `bo chia se` |
| `notification_enqueued` / `notification_delivered` / `notification_failed` | `notification` | Notification framework — enqueue / send thành công / đạt max attempts (final fail) |
| `notification_retry` | `notification` | Mỗi lần send fail nhưng `attempts < 5` (chưa final). Payload: `{attempt, error, next_retry_at}`. Cho phép trace full lịch sử retry của 1 notification qua `xem audit`. |

---

## 3. Scope Breakdown

| Sub | Tên | File chính | DB | Commands mới |
|-----|-----|-----------|-----|--------------|
| 4.1 | Audit log | `audit.py` | `014_audit_log.sql` | `xem audit` |
| 4.2 | Stealth-read under-18 | `acl.py` (edit) | — | — (mở rộng can_read) |
| 4.3 | Recycle bin | `core_handler.py` (edit) + `note_index.py`/`memory_store.py` (edit) | — | `xem thung rac`, `khoi phuc: <id>`, `xoa han: <id>` |
| 4.4 | Auto-purge at 18 | `scheduled_jobs.py` (new) | — | (scheduled, không có command) |
| 4.5 | Notification framework | `notification_store.py`, `notification_service.py` | `015_notifications.sql` | — (consumer là FR-7) |

---

## 4. File Changes Summary

### 4.1 New files

| # | File | Purpose |
|---|------|---------|
| 1 | `db/migrations/014_audit_log.sql` | `audit_log` table |
| 2 | `db/migrations/015_notifications.sql` | `pending_notifications` table |
| 3 | `audit.py` | `AuditLog` Protocol + `SqliteAuditLog` adapter; helper `log(actor, action, target_type, target_id, payload)` |
| 4 | `notification_store.py` | `SqliteNotificationStore` — CRUD pending notifications + retry queue |
| 5 | `notification_service.py` | `NotificationService` adapter — bridge giữa store và `ChannelAdapter`; chứa `enqueue()` + `flush_pending()` |
| 6 | `scheduled_jobs.py` | APScheduler job definitions (180d purge, 18-birthday purge, notification flush) — wired vào `main.py` |
| 7 | `tests/test_audit_log.py` | Audit log adapter tests |
| 8 | `tests/test_recycle_bin.py` | Recycle bin command + scheduled purge flow |
| 9 | `tests/test_stealth_read.py` | Stealth-read ACL + audit emission |
| 10 | `tests/test_notification_store.py` | Queue CRUD + retry logic |
| 11 | `tests/test_auto_purge_18.py` | Birthday detection + purge of soft-deleted entries owned by child |

### 4.2 Edited files

| # | File | Change |
|---|------|--------|
| 12 | `interfaces.py` | Thêm Protocols `AuditLog`, `NotificationStore`, `NotificationService`; mở rộng `NoteIndex`/`MemoryStore` với `list_deleted()`, `restore(id)`, `purge(id)`. |
| 13 | `acl.py` | `can_read` mở rộng: admin + owner-child-under-18 → True; trả về flag `is_stealth` để caller log audit. |
| 14 | `note_index.py` | Implement `list_deleted`, `restore`, `purge`. ACL change gọi vào `can_read` mới. |
| 15 | `memory_store.py` | (Nếu áp dụng) tương tự cho memory snapshots; có thể KHÔNG cần vì memory không nằm trong recycle bin (memory bị overwrite chứ không soft-delete). Quyết định khi đọc code. |
| 16 | `user_store.py` | `list_deleted_users()`, `restore_user(id)`, `hard_delete_user(id)`. Helper `find_users_turning_18(on_date)`. |
| 17 | `core_handler.py` | 4 commands mới (`xem audit`, `xem thung rac`, `khoi phuc`, `xoa han`); call site stealth-read → audit; sudo handlers di chuyển log từ stdout sang `audit.log`. |
| 18 | `main.py` | Wire `AuditLog`, `NotificationService` vào `CoreDeps`; đăng ký APScheduler jobs từ `scheduled_jobs.py`. |
| 19 | `channel_telegram.py` | Không bắt buộc đổi (đã có `send` + `delete_message`). Nếu thêm `send_with_retry` thì để adapter quyết định — tạm thời để retry ở `notification_service` thay vì adapter. |
| 20 | `core_handler.py` (đã list ở 17) | `chia se` / `bo chia se` / `doi role` thêm gọi `audit.log` (D2 taxonomy). |

---

## 5. Database Schema

### 5.1 `014_audit_log.sql`

```sql
CREATE TABLE audit_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_user_id   INTEGER REFERENCES users(id),  -- NULL = system event (scheduled job)
    action          TEXT NOT NULL,                  -- xem 2.3
    target_type     TEXT,                            -- 'note' | 'wiki_page' | 'user' | 'notification' | NULL
    target_id       TEXT,                            -- INTEGER hoặc drive_file_id; TEXT để linh hoạt
    payload         TEXT,                            -- JSON string; NULL nếu không có metadata thêm
    created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_audit_actor_time   ON audit_log(actor_user_id, created_at DESC);
CREATE INDEX idx_audit_target_time  ON audit_log(target_type, target_id, created_at DESC);
CREATE INDEX idx_audit_action_time  ON audit_log(action, created_at DESC);
```

- Không có UPDATE/DELETE trigger — enforce immutability ở application layer (chỉ adapter `SqliteAuditLog.log()` được phép write).
- `actor_user_id` nullable cho system events (auto-purge job, notification retry, ...).

### 5.2 `015_notifications.sql`

```sql
CREATE TABLE pending_notifications (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL REFERENCES users(id),
    channel         TEXT NOT NULL,                   -- 'telegram' | 'web' | ...
    payload         TEXT NOT NULL,                   -- JSON: {kind, text, ...} — service quyết định shape
    status          TEXT NOT NULL DEFAULT 'pending', -- 'pending' | 'delivered' | 'failed'
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,                            -- truncated error message
    next_retry_at   DATETIME,                        -- NULL = ready ngay; set khi backoff
    created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    delivered_at    DATETIME
);

CREATE INDEX idx_notif_pending ON pending_notifications(status, next_retry_at)
    WHERE status = 'pending';
```

- Partial index để job retry chỉ scan rows đang pending.
- `attempts >= 5` → status set `'failed'`, audit row `notification_failed`, không retry nữa.

### 5.3 Không cần migration 016 riêng

Recycle bin tận dụng `deleted_at` sẵn có. Auto-purge ghi vào `audit_log` (action `auto_purge_18`, `recycle_purge`). Không cần bảng "recycle_purge_log" riêng — audit_log đã đủ.

(Điều chỉnh so với plan-of-plan ban đầu: bỏ migration 016.)

---

## 6. Sub-feature Flows

### 6.1 Audit log — `audit.py`

```python
class AuditLog(Protocol):
    def log(
        self,
        actor_user_id: int | None,
        action: str,
        target_type: str | None = None,
        target_id: str | None = None,
        payload: dict | None = None,
    ) -> None: ...

    def list_recent(
        self,
        limit: int = 50,
        actor_user_id: int | None = None,
        action: str | None = None,
    ) -> list[AuditEvent]: ...
```

- `SqliteAuditLog.log()` `json.dumps(payload)` trước khi insert.
- `list_recent()` dùng cho command `xem audit`; admin-only, paginated.

### 6.2 Stealth-read under-18 — mở rộng `acl.py`

```python
def can_read(
    reader: User,
    owner_user_id: int,
    scope: str,
    *,
    user_store: UserStore,  # mới: để check parent_links + birthdate
) -> tuple[bool, bool]:
    """Return (allowed, is_stealth).

    is_stealth = True nếu reader đọc được nhờ stealth-read (caller phải log audit).
    """
```

- Existing rules giữ nguyên (owner = reader → True; scope `everyone` → True).
- Mới: nếu `reader.role == 'admin'` AND `_is_minor_child_of_anyone(owner_user_id, user_store)` → return `(True, True)`.
- Caller (note_index, wiki_client) check `is_stealth` → gọi `audit.log('stealth_read_note', target_id=...)`.

### 6.3 Recycle bin commands

| Command | Behavior |
|---|---|
| `xem thung rac` | Admin-only. Liệt kê `notes` + `wiki_pages` + `users` có `deleted_at IS NOT NULL`, sorted desc by `deleted_at`. Format: `[note/wiki/user] [id] [title/name] — đã xóa <X> ngày trước`. Audit row `recycle_view`. |
| `khoi phuc: <id>` | Admin-only. Resolve id (cần prefix: `note 12`, `wiki 5`, `user 3`). Clear `deleted_at`. Audit row `recycle_restore`. |
| `xoa han: <id>` | Admin-only. Hard delete row. Đối với note/wiki: cũng gọi adapter Drive xóa file (best-effort). Audit row `recycle_purge`. |

Resolution `<id>`: format `<kind> <id>` (vd `note 12`). Tránh ambiguity giữa note id và wiki id.

### 6.4 Auto-purge at 18 — scheduled job

`scheduled_jobs.py` định nghĩa 3 job:

| Job | Cron | Logic |
|---|---|---|
| `purge_recycle_bin_180d` | `0 3 * * *` (3h sáng UTC+7) | Scan rows có `deleted_at < now - 180 days` (notes, wiki_pages, users). Hard delete + audit `recycle_purge` (system actor). |
| `purge_children_turning_18` | `0 3 * * *` (3h sáng UTC+7) | `find_users_turning_18(today - 1 day)` → mỗi user purge tất cả soft-deleted notes/wiki owned by user (bất kể tuổi `deleted_at`). Audit `auto_purge_18` + chi tiết trong payload. |
| `flush_pending_notifications` | mỗi 30 giây | Scan `pending_notifications` có `status='pending'` AND (`next_retry_at IS NULL` OR `next_retry_at <= now`). Mỗi row: try send qua channel adapter. Success → `delivered`; fail → tăng `attempts`, set `next_retry_at = now + 2^attempts phút`; nếu `attempts >= 5` → `failed`. |

Wiring trong `main.py`:
```python
from apscheduler.schedulers.asyncio import AsyncIOScheduler
scheduler = AsyncIOScheduler(timezone="Asia/Ho_Chi_Minh")
register_jobs(scheduler, deps)
scheduler.start()
```

### 6.5 Notification framework

```python
class NotificationService(Protocol):
    async def enqueue(
        self, user_id: int, channel: str, payload: dict,
    ) -> int: ...  # return notification id

    async def flush_pending(self) -> None: ...  # called by scheduler
```

- `enqueue` chỉ ghi DB + audit `notification_enqueued`. Không gửi ngay (tránh blocking caller).
- `flush_pending` được scheduler gọi định kỳ — đọc queue, gửi qua `ChannelAdapter`, update status, ghi audit:
  - **Success** → `status='delivered'`, audit `notification_delivered` (payload: `{total_attempts: N}`).
  - **Fail nhưng `attempts < 5`** → tăng `attempts`, set `next_retry_at = now + 2^attempts phút`, audit `notification_retry` (payload: `{attempt, error, next_retry_at}`). Status vẫn `'pending'`.
  - **Fail và `attempts >= 5`** → `status='failed'`, audit `notification_failed` (payload: `{last_error, total_attempts: 5}`).
- Payload schema: `{"kind": "text" | "...", "text": "...", "extra": {...}}`. FR-7 sẽ định nghĩa thêm kinds (`reminder`, `digest`, ...).
- **Observability:** chạy `xem audit notification` cho ra full lịch sử của 1 notification — enqueue → retry × N → delivered/failed — theo đúng thứ tự thời gian.

---

## 7. Commands

| Command ID | VN prefix | Mô tả | Ai dùng |
|---|---|---|---|
| `XEM_AUDIT` | `xem audit`, `xem audit <action>` | Liệt kê 50 audit events gần nhất (paginated nếu cần) | admin |
| `XEM_THUNG_RAC` | `xem thung rac` | Liệt kê items trong recycle bin | admin |
| `KHOI_PHUC` | `khoi phuc: <kind> <id>` | Khôi phục item (vd `khoi phuc: note 12`) | admin |
| `XOA_HAN` | `xoa han: <kind> <id>` | Hard delete ngay, skip retention 180d | admin |

- Tất cả command admin-only check qua `has_role(user, "admin")` (cho phép cả natively-admin và elevated-admin từ FR-3.5).
- `XEM_THUNG_RAC` / `XEM_AUDIT` thêm vào `_QUOTA_EXEMPT` (admin ops không tiêu LLM).
- `xem audit` hỗ trợ filter optional: `xem audit sudo_elevate` → chỉ events action đó.

---

## 8. Security

| Biện pháp | Chi tiết |
|---|---|
| Audit immutability | `SqliteAuditLog` là module duy nhất write vào `audit_log`; không expose method UPDATE/DELETE. Migration 014 không thêm trigger (giữ schema gọn). |
| Audit không lưu nội dung | Chỉ identifier + metadata. Tránh tăng dung lượng + giảm bề mặt leak. |
| Stealth-read scope chặt | Chỉ áp dụng cho `owner` là child <18 (qua `parent_links` + `birthdate`). Người lớn không bị stealth-read kể cả là `member`. |
| Rate-limit `xem audit` | Không gửi raw dump > 50 rows / call. Pagination bắt buộc. |
| Notification payload sanitization | Truncate `last_error` max 500 chars để tránh stack trace dài. |
| Recycle bin admin-only | Cả 3 lệnh check `has_role(user, "admin")`. |

---

## 9. Risk & Impact

**Risk: `medium-high`**

| Risk | Mitigation |
|---|---|
| ACL change ảnh hưởng mọi retrieval path | Unit test matrix: owner-self / member-other-everyone / admin-adult / admin-child-under-18; expected `is_stealth` flag. |
| Scheduled job race với write | Job chạy trong cùng process (APScheduler async); SQLite transactions đủ isolate. Test: run job + insert đồng thời → no lost write. |
| Notification queue grow nếu adapter chết | Status `failed` sau 5 attempts; alert manual qua `xem audit`. FR sau có thể thêm cleanup hoặc dashboard. |
| Auto-purge chạy 2 lần (idempotency) | Dùng `WHERE deleted_at < ...` — sau lần purge đầu, row biến mất, lần sau không trùng. Job idempotent by design. |
| Migration 014/015 trên DB production | Chạy trên fresh + restore từ R2 → verify schema_version table tăng đúng. |
| Stealth-read leak qua audit dump | `xem audit` chỉ admin xem; bản thân admin đã có quyền stealth-read → không phải leak. |

**Impact lên codebase:**

- Mọi handler liên quan đến `can_read` cần đổi signature (thêm `user_store` param). Touch nhiều file nhưng change cơ học.
- Sudo handlers ở FR-3.5 cần migrate stdout → audit table (Decision D8). Test FR-3.5 (`test_sudo.py`) cần update expectation.

---

## 10. Dependencies

- **Python packages:** không thêm. APScheduler đã có (FR-1/FR-2 đã import); `json` stdlib cho payload.
- **Env vars:** không thêm. Cron schedule hard-code trong `scheduled_jobs.py` (offset từ UTC+7).
- **Migrations:** 014, 015 chạy tự động lúc startup.
- **FR phụ thuộc:** FR-2 (users, parent_links, birthdate), FR-3 (notes, wiki_pages, scope), FR-3.5 (sudo events migrate sang audit).

---

## 11. Test Plan

### 11.1 Unit tests

| Module | Coverage |
|---|---|
| `audit_log` | `log()` insert; `list_recent()` filter by actor/action; payload JSON round-trip; NULL actor (system event). |
| `acl.can_read` | Matrix 8 cases: owner-self, owner-other-private, owner-other-everyone × reader-admin / non-admin × owner-child-under-18 / owner-adult. Verify `is_stealth` flag đúng. |
| `note_index` | `list_deleted`, `restore`, `purge` các trường hợp: tồn tại / không tồn tại / đã purge rồi. |
| `notification_store` | `enqueue` + `next_retry_at` calculation; `mark_delivered` / `mark_failed` transitions; status filter. |
| `scheduled_jobs.purge_recycle_bin_180d` | Setup: rows với `deleted_at` cũ 200d, 100d, NULL → chỉ row 200d bị purge; audit row đúng. |
| `scheduled_jobs.purge_children_turning_18` | Setup: user A birthdate = today-18y; soft-delete notes của A → job purge note đó + audit. User B birthdate = today-17y → không bị purge. |
| `notification_service.flush_pending` | Mock channel send: (a) success ngay → delivered + audit `notification_delivered`; (b) raise → attempts++, next_retry_at set, audit `notification_retry` mỗi lần fail trung gian; (c) sau 5 fails → status failed + audit `notification_failed`. |
| `notification_service.flush_pending` — retry trace | Fail 2 lần rồi success ở lần 3 → verify đúng 4 audit rows theo thứ tự: `notification_enqueued`, `notification_retry` (attempt=1), `notification_retry` (attempt=2), `notification_delivered` (total_attempts=3). Payload mỗi row có `attempt` và `error` đúng. |

### 11.2 Integration / staging smoke

- Stealth-read: tạo user con với birthdate 10y tuổi, admin chạy `xem <file-private-cua-con>` → đọc được, audit row xuất hiện trong `xem audit`.
- Stealth-read tắt khi con tròn 18: backdate birthdate qua admin → re-test → admin không đọc được nữa.
- Recycle bin: soft-delete 1 user qua `xoa user`; `xem thung rac` thấy user đó; `khoi phuc: user <id>` → user active lại; `xoa han: user <id>` → user biến mất hẳn.
- Notification: trigger `enqueue` qua script test → 30s sau message xuất hiện trên Telegram; audit `notification_delivered` ghi nhận.
- Notification retry: tạm tắt bot token → `enqueue` 1 notification → quan sát attempts tăng dần, `next_retry_at` đẩy ra; bật lại token → notification delivered ở attempt tiếp theo.
- Migration: deploy lên staging fresh DB → 014, 015 apply; restore từ R2 backup → migration runner idempotent skip.

---

## 12. Definition of Done

- [ ] Migration 014, 015 apply OK trên fresh + restore DB
- [ ] `pytest` pass 100% (test count >= 280 — hiện 256, +24 từ 5 file test mới)
- [ ] Smoke test (11.2) pass trên staging
- [ ] Sudo events từ FR-3.5 đã migrate stdout → audit table; `test_sudo.py` update + pass
- [ ] `xem audit` hiển thị đúng events; pagination hoạt động
- [ ] Recycle bin 3 lệnh hoạt động; scheduled job 180d chạy đúng giờ trên staging (verify qua audit row)
- [ ] Auto-purge at 18: test bằng cách backdate birthdate trên staging, daily job purge + audit
- [ ] Notification framework: enqueue/deliver/retry/failed states transition đúng
- [ ] Audit log có row `notification_retry` cho mỗi fail trung gian (không chỉ final `notification_failed`); verify được full trace của 1 notification qua `xem audit notification <id>`
- [ ] ROADMAP cập nhật: FR-4 status DONE; Decision Log thêm các quyết định D1–D9
- [ ] `docs/architecture_vn.md` + `docs/architecture_en.md` cập nhật: thêm bảng `audit_log`, `pending_notifications`; subsection Audit + Recycle Bin trong Section 6; commands mới trong Section 7

---

## 13. Open Issues (resolve khi code)

1. **Memory store và recycle bin** — memory snapshots bị overwrite chứ không soft-delete. Có cần version hóa memory để recycle không? Giả định ban đầu: KHÔNG (memory được curate liên tục, recycle không phù hợp).
2. **Stealth-read scope mở rộng** — admin có nên đọc được wiki private của child <18? Plan hiện tại: CÓ (đối xứng với note). Confirm khi code.
3. **Notification payload shape** — FR-4 chỉ định nghĩa skeleton `{kind, text, extra}`. FR-7 sẽ standardize kinds; có thể cần migration sau để bổ sung field.
4. **Recycle bin cho `users`** — restore user có cần khôi phục cả `channel_bindings` không? Hiện tại binding bị soft-delete cascade? Cần verify trong code FR-2.
5. **Audit log dump cho legal request** — chưa cần export tool trong FR-4; admin đọc qua `xem audit` là đủ. Export tool có thể là FR-6 (Backup/Restore).
6. **Timezone của scheduled job** — chốt UTC+7 (`Asia/Ho_Chi_Minh`) cho cron tại Việt Nam; verify APScheduler config.

---

**End of FR-4 Plan**

> Đọc cùng `docs/ROADMAP.md` Section 5 (FR-4 entry), Section 4.4/4.5 (auto-off + opt-in), và Decisions #5, #6, #14, #22, #52.
