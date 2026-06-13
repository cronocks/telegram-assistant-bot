# Kiến Trúc Hệ Thống

> Tài liệu này mô tả kiến trúc của Telegram Claude Bot tính đến **FR-11** (Family Genealogy / Gia phả).
> Xem lộ trình phát triển đầy đủ tại [`ROADMAP.md`](ROADMAP.md).

---

## 1. Tổng Quan

Hệ thống **quản lý kiến thức cá nhân và gia đình** đa kênh, giao tiếp chủ yếu qua Telegram, được hỗ trợ bởi Anthropic Claude. Thiết kế cho quy mô gia đình (~10 người), không phải SaaS công cộng.

**Mục tiêu cốt lõi:**
- Ghi chú, nhật ký, hỏi đáp wiki qua ngôn ngữ tự nhiên
- Đa người dùng với phân quyền và giám sát của cha mẹ
- Core không phụ thuộc kênh: cùng business logic hoạt động trên Telegram hôm nay, Web UI hoặc Discord trong tương lai
- Tự host được, chi phí thấp (Render free tier + Cloudflare R2 free tier)

---

## 2. Kiến Trúc — Hexagonal (Ports & Adapters)

Hệ thống dùng **Modular Monolith** với kiến trúc hexagonal. Business logic được tổ chức trong các module `cmd_*.py` và chỉ phụ thuộc vào *Protocols* (interfaces), không bao giờ phụ thuộc vào concrete adapter. `core_handler.py` đóng vai trò dispatcher — route lệnh tới đúng `cmd_*` handler. Các adapter được kết nối (wire) tại `main.py`.

```
                  ┌──────────────────────┐
                  │    core_handler.py   │  ← business logic
                  │   handle_message()   │     không phụ thuộc kênh
                  └──────────┬───────────┘
                             │ chỉ phụ thuộc Protocols (interfaces.py)
              ┌──────────────┼──────────────┐
              ▼              ▼              ▼
         LLMClient      NoteStore      WikiStore
         UserStore      ChannelAdapter
              ▲              ▲              ▲
              │  được implement bởi         │
              │                             │
       AnthropicLLM    DriveNoteStore  DriveWikiStore
       TelegramAdapter  UserStore(SQLite)
       (tương lai: OllamaLLM, LocalFSNoteStore, DiscordAdapter, WebAdapter)
```

**Nguyên tắc quan trọng:** Các `cmd_*.py` module không bao giờ import concrete class. Toàn bộ việc kết nối adapter xảy ra tại `main.py`.

**Tại sao chọn Modular Monolith (không phải Microservice):**

| Vấn đề | Microservice | Modular Monolith (đã chọn) |
|--------|-------------|---------------------------|
| Quy mô user | Hàng nghìn | ~10 thành viên gia đình |
| Đội dev | Nhiều team | 1 người |
| Free tier | Nhiều slot RAM | 1 process |
| Transaction | Distributed phức tạp | SQLite ACID đơn giản |
| Debug | Cross-service trace | 1 stack trace |

---

## 3. Cấu Trúc File

| File | Vai trò |
|------|---------|
| `main.py` | Wiring layer — khởi tạo adapter, route webhook, health check |
| `interfaces.py` | Protocols + `ChannelMessage` dataclass — lớp contract |
| `core_handler.py` | Command dispatcher + `/start` + `/help`; route message tới `cmd_*` handlers (FR-7 refactor) |
| `deps.py` | `CoreDeps` dataclass — gom tất cả dependency inject vào handlers (FR-4 refactor) |
| `cmd_utils.py` | Shared helpers: pending state machine, ACL filter helpers, parsing utilities (FR-7) |
| `cmd_user.py` | User management handlers: `them user`, `xoa user`, `doi role`, `dat birthdate`, `dat cha`, v.v. (FR-7) |
| `cmd_audit.py` | Audit + recycle bin handlers: `xem audit`, `xem thung rac`, `khoi phuc`, `xoa han` (FR-7) |
| `cmd_notes.py` | Note/journal handlers: `ghi nho`, `nhat ky`, `xem`, `liet ke`, `tim`, `chia se` (FR-7) |
| `cmd_sudo.py` | Sudo handlers: `sudo`, `thoat sudo`, `dat mat khau`, `dat web pass` (FR-7) |
| `cmd_wiki.py` | Wiki + memory handlers: `wiki`, `hoi wiki`, `xem tri nho`, `cap nhat tri nho` (FR-7) |
| `cmd_task.py` | Task + study schedule handlers + inline keyboard callback dispatcher (FR-7) |
| `anniversary_store.py` | `SqliteAnniversaryStore` — CRUD kỷ niệm + soft-delete + validation; `family_member_id` link tới gia phả (FR-8, FR-11) |
| `anniversary_engine.py` | `AnniversaryEngine` — `compute_year()`, `tick()`, `cancel_all_for_anniversary()`; fire 08:00 VN, grace 12h; nhận `burial_store` để đính kèm thông tin mộ phần vào nhắc giỗ (FR-8, FR-11) |
| `lunar_utils.py` | `lunar_to_solar()` + `compute_anniversary_solar_date()`; lib `lunardate==0.2.2` (FR-8) |
| `cmd_anniversary.py` | 5 Telegram handlers: `them ky niem`, `danh sach ky niem`, `ky niem <id>`, `xoa ky niem`, `sua ky niem` (FR-8) |
| `category_store.py` | `SqliteCategoryStore` — CRUD danh mục chi/thu + family-shared scope (`user_id IS NULL`) (FR-9) |
| `ledger_store.py` | `SqliteLedgerStore` — entry CRUD + monthly aggregates + 7-day query + void (soft-delete) + 30-day purge (FR-9) |
| `budget_store.py` | `SqliteBudgetStore` — upsert `(user_id, month)`, threshold alert state JSON (FR-9) |
| `ledger_parser.py` | `LedgerParser` — parse amount (k/tr/m suffix, VND integer) + fast-path Vietnamese keyword + fuzzy category match (FR-9) |
| `ledger_reports.py` | `LedgerReports` — monthly summary, yearly breakdown, 7-day view, threshold check 80%/100% (FR-9) |
| `cmd_ledger.py` | 16 Telegram handlers: `chi:`, `thu:`, `danh sach ghi chep`, `sua/huy ghi chep:`, `xem/them/xoa/sua danh muc`, `bao cao thang/nam`, `xem chi tieu`, `dat han muc chi:`, `dat muc tieu tiet kiem:`, `xem han muc` (FR-9) |
| `family_store.py` | `SqliteFamilyStore` — CRUD hồ sơ người thân + tìm kiếm `normalize_vn` + quản lý quan hệ (cha/mẹ/vợ/chồng/con nuôi) với phát hiện vòng lặp qua recursive CTE (FR-11) |
| `burial_store.py` | `SqliteBurialStore` — CRUD bản ghi mộ phần; xoay `is_current` khi thêm mộ mới (cải táng); validate GPS (FR-11) |
| `family_tree.py` | `ancestors()`, `descendants()`, `family_roots()`, `render_tree()` — cây gia phả text (chat); `build_tree_structure()` — cây theo đời dạng card (web, vợ/chồng ghép cặp, SVG connector); dùng recursive CTE (FR-11) |
| `cmd_family.py` | Telegram handlers cho gia phả: `them nguoi than`, `xem nguoi than`, `danh sach nguoi than`, `sua nguoi than`, `xoa nguoi than`, `them mo phan`, `sua mo phan`, `xoa mo phan`, `tim mo`, `them quan he`, `xoa quan he`, `gia pha` (FR-11) |
| `csrf.py` | `CSRFMiddleware` — double-submit cookie CSRF; set cookie non-HttpOnly trên GET, validate cookie vs form-field/header trên POST/PUT/PATCH/DELETE; `/webhook` được exempt *(Security hardening)* |
| `rate_limit.py` | `RateLimitMiddleware` — sliding-window per `(IP, path)`; `/login` giới hạn 10 req/60s, default 120 req/60s cho các route khác; không dùng external dependency *(Security hardening)* |
| `security_headers.py` | `SecurityHeadersMiddleware` — stamp `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy`, `Content-Security-Policy`, `Strict-Transport-Security` (chỉ staging/prod) *(Security hardening)* |
| `channel_telegram.py` | `TelegramAdapter` — parse Telegram webhook payload, gửi reply, `send_with_inline_keyboard` |
| `claude_client.py` | `AnthropicLLM` — wrapper Anthropic SDK |
| `drive_client.py` | `DriveNoteStore` — lưu trữ ghi chú trên Google Drive |
| `wiki_client.py` | `DriveWikiStore` — wiki trên Google Drive, dùng LLM qua DI |
| `user_store.py` | `UserStore` — registry người dùng SQLite, quota, parent links, password, task prefs |
| `note_index.py` | `SqliteNoteIndex` — lớp ACL/index SQLite ánh xạ Drive file ID → owner + scope |
| `memory_store.py` | `SqliteMemoryStore` — L1 memory (2 slot `memory` và `user` mỗi user) |
| `task_store.py` | `SqliteTaskStore` — CRUD task, query by user/status/category, soft-delete (FR-7) |
| `reminder_store.py` | `SqliteReminderStore` — CRUD reminder, ready-to-fire query, cancel by task (FR-7) |
| `reminder_engine.py` | `ReminderEngine` — scan + emit + lazy recurring expansion + parent mirror + grace window (FR-7) |
| `task_parser.py` | `TaskParser` — Haiku 4.5 tool-use; parse free-form Vietnamese → `{title, deadline, recurring_rule}` (FR-7) |
| `elevation_store.py` | `SqliteElevationStore` — phiên nâng quyền sudo + rate-limit thất bại (FR-3.5) |
| `audit.py` | `SqliteAuditLog` — ghi sự kiện audit append-only; Protocol `AuditLog` (FR-4) |
| `notification_store.py` | `SqliteNotificationStore` — CRUD hàng đợi thông báo persistent (FR-4) |
| `notification_service.py` | `NotificationService` — bridge store ↔ `ChannelAdapter`; `enqueue()` + `flush_pending()` (FR-4) |
| `scheduled_jobs.py` | APScheduler jobs: purge 180d, purge-at-18, flush notifications, scan_reminders, daily_summary, parent_digest, anniversary_tick, compute_anniversary_year, weekly_ledger_summary, purge_voided_ledger (FR-4, FR-7, FR-8, FR-9) |
| `web_session_store.py` | `SqliteWebSessionStore` — session web DB-revocable (không JWT); find/revoke/create (FR-5) |
| `web_channel.py` | `WebChannelAdapter` — SSE queue per `conversation_id`; `send_with_inline_keyboard` fallback (FR-5, FR-5.5, FR-7) |
| `web_context.py` | Shared globals cho tất cả sub-router: store references, token helpers, cookie helpers, `_resolve_user`, `init_web_router()` — populated một lần khi startup (FR-5 → FR-11) |
| `web_router.py` | Thin aggregator: re-export `init_web_router`; `include_router` cho tất cả sub-router theo thứ tự; `__getattr__` backward-compat (FR-5 → FR-11) |
| `web_auth.py` | Routes `/`, `/login`, `/logout`, `/setup-password`, `/settings/password` (FR-5) |
| `web_chat.py` | Routes `/chat`, `/chat/stream` (SSE), `/chat/{conv_id}`, `/chat/send`, `/chat/{conv_id}/send`, `/api/conversations*` (FR-5, FR-5.5) |
| `web_tasks.py` | Routes `/tasks`, `/api/tasks*` (FR-7) |
| `web_anniversaries.py` | Routes `/anniversaries*` (FR-8) |
| `web_ledger.py` | Routes `/ledger*` — entries, categories, budget, report (FR-9) |
| `web_admin.py` | Routes `/admin/*`, `/settings/export*` — admin stealth-read, export/import ZIP (FR-5.5.6, FR-6) |
| `web_family.py` | Routes `/family*` — member CRUD, family tree view (FR-11) |
| `web_conversation_store.py` | `SqliteWebConversationStore` — CRUD conversation + message; search LIKE; admin stealth-read path (FR-5.5) |
| `backup_engine.py` | `BackupEngine` — export ZIP in-memory, parse/apply import transactional, upload Drive `Claude-Notes/Backups/`, rate-limit 5 phút/user (FR-6) |
| `tools/local_migrate.py` | CLI standalone: copy SQLite + mirror Drive files → local FS; `--dry-run`, `--users`, `--include-deleted` (FR-6) |
| `templates/` | Jinja2 templates: `login.html`, `setup_password.html`, `chat.html`, `import.html`, `tasks.html`, `task_form.html`, `task_view.html`, `anniversaries.html`, `anniversary_form.html`, `anniversary_view.html`, `ledger.html`, `ledger_entry_form.html`, `ledger_categories.html`, `ledger_report.html`, `ledger_budget.html`, `family_members.html`, `family_member_view.html`, `family_member_form.html`, `family_tree.html` (FR-5 → FR-11) |
| `acl.py` | ACL helpers (`can_read`, `filter_visible`) dùng bởi các retrieval path |
| `auth.py` | Argon2id password hashing (hạ tầng từ FR-2; FR-3.5 dùng để verify mật khẩu sudo) |
| `permissions.py` | Permission helpers theo role |
| `text_utils.py` | Chuẩn hóa dấu tiếng Việt, multi-prefix command matcher |
| `timeutils.py` | Helpers UTC+7 |
| `cost_monitor.py` | Theo dõi chi phí LLM, cảnh báo ngưỡng |
| `security.py` | Kiểm soát truy cập folder Drive (OAuth scope, folder whitelist, MIME whitelist, rate limit file/giờ); `set_audit_sink()` để route Drive audit events vào SQLite `audit_log` thay vì chỉ stdout *(Security hardening)* |
| `config.py` | Load biến môi trường |
| `db/connection.py` | SQLite connection factory |
| `db/migrations.py` | Migration runner idempotent dựa trên file |
| `db/migrations/*.sql` | File SQL migration (001–032) |

