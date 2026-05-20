# Kiến Trúc Hệ Thống

> Tài liệu này mô tả kiến trúc của Telegram Claude Bot tính đến **FR-3** (SQLite scope + L1 Memory).
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

Hệ thống dùng **Modular Monolith** với kiến trúc hexagonal. Toàn bộ business logic nằm trong `core_handler.py` và chỉ phụ thuộc vào *Protocols* (interfaces), không bao giờ phụ thuộc vào concrete adapter. Các adapter được kết nối (wire) tại `main.py`.

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

**Nguyên tắc quan trọng:** `core_handler.py` không bao giờ import concrete class. Toàn bộ việc kết nối adapter xảy ra tại `main.py`.

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
| `core_handler.py` | Business logic, command dispatch, pending state machine |
| `channel_telegram.py` | `TelegramAdapter` — parse Telegram webhook payload, gửi reply |
| `claude_client.py` | `AnthropicLLM` — wrapper Anthropic SDK |
| `drive_client.py` | `DriveNoteStore` — lưu trữ ghi chú trên Google Drive |
| `wiki_client.py` | `DriveWikiStore` — wiki trên Google Drive, dùng LLM qua DI |
| `user_store.py` | `UserStore` — registry người dùng SQLite, quota, parent links, password |
| `note_index.py` | `SqliteNoteIndex` — lớp ACL/index SQLite ánh xạ Drive file ID → owner + scope |
| `memory_store.py` | `SqliteMemoryStore` — L1 memory (2 slot `memory` và `user` mỗi user) |
| `acl.py` | ACL helpers (`can_read`, `filter_visible`) dùng bởi các retrieval path |
| `auth.py` | Argon2id password hashing (hạ tầng web auth, chưa expose qua lệnh) |
| `permissions.py` | Permission helpers theo role |
| `text_utils.py` | Chuẩn hóa dấu tiếng Việt, multi-prefix command matcher |
| `timeutils.py` | Helpers UTC+7 |
| `cost_monitor.py` | Theo dõi chi phí LLM, cảnh báo ngưỡng |
| `security.py` | Kiểm soát truy cập folder Drive, rate limiting |
| `config.py` | Load biến môi trường |
| `db/connection.py` | SQLite connection factory |
| `db/migrations.py` | Migration runner idempotent dựa trên file |
| `db/migrations/*.sql` | File SQL migration (001–012) |

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
| `password_hash` | TEXT | Hash Argon2id; NULL cho tới khi được set (web auth — chưa expose qua Telegram) |
| `created_at` | DATETIME | Default `CURRENT_TIMESTAMP` |
| `deleted_at` | DATETIME | Mốc soft-delete; NULL = đang active |

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

**Admin và data private:** trong FR-3, admin **không** đọc được note private của người khác (Decision #52). Stealth-read + audit log dời sang FR-4.

### Phân quyền lệnh theo role

| Lệnh | admin | manager | member | readonly |
|------|-------|---------|--------|----------|
| Thêm / xóa user | ✅ | ❌ | ❌ | ❌ |
| Đặt quota | ✅ | ❌ | ❌ | ❌ |
| Duyệt ngày sinh | ✅ | ✅ | ❌ | ❌ |
| Duyệt username | ✅ | ❌ | ❌ | ❌ |
| Đặt parent link | ✅ | ❌ | ❌ | ❌ |
| Ghi chú / nhật ký / wiki | ✅ | ✅ | ✅ | chỉ đọc |

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

Feature branch merge **song song** vào cả `dev` (để test staging) và `main` (production), chỉ xóa sau khi đã vào `main`.

Xem chi tiết đầy đủ tại [`ROADMAP.md`](ROADMAP.md) Section 3.5.