---

## 4. Tech Stack

| Tầng | Công nghệ |
|------|----------|
| Runtime | Python 3.11 |
| Web framework | FastAPI + Uvicorn |
| Scheduler | APScheduler |
| HTTP client | httpx (async) |
| LLM | Anthropic Claude (qua adapter `AnthropicLLM`) |
| Embeddings | Voyage AI `voyage-3-lite` (kế hoạch — L3 vector, FR tương lai) |
| Ghi chú / Wiki | Google Drive |
| Database người dùng | SQLite (qua `sqlite3` stdlib) |
| Replicate DB | Litestream → Cloudflare R2 (WAL streaming, lag ~1 giây) |
| Mã hóa mật khẩu | argon2-cffi (Argon2id) |
| Kênh chính | Telegram Bot API (webhook mode) |
| Hosting | Render.com (Docker, free tier) |
| Object storage | Cloudflare R2 (tương thích S3, miễn phí egress) |

---

## 5. Data Model

Toàn bộ dữ liệu người dùng lưu trong SQLite. Migration chạy tự động khi khởi động qua `db/migrations.py`.

### Các bảng

#### `users`
Bảng định danh cốt lõi. Mỗi người dùng đã đăng ký là một row. User bị soft-delete có `deleted_at` được set; có unique index trên `name` loại trừ row đã xóa để tên có thể tái dùng.

| Cột | Kiểu | Ghi chú |
|-----|------|---------|
| `id` | INTEGER PK | Auto-increment |
| `name` | TEXT NOT NULL | Tên hiển thị; unique trong số user còn active |
| `username` | TEXT UNIQUE NOCASE | Handle tùy chọn; CHECK regex `[A-Za-z0-9_.-]{3,32}` |
| `role` | TEXT NOT NULL | `admin` \| `manager` \| `member` \| `readonly` |
| `birthdate` | DATE | Ngày ISO, nullable |
| `password_hash` | TEXT | Hash Argon2id; NULL cho tới khi được set |
| `must_change_password` | INTEGER | 0 = bình thường; 1 = force-reset lần đăng nhập web tiếp theo (FR-5) |
| `created_at` | DATETIME | Default `CURRENT_TIMESTAMP` |
| `deleted_at` | DATETIME | Mốc soft-delete; NULL = đang active |
| `daily_summary_time` | TEXT | `NULL` = 21:00 mặc định; `'off'` = tắt; `'HH:MM'` = tùy chỉnh *(FR-7)* |
| `morning_default_time` | TEXT | `NULL` = 09:00 mặc định; `'HH:MM'` = tùy chỉnh — dùng khi task không có giờ cụ thể *(FR-7)* |

#### `channel_bindings`
Ánh xạ `chat_id` Telegram (hoặc định danh kênh khác) tới user.

| Cột | Kiểu | Ghi chú |
|-----|------|---------|
| `user_id` | INTEGER FK → users | |
| `channel` | TEXT | `telegram` \| `web` \| … |
| `chat_id` | TEXT | Định danh hội thoại phía kênh (ví dụ `chat_id` Telegram) |
| PRIMARY KEY | `(channel, chat_id)` | |

#### `invite_codes`
Mã mời dùng một lần do admin tạo cho người dùng mới đăng ký.

| Cột | Kiểu | Ghi chú |
|-----|------|---------|
| `code` | TEXT PK | Hex ngẫu nhiên |
| `role` | TEXT | Role gán khi dùng mã |
| `name` | TEXT | Tên hiển thị gợi ý |
| `expires_at` | DATETIME | TTL 7 ngày |
| `used_at` | DATETIME NULL | Set khi dùng |

#### `birthdate_changes`
Yêu cầu thay đổi ngày sinh đang chờ duyệt bởi admin/manager.

| Cột | Kiểu | Ghi chú |
|-----|------|---------|
| `user_id` | INTEGER FK → users | |
| `requested_date` | TEXT | Ngày ISO |
| `status` | TEXT | `pending` \| `approved` \| `rejected` |
| `reviewed_by` | INTEGER FK → users | Nullable |

#### `username_changes`
Yêu cầu đổi username đang chờ duyệt (lần đầu set là trực tiếp; đổi lần sau cần admin duyệt + cooldown 30 ngày).

| Cột | Kiểu | Ghi chú |
|-----|------|---------|
| `user_id` | INTEGER FK → users | |
| `requested_username` | TEXT | |
| `status` | TEXT | `pending` \| `approved` \| `rejected` |

#### `parent_links`
Quan hệ cha-con many-to-many. Hỗ trợ gia đình 2 cha mẹ, ly hôn, ông bà làm người giám hộ.

| Cột | Kiểu | Ghi chú |
|-----|------|---------|
| `parent_user_id` | INTEGER FK → users | |
| `child_user_id` | INTEGER FK → users | |
| `digest_frequency` | TEXT | `daily` \| `weekly` \| `monthly` \| `off` |
| `digest_time` | TEXT | Ví dụ: `21:00`, `SUN 20:00`, `1 20:00` |
| `adult_optin_enabled` | BOOLEAN | Chỉ có nghĩa khi con ≥ 18 tuổi |
| PRIMARY KEY | `(parent_user_id, child_user_id)` | |

#### `user_quotas`
Quota token LLM per user theo tháng. Một row mỗi user; cột `month` tự reset lazily khi ghi đầu tiên của tháng mới. `monthly_token_limit = 0` nghĩa là không giới hạn.

| Cột | Kiểu | Ghi chú |
|-----|------|---------|
| `user_id` | INTEGER PK FK → users | Một row mỗi user |
| `monthly_token_limit` | INTEGER | 0 = không giới hạn |
| `used_tokens` | INTEGER | Lũy kế tháng này |
| `month` | TEXT | `YYYY-MM` — dùng để auto-reset lazy |
| `updated_at` | TEXT | ISO timestamp |

#### `notes` *(FR-3)*
Lớp ACL/index SQLite cho file note và journal lưu trên Google Drive. Drive giữ nội dung; bảng này giữ owner + scope để kiểm soát truy cập.

| Cột | Kiểu | Ghi chú |
|-----|------|---------|
| `id` | INTEGER PK | Auto-increment |
| `drive_file_id` | TEXT UNIQUE NOT NULL | Google Drive file ID |
| `owner_user_id` | INTEGER FK → users | |
| `scope` | TEXT NOT NULL | `private` \| `everyone` — default `private` |
| `kind` | TEXT NOT NULL | `note` \| `journal` — default `note` |
| `title` | TEXT | Tùy chọn |
| `created_at` / `updated_at` / `deleted_at` | TEXT | ISO timestamps |

Index: `(owner_user_id)`, `(scope)`.

#### `wiki_pages` *(FR-3)*
Lớp ACL/index SQLite cho wiki page lưu trên Google Drive. Default scope `everyone` — wiki là tri thức chung của gia đình.

| Cột | Kiểu | Ghi chú |
|-----|------|---------|
| `id` | INTEGER PK | Auto-increment |
| `drive_file_id` | TEXT UNIQUE NOT NULL | Google Drive file ID |
| `owner_user_id` | INTEGER FK → users | |
| `scope` | TEXT NOT NULL | `private` \| `everyone` — default `everyone` |
| `topic` | TEXT NOT NULL | Tên topic dễ đọc |
| `slug` | TEXT NOT NULL | Định danh an toàn cho filesystem |
| `created_at` / `updated_at` / `deleted_at` | TEXT | ISO timestamps |

Index: `(owner_user_id)`, `(scope)`, `(slug)`.

#### `user_memory` *(FR-3)*
L1 memory store. Hai slot có tên mỗi user, được LLM curate theo yêu cầu.

| Cột | Kiểu | Ghi chú |
|-----|------|---------|
| `user_id` | INTEGER FK → users | |
| `kind` | TEXT NOT NULL | `memory` (facts cuộn) \| `user` (profile ổn định) |
| `content` | TEXT NOT NULL | Default rỗng |
| `updated_at` | TEXT | ISO timestamp |
| `curated_at` | TEXT | Mốc curate gần nhất; NULL = chưa curate lần nào |
| PRIMARY KEY | `(user_id, kind)` | |

#### `elevation_sessions` *(FR-3.5)*
Phiên nâng quyền sudo, một dòng / `(channel, chat_id)`. Re-elevate sẽ refresh `expires_at`. Hết hạn xử lý lazy (`get_active_session` chỉ trả dòng còn hạn).

| Cột | Kiểu | Ghi chú |
|-----|------|---------|
| `channel` | TEXT NOT NULL | `telegram` \| `web` \| … |
| `chat_id` | TEXT NOT NULL | Định danh hội thoại phía kênh |
| `base_user_id` | INTEGER FK → users | User thật đứng sau phiên (manager); KHÔNG đổi danh tính |
| `started_at` | DATETIME | Default `CURRENT_TIMESTAMP` |
| `expires_at` | DATETIME NOT NULL | TTL 15 phút từ lúc elevate |
| PRIMARY KEY | `(channel, chat_id)` | |

#### `sudo_attempts` *(FR-3.5)*
Đếm số lần nhập sai mật khẩu sudo per chat; quá ngưỡng thì khóa. Reset khi sudo thành công.

| Cột | Kiểu | Ghi chú |
|-----|------|---------|
| `channel` | TEXT NOT NULL | |
| `chat_id` | TEXT NOT NULL | |
| `failed_count` | INTEGER | Mặc định 0; ≥ `SUDO_MAX_FAILS` (5) → set `locked_until` |
| `locked_until` | DATETIME NULL | Mốc hết khóa (15 phút sau lần fail cuối) |
| `last_attempt_at` | DATETIME NULL | |
| PRIMARY KEY | `(channel, chat_id)` | |

#### `audit_log` *(FR-4)*
Bảng append-only ghi mọi sự kiện có ý nghĩa pháp lý/quản trị. Chỉ INSERT — không bao giờ UPDATE/DELETE. `actor_user_id` nullable cho system events (scheduled job, ...).

| Cột | Kiểu | Ghi chú |
|-----|------|---------|
| `id` | INTEGER PK | Auto-increment |
| `actor_user_id` | INTEGER FK → users | NULL = sự kiện hệ thống (scheduled job) |
| `action` | TEXT NOT NULL | Tên sự kiện (xem taxonomy Section 6) |
| `target_type` | TEXT | `note` \| `wiki_page` \| `user` \| `notification` \| NULL |
| `target_id` | TEXT | Drive file ID hoặc integer id; TEXT để linh hoạt |
| `payload` | TEXT | JSON string; NULL nếu không có metadata thêm |
| `created_at` | DATETIME | Default `CURRENT_TIMESTAMP` |

Indexes: `(actor_user_id, created_at DESC)`, `(target_type, target_id, created_at DESC)`, `(action, created_at DESC)`.

#### `pending_notifications` *(FR-4)*
Hàng đợi thông báo persistent. Survive restart (không dùng in-memory queue). Job `flush_pending_notifications` đọc bảng này mỗi 30 giây.

| Cột | Kiểu | Ghi chú |
|-----|------|---------|
| `id` | INTEGER PK | Auto-increment |
| `user_id` | INTEGER FK → users | Người nhận |
| `channel` | TEXT NOT NULL | `telegram` \| `web` \| … |
| `payload` | TEXT NOT NULL | JSON: `{kind, text, extra}` — service quyết định shape |
| `status` | TEXT | `pending` \| `delivered` \| `failed` — default `pending` |
| `attempts` | INTEGER | Mặc định 0; ≥ 5 → `failed` |
| `last_error` | TEXT | Error message rút gọn (max 500 chars) |
| `next_retry_at` | DATETIME | NULL = ready ngay; set khi backoff |
| `created_at` / `delivered_at` | DATETIME | Timestamps |

Partial index: `(status, next_retry_at) WHERE status = 'pending'` — job retry chỉ scan rows đang pending.

#### `web_sessions` *(FR-5)*
Session web server-side DB-revocable. Mỗi login tạo 1 row; logout set `revoked_at`. Cookie chứa opaque token 32 byte hex (256-bit entropy) — không dùng JWT để hỗ trợ force-logout ngay lập tức.

| Cột | Kiểu | Ghi chú |
|-----|------|---------|
| `id` | INTEGER PK | Auto-increment |
| `user_id` | INTEGER FK → users | |
| `token` | TEXT UNIQUE NOT NULL | 32-byte random hex |
| `created_at` | DATETIME | Default `CURRENT_TIMESTAMP` |
| `expires_at` | DATETIME NOT NULL | `created_at + WEB_SESSION_TTL_DAYS` (default 7 ngày) |
| `revoked_at` | DATETIME | NULL = active; set khi logout hoặc đổi mật khẩu |

Index: `(token)`, `(user_id)`.

#### `web_conversations` *(FR-5.5)*
Mỗi phiên chat web là một conversation. Tạo lazy khi user gửi message đầu tiên — mở "New chat" rồi rời đi không tạo rác DB.

| Cột | Kiểu | Ghi chú |
|-----|------|---------|
| `id` | INTEGER PK | Auto-increment |
| `user_id` | INTEGER FK → users | Chủ conversation |
| `title` | TEXT | NULL cho tới khi LLM gen xong; FE hiển thị "New chat" |
| `created_at` | DATETIME | Default `CURRENT_TIMESTAMP` |
| `updated_at` | DATETIME | Bump mỗi khi có message mới |

Index: `(user_id, updated_at DESC)`.

#### `web_messages` *(FR-5.5)*
Mỗi turn chat (user hoặc bot) là một row. Lưu toàn bộ, không giới hạn — retention vĩnh viễn theo Decision #74.

| Cột | Kiểu | Ghi chú |
|-----|------|---------|
| `id` | INTEGER PK | Auto-increment |
| `conversation_id` | INTEGER FK → web_conversations | |
| `role` | TEXT NOT NULL | `user` \| `bot` |
| `text` | TEXT NOT NULL | Nội dung tin nhắn |
| `created_at` | DATETIME | Default `CURRENT_TIMESTAMP` |

Index: `(conversation_id, created_at)`, `(conversation_id, text)` — index text cho LIKE search.

#### `tasks` *(FR-7)*
Task CRUD. Category `study` dùng cho lịch học định kỳ của trẻ. Soft-delete qua `deleted_at`.

| Cột | Kiểu | Ghi chú |
|-----|------|---------|
| `id` | INTEGER PK | Auto-increment |
| `user_id` | INTEGER FK → users | |
| `title` | TEXT NOT NULL | Tên task ngắn gọn |
| `description` | TEXT | Chi tiết tùy chọn |
| `deadline` | TEXT NOT NULL | ISO datetime +07:00 |
| `category` | TEXT | `task` \| `study` \| `reminder` — default `task` |
| `scope` | TEXT | `private` (v1 chỉ private) |
| `recurring_rule` | TEXT | NULL = one-shot; vd `weekly:MON,WED@07:00` hoặc `daily@21:00` |
| `reminder_offsets` | TEXT | CSV giây: default `7200,3600,1800,900` (2h/1h/30m/15m) |
| `status` | TEXT | `pending` \| `completed` \| `cancelled` |
| `completed_at` | TEXT | ISO datetime; NULL nếu chưa done |
| `snooze_count` | INTEGER | Số lần đã hoãn |
| `source` | TEXT | `telegram` \| `web` |
| `created_at` / `updated_at` / `deleted_at` | TEXT | ISO timestamps |

Index: `(user_id, status)`, `(deadline)` WHERE pending, `(recurring_rule)` WHERE not null.

#### `task_reminders` *(FR-7)*
Mỗi mốc nhắc của một task là một row. Khi task recurring fire xong → engine tính next occurrence + insert rows mới (lazy expansion).

| Cột | Kiểu | Ghi chú |
|-----|------|---------|
| `id` | INTEGER PK | Auto-increment |
| `task_id` | INTEGER FK → tasks | |
| `fire_at` | TEXT NOT NULL | ISO datetime +07:00 — thời điểm cần fire |
| `offset_seconds` | INTEGER | Khoảng cách với deadline (ví dụ 7200 = 2h trước) |
| `kind` | TEXT | `scheduled` \| `snoozed` |
| `status` | TEXT | `pending` \| `fired` \| `missed` \| `cancelled` |
| `fired_at` | TEXT | ISO datetime; NULL nếu chưa fire |
| `created_at` | TEXT | ISO timestamp |

Index: `(fire_at, status)` WHERE pending — job `scan_reminders` chỉ scan rows đang pending.

#### `anniversaries` *(FR-8)*
Sự kiện kỷ niệm hàng năm: giỗ, kỷ niệm cưới, dịp khác. Lưu ngày âm/dương nguyên gốc; ngày dương được recompute mỗi năm tại runtime (Decision #47).

| Cột | Kiểu | Ghi chú |
|-----|------|---------|
| `id` | INTEGER PK | Auto-increment |
| `user_id` | INTEGER FK → users | |
| `name` | TEXT NOT NULL | Tên sự kiện, vd "Giỗ ông nội" |
| `date_type` | TEXT NOT NULL | `lunar` \| `solar` |
| `month` | INTEGER NOT NULL | 1–12 |
| `day` | INTEGER NOT NULL | 1–30 (âm) hoặc 1–31 (dương) |
| `year` | INTEGER | Năm gốc của sự kiện (tùy chọn) |
| `family_member_id` | INTEGER FK → family_members | Nullable — link tới hồ sơ người thân (FR-11) |
| `category` | TEXT NOT NULL | `gio` \| `cuoi` \| `khac` — default `khac` |
| `is_leap_month` | INTEGER NOT NULL | 1 = tháng nhuận âm lịch — default 0 |
| `reminder_offsets` | TEXT NOT NULL | CSV số ngày trước: default `30,15,7,3,1,0` |
| `enabled` | INTEGER NOT NULL | 1 = đang bật; 0 = tạm dừng |
| `note` | TEXT | Ghi chú tùy chọn |
| `created_at` / `updated_at` / `deleted_at` | TEXT | ISO timestamps |

Index: `(user_id)` WHERE deleted_at IS NULL, `(enabled)` WHERE enabled=1 AND deleted_at IS NULL.

#### `categories` *(FR-9)*
Danh mục phân loại bút toán chi/thu. Có thể là riêng (`user_id` = user) hoặc chung cả nhà (`user_id IS NULL` — chỉ admin/manager tạo).

| Cột | Kiểu | Ghi chú |
|-----|------|---------|
| `id` | INTEGER PK | Auto-increment |
| `user_id` | INTEGER FK → users | NULL = danh mục chung (family-shared) |
| `name` | TEXT NOT NULL | Tên danh mục, vd "Ăn uống" |
| `kind` | TEXT NOT NULL | `expense` \| `income` |
| `created_at` / `updated_at` / `deleted_at` | TEXT | ISO timestamps; `deleted_at` dùng soft-delete |

#### `ledger_entries` *(FR-9)*
Bút toán thu/chi. Amount lưu VND nguyên (integer) — không dùng FLOAT (Decision #87). Soft-delete qua `voided_at`; auto-purge sau 30 ngày.

| Cột | Kiểu | Ghi chú |
|-----|------|---------|
| `id` | INTEGER PK | Auto-increment |
| `user_id` | INTEGER FK → users | |
| `kind` | TEXT NOT NULL | `income` \| `expense` |
| `amount` | INTEGER NOT NULL | VND — luôn dương |
| `category_id` | INTEGER FK → categories | Nullable — không bắt buộc gắn danh mục |
| `note` | TEXT | Mô tả raw từ user |
| `occurred_at` | TEXT | ISO datetime — thời điểm giao dịch |
| `source` | TEXT | `telegram` \| `web` |
| `created_at` / `updated_at` | TEXT | ISO timestamps |
| `voided_at` | TEXT | NULL = đang active; set khi void (soft-delete) |

Index: `(user_id, occurred_at DESC)`, `(user_id, category_id, occurred_at)`.

#### `monthly_budgets` *(FR-9)*
Hạn mức chi tiêu và mục tiêu tiết kiệm theo tháng, mỗi user một row mỗi tháng (upsert).

| Cột | Kiểu | Ghi chú |
|-----|------|---------|
| `id` | INTEGER PK | Auto-increment |
| `user_id` | INTEGER FK → users | |
| `month` | TEXT NOT NULL | `YYYY-MM` |
| `expense_budget` | INTEGER | Hạn mức chi tháng (VND); NULL = chưa đặt |
| `savings_target` | INTEGER | Mục tiêu tiết kiệm (VND); NULL = chưa đặt |
| `alerts_sent` | TEXT | JSON string — track ngưỡng 80%/100% đã gửi để không spam |
| `created_at` / `updated_at` | TEXT | ISO timestamps |
| UNIQUE | `(user_id, month)` | Một row mỗi user mỗi tháng |

#### `family_members` *(FR-11)*
Hồ sơ người thân trong gia phả. Lưu cả người còn sống lẫn người đã mất. Soft-delete qua `deleted_at`.

| Cột | Kiểu | Ghi chú |
|-----|------|---------|
| `id` | INTEGER PK | Auto-increment |
| `full_name` | TEXT NOT NULL | Họ và tên đầy đủ |
| `alias_name` | TEXT | Tên thường gọi / tên gọi trong gia đình |
| `gender` | TEXT | `nam` \| `nu` — nullable |
| `generation` | INTEGER | Đời thứ mấy trong gia tộc — nullable |
| `branch` | TEXT | Chi/nhánh họ — nullable |
| `bio` | TEXT | Ghi chú tiểu sử — nullable |
| `birth_date_type` | TEXT | `lunar` \| `solar` \| `year_only` \| `approx` — nullable |
| `birth_year` / `birth_month` / `birth_day` | INTEGER | Ngày sinh (nullable từng phần) |
| `birth_approx` | INTEGER | 1 = năm sinh ước chừng — default 0 |
| `death_date_type` | TEXT | `lunar` \| `solar` \| `year_only` \| `approx` — nullable |
| `death_year` / `death_month` / `death_day` | INTEGER | Ngày mất (nullable từng phần) |
| `death_approx` | INTEGER | 1 = năm mất ước chừng — default 0 |
| `created_by` | INTEGER FK → users | |
| `created_at` / `updated_at` / `deleted_at` | TEXT | ISO timestamps |

Index: `(full_name)`, `(generation)`.

#### `burial_records` *(FR-11)*
Bản ghi mộ phần của một người thân. Một người có thể có nhiều bản ghi (cải táng/di dời); `is_current=1` là bản ghi hiện tại.

| Cột | Kiểu | Ghi chú |
|-----|------|---------|
| `id` | INTEGER PK | Auto-increment |
| `member_id` | INTEGER FK → family_members | |
| `cemetery_name` | TEXT | Tên nghĩa trang — nullable |
| `address` | TEXT | Địa chỉ đầy đủ — nullable |
| `plot_info` | TEXT | Vị trí lô/hàng trong nghĩa trang — nullable |
| `lat` | REAL | Vĩ độ GPS — nullable |
| `lng` | REAL | Kinh độ GPS — nullable |
| `note` | TEXT | Ghi chú thêm — nullable |
| `is_current` | INTEGER | 1 = bản ghi hiện tại; 0 = lịch sử (cải táng cũ) — default 1 |
| `created_by` | INTEGER FK → users | |
| `created_at` / `updated_at` | TEXT | ISO timestamps |

Index: `(member_id, is_current)`.

#### `family_relationships` *(FR-11)*
Quan hệ gia phả giữa các thành viên. Dùng để xây cây gia phả và phát hiện vòng lặp. Mỗi cặp `(from_id, rel_type)` là duy nhất (ràng buộc tối đa 1 cha, 1 mẹ mỗi người).

| Cột | Kiểu | Ghi chú |
|-----|------|---------|
| `id` | INTEGER PK | Auto-increment |
| `from_member_id` | INTEGER FK → family_members | Người mà quan hệ được đặt từ phía họ |
| `to_member_id` | INTEGER FK → family_members | Người mà quan hệ hướng tới |
| `rel_type` | TEXT NOT NULL | `cha` \| `me` \| `vo` \| `chong` \| `con_nuoi` |
| `created_by` | INTEGER FK → users | |
| `created_at` | TEXT | ISO timestamp |
| UNIQUE | `(from_member_id, rel_type)` | Đảm bảo 1 cha và 1 mẹ tối đa; `vo`/`chong`/`con_nuoi` cũng unique per from |

#### `anniversary_reminders` *(FR-8)*
Mỗi mốc nhắc của một kỷ niệm trong một năm cụ thể là một row. UNIQUE constraint đảm bảo compute job idempotent.

| Cột | Kiểu | Ghi chú |
|-----|------|---------|
| `id` | INTEGER PK | Auto-increment |
| `anniversary_id` | INTEGER FK → anniversaries | |
| `year` | INTEGER NOT NULL | Năm solar của lần nhắc này |
| `fire_at` | TEXT NOT NULL | ISO datetime, 08:00 VN |
| `offset_days` | INTEGER NOT NULL | Số ngày trước ngày kỷ niệm (0 = đúng ngày) |
| `status` | TEXT NOT NULL | `pending` \| `fired` \| `missed` \| `cancelled` — default `pending` |
| `fired_at` | TEXT | ISO datetime; NULL nếu chưa fire |
| `created_at` | TEXT NOT NULL | ISO timestamp |
| UNIQUE | `(anniversary_id, year, offset_days)` | Idempotent — compute job có thể chạy lại nhiều lần |

---

## 6. Mô Hình Phân Quyền

### Roles

| Role | Đối tượng | Khả năng |
|------|-----------|----------|
| `admin` | Cha/mẹ chính | Toàn quyền; đọc data private của con dưới 18; truy cập recycle bin |
| `manager` | Ông/bà, người thân lớn tuổi | Duyệt thay đổi ngày sinh; giám sát; KHÔNG đọc data private |
| `member` | Các con, thành viên thường | Đọc/ghi data của mình; nội dung scope group/everyone |
| `readonly` | Khách | Chỉ đọc nội dung scope everyone |

### Quan hệ cha-con
Cấu hình qua bảng `parent_links`. Hỗ trợ:
- Mirror reminder real-time sang cha mẹ (luôn bật, không cấu hình được — đây là core value)
- Digest tổng kết hoạt động (tần suất tùy chỉnh: daily / weekly / monthly / off)
- Tự động tắt giám sát khi con tròn 18 tuổi (enforce tại runtime, không mutate DB)
- Adult opt-in: con ≥ 18 tuổi có thể tự nguyện bật lại chia sẻ (`chia sẻ với cha mẹ: bật`)

### Mô hình scope *(FR-3)*

Mỗi note và wiki page có cột `scope` trong lớp ACL SQLite (`notes`, `wiki_pages`). Drive giữ nội dung; row SQLite quyết định ai được đọc.

| Scope | Ai thấy |
|-------|---------|
| `private` | Chỉ chủ sở hữu |
| `everyone` | Tất cả user còn active |

**Default khi tạo:**
- `ghi nhớ <nội dung>` / `ghi nhớ vào <file>` → `private`
- `nhật ký <nội dung>` → `private`
- `wiki <nội dung>` → `everyone`

**Đổi scope:** `chia sẻ <file>` / `bỏ chia sẻ <file>` (chỉ chủ sở hữu). Người khác sẽ nhận *"Bạn không phải chủ file này"*.

**Điểm enforce ACL:** mọi retrieval path đều filter qua `acl.can_read` / `acl.filter_visible` — `smart_search`, `get_recent_notes`, `get_current_week_notes`, wiki `retrieve_pages`, và các lệnh trực tiếp `xem` / `xem wiki` / `liệt kê`.

**Admin và data private (FR-4):** admin **đọc được** note/wiki `private` của user là con dưới 18 tuổi (stealth-read). Điều kiện: `reader.role == 'admin'` AND owner có quan hệ `parent_links` (là con của ai đó) AND `age(owner) < 18`. Mọi lần đọc đều ghi audit row `stealth_read_note` / `stealth_read_wiki`; owner KHÔNG nhận thông báo. Khi con tròn 18, stealth-read tự động tắt tại runtime (không mutate DB).

### Recycle Bin *(FR-4)*

Soft-delete đã có từ trước qua cột `deleted_at` trên `notes`, `wiki_pages`, `users`. FR-4 bổ sung lệnh admin để xem, khôi phục, và xóa hẳn.

| Lệnh | Hành vi |
|------|---------|
| `xem thung rac` | Liệt kê tất cả items có `deleted_at IS NOT NULL` (notes, wiki pages, users), sắp xếp theo `deleted_at` giảm dần. Ghi audit `recycle_view`. |
| `khoi phuc: <kind> <id>` | Clear `deleted_at`. Ví dụ: `khoi phuc: note 12`. Ghi audit `recycle_restore`. |
| `xoa han: <kind> <id>` | Hard delete ngay, bỏ qua retention 180 ngày. Với note/wiki: xóa cả file trên Drive (best-effort). Ghi audit `recycle_purge`. |

**Scheduled jobs (chạy 3h sáng UTC+7 hàng ngày):**
- `purge_recycle_bin_180d`: xóa vĩnh viễn mọi item có `deleted_at < now − 180 ngày`.
- `purge_children_turning_18`: khi user vừa tròn 18 hôm trước, purge toàn bộ soft-deleted notes/wiki thuộc user đó. Live data không bị đụng.

### Audit Log Taxonomy *(FR-4)*

| `action` | `target_type` | Khi nào |
|---|---|---|
| `stealth_read_note` | `note` | Admin đọc private note của child <18 |
| `stealth_read_wiki` | `wiki_page` | Admin đọc private wiki của child <18 |
| `recycle_view` | — | Admin chạy `xem thung rac` |
| `recycle_restore` | `note` / `wiki_page` / `user` | Admin khôi phục item |
| `recycle_purge` | `note` / `wiki_page` / `user` | Hard delete (manual hoặc auto 180d) |
| `auto_purge_18` | `user` | Daily job phát hiện user vừa tròn 18 |
| `sudo_elevate` / `sudo_drop` / `sudo_fail` / `sudo_locked` | — | Sudo events (migrate từ stdout FR-3.5) |
| `password_set` | `user` | Đặt/đổi mật khẩu admin |
| `role_change` | `user` | Admin đổi role |
| `scope_change` | `note` / `wiki_page` | `chia se` / `bo chia se` |
| `notification_enqueued` | `notification` | Thông báo được đưa vào queue |
| `notification_delivered` | `notification` | Gửi thành công |
| `notification_retry` | `notification` | Lần gửi lại trung gian (attempts < 5) |
| `notification_failed` | `notification` | Đạt max 5 attempts — không retry nữa |
| `web_login` | `user` | Đăng nhập web thành công *(FR-5)* |
| `web_logout` | `user` | Đăng xuất web *(FR-5)* |
| `web_login_failed` | `user` | Đăng nhập web thất bại — sai mật khẩu *(FR-5)* |
| `web_password_set` | `user` | Admin đặt mật khẩu web cho user *(FR-5)* |
| `web_conversation_created` | `web_conversation` | Lazy create khi user gửi message đầu *(FR-5.5)* |
| `web_conversation_renamed` | `web_conversation` | User đổi tên conversation *(FR-5.5)* |
| `stealth_read_web_conversation` | `web_conversation` | Admin xem hội thoại web của user under-18 *(FR-5.5)* |
| `task_created` | `task` | Tạo task mới *(FR-7)* |
| `task_updated` | `task` | Sửa task (tiêu đề, deadline, recurring) *(FR-7)* |
| `task_completed` | `task` | User đánh dấu hoàn thành *(FR-7)* |
| `task_deleted` | `task` | Soft-delete task *(FR-7)* |
| `task_snoozed` | `task` | User hoãn reminder *(FR-7)* |
| `reminder_fired` | `task` | Reminder gửi thành công *(FR-7)* |
| `reminder_missed` | `task` | Reminder quá hạn grace 1h — bỏ qua *(FR-7)* |
| `daily_summary_sent` | `user` | Daily summary gửi cuối ngày *(FR-7)* |
| `parent_digest_sent` | `user` | Parent digest gửi theo tần suất cấu hình *(FR-7)* |
| `anniversary_created` | `anniversary` | Tạo kỷ niệm mới (Telegram hoặc web) *(FR-8)* |
| `anniversary_updated` | `anniversary` | Sửa kỷ niệm *(FR-8)* |
| `anniversary_deleted` | `anniversary` | Soft-delete kỷ niệm *(FR-8)* |
| `anniversary_reminder_fired` | `anniversary` | Nhắc kỷ niệm gửi thành công *(FR-8)* |
| `anniversary_reminder_missed` | `anniversary` | Nhắc kỷ niệm quá hạn grace 12h — bỏ qua *(FR-8)* |
| `ledger_created` | `ledger_entry` | Ghi bút toán thu/chi mới *(FR-9)* |
| `ledger_updated` | `ledger_entry` | Sửa bút toán *(FR-9)* |
| `ledger_voided` | `ledger_entry` | Hủy bút toán (soft-delete) *(FR-9)* |
| `category_created` | `category` | Tạo danh mục mới *(FR-9)* |
| `category_updated` | `category` | Đổi tên danh mục *(FR-9)* |
| `category_deleted` | `category` | Xóa danh mục (soft-delete) *(FR-9)* |
| `family_member_created` | `family_member` | Thêm hồ sơ người thân mới *(FR-11)* |
| `family_member_updated` | `family_member` | Sửa hồ sơ người thân *(FR-11)* |
| `family_member_deleted` | `family_member` | Soft-delete hồ sơ người thân *(FR-11)* |
| `family_relationship_created` | `family_relationship` | Thêm quan hệ gia phả (cha/mẹ/vợ/chồng/con nuôi) *(FR-11)* |
| `family_relationship_deleted` | `family_relationship` | Xóa quan hệ gia phả *(FR-11)* |
| `folder_registered` | `drive` | Drive folder được trust sau khi bot tự tạo/xác minh *(Security hardening)* |
| `scope_validated` | `drive` | OAuth token scope đã xác minh là `drive.file` *(Security hardening)* |
| `file_created` | `drive` | File tạo thành công trên Drive *(Security hardening)* |
| `file_updated` | `drive` | File cập nhật trên Drive *(Security hardening)* |
| `file_deleted` | `drive` | File xóa trên Drive *(Security hardening)* |

### Notification Framework *(FR-4)*

Plumbing tối thiểu để bất kỳ module nào enqueue thông báo gửi qua `ChannelAdapter`, với retry/backoff persistent qua SQLite.

- **`enqueue(user_id, channel, payload)`** — chỉ ghi DB + audit `notification_enqueued`. Không gửi ngay, không blocking caller.
- **`flush_pending()`** — scheduler gọi mỗi 30 giây; đọc queue, gửi qua adapter:
  - Thành công → `status='delivered'`, audit `notification_delivered`.
  - Thất bại nhưng `attempts < 5` → tăng `attempts`, set `next_retry_at = now + 2^attempts phút`, audit `notification_retry`.
  - Thất bại và `attempts >= 5` → `status='failed'`, audit `notification_failed`.
- Payload schema: `{"kind": "text", "text": "...", "extra": {...}}`. FR-7 sẽ định nghĩa thêm kinds (`reminder`, `digest`, ...).
- Observability: `xem audit` cho ra full trace enqueue → retry × N → delivered/failed theo thứ tự thời gian.

### Privilege Elevation — sudo *(FR-3.5)*

Production KHÔNG dùng admin làm tài khoản mặc định. Tài khoản chính chạy role `manager`; khi cần thao tác quản trị thì **nâng quyền tạm thời** lên `admin`.

| Khái niệm | Mô tả |
|-----------|-------|
| **Natively-admin** | User có `role='admin'` trong DB, bind trực tiếp với chat_id. Không có phiên elevation. |
| **Elevated-admin** | User role thật `manager`, đang có phiên elevation còn hạn → `role` bị override thành `admin` tại resolution. |

**Cơ chế:**
- `main.py` sau khi `find_by_channel` kiểm tra `elevation_store.get_active_session()`. Có phiên hợp lệ → `dataclasses.replace(user, role="admin")`. Identity (`id`, `name`) **không đổi** — audit luôn ghi đúng người thật (Decision #57).
- TTL 15 phút (`SUDO_TTL_MINUTES`), hết hạn lazy — không cần cron.
- Gating nhiều tầng: lệnh `sudo` chỉ role `manager` dùng; verify Argon2id với hash của (các) user role `admin`; rate-limit 5 fail → khóa 15 phút.
- Bot tự xóa message chứa mật khẩu (`delete_message` trên `ChannelAdapter`, implement bằng Telegram `deleteMessage` API).
- Audit table: `sudo_elevate`, `sudo_drop`, `sudo_fail`, `sudo_locked`, `password_set` ghi vào `audit_log` (migrate từ stdout FR-3.5 sang FR-4).
- `dat mat khau` chỉ chạy được từ tài khoản **natively-admin** — vừa là đặt lần đầu vừa là cơ chế recovery (không có flow "quên mật khẩu" riêng — Decision #59).

### Phân quyền lệnh theo role

| Lệnh | admin | manager | member | readonly |
|------|-------|---------|--------|----------|
| Thêm / xóa user | ✅ | ❌ | ❌ | ❌ |
| Đặt quota | ✅ | ❌ | ❌ | ❌ |
| Duyệt ngày sinh | ✅ | ✅ | ❌ | ❌ |
| Duyệt username | ✅ | ❌ | ❌ | ❌ |
| Đặt parent link | ✅ | ❌ | ❌ | ❌ |
| Ghi chú / nhật ký / wiki | ✅ | ✅ | ✅ | chỉ đọc |
| Xem thung rác / khôi phục / xóa hẳn | ✅ | ❌ | ❌ | ❌ |
| Xem audit log | ✅ | ❌ | ❌ | ❌ |
| Xem gia phả / tra cứu mộ phần | ✅ | ✅ | ✅ | ✅ |
| Gia phả thêm/sửa/xóa thành viên & quan hệ | ✅ | ✅ | ❌ | ❌ |

---

## 7. Danh Sách Lệnh

Lệnh được match qua prefix matcher không phân biệt dấu tiếng Việt — cả dạng có dấu (`ghi nhớ`) lẫn không dấu (`ghi nho`) đều hoạt động.

### Slash commands
| Lệnh | Mô tả |
|------|-------|
| `/start` | Tổng quan các nhóm lệnh |
| `/help [nhóm]` | Chi tiết một nhóm (ví dụ `/help tri nho`, `/help wiki`) |
| `/cost` | Xem chi phí LLM hiện tại |
| `/test` | Test kết nối |
| `/security` | Xem trạng thái bảo mật Drive |

### Quản lý user (chỉ admin)
| Lệnh | Mô tả |
|------|-------|
| `thêm user: <tên>, <role>` | Tạo mã mời cho user mới |
| `xem danh sách user` | Liệt kê tất cả user |
| `xóa user: <tên>` | Soft-delete user |
| `đổi role: <tên/id> <role mới>` | Đổi role của user đã tồn tại (safety guard: không cho admin tự hạ role chính mình) |
| `đặt quota: <tên>, <tokens>` | Đặt hạn mức token tháng |
| `reset quota: <tên>` | Reset usage tháng hiện tại |
| `đặt cha: <cha mẹ>, <con>` | Tạo quan hệ cha-con |

### Hồ sơ cá nhân
| Lệnh | Mô tả |
|------|-------|
| `đặt username: <handle>` | Đặt / yêu cầu đổi username |
| `đặt birthdate: <ngày>` | Yêu cầu thay đổi ngày sinh (cần duyệt) |
| `duyệt username` | Duyệt yêu cầu đổi username (admin) |
| `duyệt birthdate` | Duyệt yêu cầu thay đổi ngày sinh (admin/manager) |
| `xem cha: <tên>` | Xem quan hệ cha-con của user |
| `xem quota` | Xem usage quota của mình |
| `tôi là ai` | Xem danh tính của mình (tên, username, role, id) |

### Ghi chú & nhật ký
| Lệnh | Mô tả |
|------|-------|
| `ghi nhớ <nội dung>` | Lưu ghi chú |
| `ghi nhớ vào <tiêu đề>: <nội dung>` | Lưu vào file cụ thể |
| `nhật ký <nội dung>` | Ghi thêm vào nhật ký hôm nay |
| `xem nhật ký` | Đọc các entry nhật ký |
| `liệt kê` | Liệt kê ghi chú gần đây |
| `tìm <từ khóa>` | Tìm kiếm mờ trong ghi chú |
| `xem <tiêu đề>` | Đọc ghi chú cụ thể |

### Wiki
| Lệnh | Mô tả |
|------|-------|
| `wiki <nội dung>` | Nạp nội dung vào wiki |
| `hỏi wiki <câu hỏi>` | Hỏi đáp wiki (hỗ trợ bởi LLM) |
| `xem wiki` | Liệt kê các trang wiki |
| `xem wiki <trang>` | Đọc trang wiki cụ thể |

### Scope & chia sẻ *(FR-3)*
| Lệnh | Mô tả |
|------|-------|
| `chia sẻ <file>` | Đặt scope thành `everyone` (chỉ chủ sở hữu) |
| `bỏ chia sẻ <file>` | Đặt scope về `private` (chỉ chủ sở hữu) |
| `xem scope <file>` | Hiện scope, chủ sở hữu, loại, timestamps của file |

### L1 Memory *(FR-3)*
| Lệnh | Mô tả |
|------|-------|
| `xem trí nhớ` | Đọc snapshot `memory` của mình (facts cuộn) |
| `xem hồ sơ` | Đọc snapshot `user` của mình (profile ổn định) |
| `cập nhật trí nhớ` | Trigger LLM curate dựa trên note gần đây |

### Privilege Elevation *(FR-3.5)*
| Lệnh | Mô tả |
|------|-------|
| `sudo: <mật khẩu>` | Nâng role `manager` lên `admin` tạm thời 15 phút (bot tự xóa message chứa mật khẩu) |
| `thoát sudo` | Hạ quyền ngay lập tức |
| `đặt mật khẩu: <mật khẩu>` | Đặt/đổi mật khẩu admin — chỉ tài khoản natively-admin (cũng là cơ chế recovery) |

### Audit & Quản trị *(FR-4)*
| Lệnh | Mô tả | Ai dùng |
|------|-------|---------|
| `xem audit` | Liệt kê 50 audit events gần nhất | admin |
| `xem audit <action>` | Lọc theo loại sự kiện (vd `xem audit sudo_elevate`) | admin |
| `xem thung rac` | Liệt kê items đang trong recycle bin (soft-deleted) | admin |
| `khoi phuc: <kind> <id>` | Khôi phục item (vd `khoi phuc: note 12`) | admin |
| `xoa han: <kind> <id>` | Hard delete ngay, bỏ qua retention 180 ngày | admin |

### Web UI — quản trị *(FR-5)*
| Lệnh Telegram | Mô tả | Ai dùng |
|---------------|-------|---------|
| `dat web pass: <tên_user>, <mật_khẩu>` | Đặt mật khẩu web cho user + set `must_change_password=1` → user bị force-reset lần đăng nhập đầu | admin |

**Luồng web (qua browser):**
- `/login` — đăng nhập; cookie `web_session` HttpOnly + SameSite=Lax + Secure
- `/setup-password` — force-reset mật khẩu nếu `must_change_password=1`
- `/logout` — revoke session server-side ngay lập tức
- Brute-force: 5 fail → khóa 15 phút (tái dùng bảng `sudo_attempts` với `channel="web"`)

**HTTP security middleware (security hardening):**
- **CSRF:** Cookie `csrf_token` non-HttpOnly + SameSite=Lax được set trên mọi GET. POST/PUT/PATCH/DELETE phải echo token qua form field `csrf_token` (HTML form) hoặc header `X-CSRF-Token` (htmx/fetch). `/webhook` được exempt (Telegram không có browser cookie). JS trong `base.html` tự inject token vào form submit và htmx request.
- **Rate limiting:** `/login` giới hạn 10 request/60s per IP (tầng transport, độc lập với per-user lockout ở `elevation_store`); default 120/60s cho tất cả route mutating khác.
- **Security headers:** Mọi response đều có `X-Frame-Options: DENY` (chống clickjacking), `X-Content-Type-Options: nosniff`, `Referrer-Policy: strict-origin-when-cross-origin`, `Content-Security-Policy` (cho phép unpkg.com + `unsafe-inline/eval` do Alpine 3 yêu cầu), `Strict-Transport-Security` (chỉ staging/production).
- **SRI:** htmx và Alpine.js được load từ CDN với `integrity="sha384-..."` + `crossorigin="anonymous"` để phát hiện nếu CDN bị tamper.

### Web Chat History *(FR-5.5)*
**Các route:**

| Method | Path | Mô tả |
|--------|------|-------|
| GET | `/chat` | New chat lazy; render sidebar + empty messages |
| GET | `/chat/<id>` | Mở conversation cụ thể |
| POST | `/chat/send` | Gửi message khi chưa có conversation (lazy create) |
| POST | `/chat/<id>/send` | Gửi message vào conversation hiện có |
| GET | `/chat/stream?conversation_id=<id>` | SSE stream per conversation |
| GET | `/api/conversations` | JSON list conversations của user |
| GET | `/api/conversations/<id>/messages` | JSON messages của conversation |
| PATCH | `/api/conversations/<id>` | Đổi tên conversation |
| GET | `/api/conversations/search?q=...` | LIKE search trong messages |
| GET | `/admin/users/<id>/conversations` | Admin xem conversations của user under-18 |
| GET | `/admin/conversations/<id>` | Admin xem messages (emit audit `stealth_read_web_conversation`) |

**Tính năng sidebar:**
- Collapsible (mặc định collapsed trên mobile)
- Rename inline (double-click tên → input → Enter/blur save)
- Search box với debounce 300ms
- New chat button — lazy create conversation khi user gửi message đầu

### Task & Lịch học *(FR-7)*
| Lệnh | Mô tả |
|------|-------|
| `tao task: <mô tả>` | Tạo task mới — LLM parse deadline từ mô tả tự nhiên |
| `task: <mô tả>` | Tương đương `tao task:` |
| `xong task: <id>` | Đánh dấu task hoàn thành |
| `huy task: <id>` | Hủy task |
| `task <id>` | Xem chi tiết task |
| `danh sach task` | Liệt kê task đang chờ |
| `lich hoc: <mô tả>` | Tạo lịch học định kỳ (category=study, recurring) |
| `danh sach lich hoc` | Xem tất cả lịch học đang hoạt động |
| `sua lich hoc: <id> <mô tả mới>` | Cập nhật lịch học (LLM re-parse + reschedule) |
| `huy lich hoc: <id>` | Hủy một lịch học |
| `hoan task: <id> <phút>` | Hoãn task thêm N phút |
| `tom tat hom nay` | Tổng kết task hôm nay |
| `cau hinh tong ket: <HH:MM\|tat>` | Đổi giờ gửi tổng kết hàng ngày (hoặc tắt) |
| `cau hinh gio mac dinh: <HH:MM>` | Đổi giờ mặc định cho task không có giờ cụ thể |

**Web routes *(FR-7)*:**

| Method | Path | Mô tả |
|--------|------|-------|
| GET | `/tasks` | Danh sách task (filter `?status=pending\|completed\|all`) |
| GET | `/tasks/new` | Form tạo task |
| POST | `/tasks` | Tạo task |
| GET | `/tasks/{id}` | Chi tiết + lịch sử reminder |
| GET | `/tasks/{id}/edit` | Form sửa task |
| POST | `/tasks/{id}` | Cập nhật task |
| POST | `/tasks/{id}/complete` | Đánh dấu hoàn thành |
| POST | `/tasks/{id}/delete` | Soft-delete |

### Kỷ niệm & Nhắc ngày *(FR-8)*
| Lệnh | Mô tả |
|------|-------|
| `them ky niem: <tên>, âm/dương DD/MM[, <loại>]` | Thêm kỷ niệm mới. Loại: gio / cuoi / khac |
| `danh sach ky niem` | Liệt kê tất cả kỷ niệm của bạn |
| `ky niem <id>` | Xem chi tiết 1 kỷ niệm |
| `xoa ky niem: <id>` | Soft-delete + hủy các reminder đang chờ |
| `sua ky niem: <id>, ten=…, ngay=âm/dương DD/MM, loai=…, nhac=<csv>, bat/tat` | Cập nhật kỷ niệm |

**Web routes *(FR-8)*:**

| Method | Path | Mô tả |
|--------|------|-------|
| GET | `/anniversaries` | Danh sách kỷ niệm của user |
| GET | `/anniversaries/new` | Form tạo kỷ niệm |
| POST | `/anniversaries` | Tạo kỷ niệm — redirect về detail |
| GET | `/anniversaries/{id}` | Chi tiết kỷ niệm |
| GET | `/anniversaries/{id}/edit` | Form sửa |
| POST | `/anniversaries/{id}` | Cập nhật |
| POST | `/anniversaries/{id}/delete` | Soft-delete |

### Chi tiêu & Ngân sách *(FR-9)*
| Lệnh | Mô tả |
|------|-------|
| `chi: <số> <mô tả>` | Ghi khoản chi tiêu. Ví dụ: `chi: 50k ăn trưa` |
| `thu: <số> <mô tả>` | Ghi khoản thu nhập. Ví dụ: `thu: 5tr lương` |
| `ghi chep: <id>` | Xem chi tiết một bút toán |
| `danh sach ghi chep` | Liệt kê 20 bút toán gần nhất |
| `sua ghi chep: <id>, so=<số>[, mo ta=<text>]` | Sửa bút toán |
| `huy ghi chep: <id>` | Hủy bút toán (soft-delete, giữ 30 ngày) |
| `xem danh muc` | Xem danh sách danh mục chi/thu |
| `them danh muc: <tên>, chi\|thu[, chung]` | Thêm danh mục (thêm `, chung` cần admin/manager) |
| `xoa danh muc: <id>` | Xóa danh mục (soft-delete) |
| `sua danh muc: <id> <tên mới>` | Đổi tên danh mục |
| `bao cao thang [YYYY-MM]` | Báo cáo thu/chi tháng (mặc định tháng hiện tại) |
| `bao cao nam` | Báo cáo thu/chi từng tháng trong năm hiện tại |
| `xem chi tieu` | Tổng thu/chi 7 ngày qua |
| `dat han muc chi: <số>` | Đặt hạn mức chi tháng này |
| `dat muc tieu tiet kiem: <số>` | Đặt mục tiêu tiết kiệm tháng này |
| `xem han muc` | Xem hạn mức chi và mục tiêu tiết kiệm hiện tại |

**Định dạng số:** `50000`, `50k`, `50.000`, `50tr`, `5m` đều được chấp nhận. Xuất ra dạng `50.000 đ`.

**Web routes *(FR-9)*:**

| Method | Path | Mô tả |
|--------|------|-------|
| GET | `/ledger` | Danh sách bút toán tháng hiện tại + tổng thu/chi/tiết kiệm |
| GET | `/ledger/new` | Form tạo bút toán mới |
| POST | `/ledger` | Lưu bút toán mới |
| GET | `/ledger/categories` | Quản lý danh mục |
| POST | `/ledger/categories` | Tạo danh mục mới |
| POST | `/ledger/categories/{id}/delete` | Xóa danh mục |
| GET | `/ledger/report` | Báo cáo tháng: tổng, theo danh mục, biểu đồ |
| GET | `/ledger/budget` | Xem hạn mức + mục tiêu tiết kiệm |
| POST | `/ledger/budget` | Cập nhật hạn mức / mục tiêu |
| GET | `/ledger/{id}/edit` | Form sửa bút toán |
| POST | `/ledger/{id}` | Cập nhật bút toán |
| POST | `/ledger/{id}/void` | Hủy bút toán |

### Gia phả & Mộ phần *(FR-11)*
| Lệnh | Mô tả | Ai dùng |
|------|-------|---------|
| `them nguoi than: <tên>[, doi <n>][, sinh <ngày>][, mat <ngày>][, gioi tinh nam/nu][, ten goi <alias>][, chi <chi>][, ghi chu <text>]` | Thêm hồ sơ người thân | admin/manager |
| `xem nguoi than <id/tên>` | Xem hồ sơ đầy đủ (kèm mộ phần hiện tại) | mọi user |
| `danh sach nguoi than [doi <n>]` | Liệt kê người thân (tùy chọn lọc theo đời) | mọi user |
| `sua nguoi than: <id>, <field>=<giá trị>[, ...]` | Sửa hồ sơ | admin/manager |
| `xoa nguoi than: <id>` | Soft-delete (phải xóa mộ phần trước) | admin/manager |
| `them mo phan: <id>, <nghĩa trang>[, dia chi <địa chỉ>][, gps <lat>,<lng>][, lo <vị trí>][, ghi chu <text>]` | Thêm bản ghi mộ phần (cũ trở thành lịch sử) | admin/manager |
| `sua mo phan: <id>, <field>=<giá trị>[, ...]` | Sửa mộ phần hiện tại | admin/manager |
| `xoa mo phan: <id>` | Xóa bản ghi mộ phần | admin/manager |
| `tim mo <id/tên>` | Tra cứu nhanh địa điểm mộ + link Google Maps | mọi user |
| `them quan he: <id> la <loại> cua <id>` | Tạo quan hệ gia phả (loại: cha/me/vo/chong/con nuoi) | admin/manager |
| `xoa quan he: <id> <loại> <id>` | Xóa quan hệ gia phả | admin/manager |
| `gia pha [<id/tên>]` | Xem cây gia phả toàn bộ hoặc từ một người cụ thể | mọi user |

**Web routes *(FR-11)*:**

| Method | Path | Mô tả |
|--------|------|-------|
| GET | `/family/members` | Danh sách người thân + tìm kiếm `?q=` |
| GET | `/family/members/new` | Form tạo hồ sơ (admin/manager) |
| POST | `/family/members` | Lưu hồ sơ mới → redirect về detail |
| GET | `/family/members/{id}` | Chi tiết hồ sơ + mộ phần + link Google Maps |
| GET | `/family/members/{id}/edit` | Form sửa hồ sơ (admin/manager) |
| POST | `/family/members/{id}` | Cập nhật hồ sơ → redirect về detail |
| GET | `/family` | Cây gia phả text (dạng `<pre>`) |

### Đăng ký (trước khi xác thực)
| Lệnh | Mô tả |
|------|-------|
| `đăng ký: <mã>` | Đăng ký bằng mã mời |

### Khác
| Lệnh | Mô tả |
|------|-------|
| `tóm tắt tuần này` | Tóm tắt hoạt động tuần |
| Câu hỏi tự do | Xử lý bởi agentic LLM loop |

---

## 8. Mô Hình Bộ Nhớ (Vision)

Lấy cảm hứng từ Hermes Agent của NousResearch. Ba tầng, xây dựng dần qua các FR:

| Tầng | Lưu trữ | Mô tả | Trạng thái |
|------|---------|-------|-----------|
| L1 | SQLite (bảng `user_memory`, kind: `memory` \| `user`) | Snapshot đóng băng; LLM curate theo yêu cầu (`cập nhật trí nhớ`); inject vào context Q&A | FR-3 ✅ |
| L2 | Graph DB (Memgraph/Neo4j embedded) | Quan hệ giữa entities; passive | Tương lai |
| L3 | Vector store (sqlite-vss hoặc Qdrant) | Semantic search qua Voyage AI embeddings | Tương lai |

---

## 9. Persistence & Deployment

```
┌─────────────────────────────┐
│   Render.com (Docker)        │
│                              │
│  ┌────────────────────────┐ │
│  │  docker-entrypoint.sh  │ │
│  │  1. litestream restore  │ │  ← restore SQLite từ R2 mỗi lần khởi động
│  │  2. litestream replicate│ │  ← stream WAL lên R2 liên tục (~1 giây)
│  │     + uvicorn main:app  │ │
│  └────────────────────────┘ │
│                              │
│  /data/bot.db  (ephemeral)   │
└──────────────┬───────────────┘
               │ WAL replication
               ▼
┌──────────────────────────────┐
│  Cloudflare R2               │
│  telegram-bot-db/            │
│    staging/bot.db            │
│    production/bot.db         │
└──────────────────────────────┘
```

Vì Render free tier dùng **ephemeral filesystem**, SQLite sẽ mất dữ liệu sau mỗi lần restart nếu không có Litestream. Litestream stream WAL SQLite lên R2 (~1 giây lag) và restore từ R2 khi khởi động.

---

## 10. Git Workflow

| Branch | Mục đích |
|--------|---------|
| `main` | Production — mỗi commit đã verify trên staging |
| `dev` | Staging integration buffer — không bao giờ dùng làm base feature |
| `feature/*` | Feature branches — luôn branch off từ `main` |

Feature branch merge **tuần tự**: feature → `dev` trước (test staging) → xác nhận không lỗi → feature → `main` (production). Chỉ xóa feature branch sau khi đã vào `main`.

Xem chi tiết đầy đủ tại [`ROADMAP.md`](ROADMAP.md) Section 3.5.
