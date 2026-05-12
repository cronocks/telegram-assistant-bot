# Telegram Bot — Foundation Release Roadmap

> Tài liệu master plan cho project. Đây là **single source of truth** cho mọi quyết định thiết kế và roadmap.

---

## 0. Cách dùng file này

**Khi mở project lần đầu hoặc chuyển máy:**
- Đọc file này trước, sau đó đọc `CLAUDE.md` (workflow rules)
- Nhảy thẳng đến **Section 8 (Current Status & Next Action)** để biết đang ở đâu

**Khi làm việc với Claude:**
> "Đọc `docs/ROADMAP.md` và `CLAUDE.md`, sau đó tiếp tục từ Section 8."

**Khi có quyết định mới phát sinh:**
- Cập nhật section liên quan
- Thêm entry vào **Section 6 (Decision Log)**
- Cập nhật **Section 8 (Current Status)** nếu trạng thái thay đổi
- Commit vào branch hiện tại đang làm

---

## 1. Vision

### Mục tiêu cuối
Personal/family **knowledge management system** đa kênh, tự học hỏi, tiến hóa liên tục.

### Bối cảnh hiện tại
Telegram bot single-user dùng Google Drive làm note/wiki store. Sẽ nâng cấp toàn diện thành hệ thống gia đình ~10 người.

### Định hướng dài hạn
- **Multi-channel:** Telegram (primary), Discord, Web UI, có thể mở rộng WhatsApp/Signal/Messenger
- **3-tier memory** (lấy cảm hứng từ Hermes Agent của NousResearch):
  - **L1 (text):** `MEMORY.md`, `USER.md` — frozen snapshot, agentic curation
  - **L2 (graph):** quan hệ giữa các entity, passive
  - **L3 (vector):** semantic search, passive
- **Local deployment** (tương lai): chuyển toàn bộ stack về máy nhà, không phụ thuộc cloud
- **Local LLM** (tương lai xa): Ollama integration để chạy hoàn toàn offline
- **Self-evolution:** cron định kỳ phản tỉnh trên data, tự refine

---

## 2. Kiến trúc tổng quan

### 2.1 Quyết định nền tảng: Modular Monolith, KHÔNG Microservice

| Vấn đề | Microservice | Modular Monolith (đã chọn) |
|--------|--------------|---------------------------|
| Quy mô user | Hàng nghìn-triệu | ~10 user gia đình |
| Đội dev | Nhiều team | 1 người (bạn) |
| Render free tier | Mỗi service tốn slot RAM + sleep riêng | 1 service duy nhất |
| Transaction (ledger) | Distributed transaction ác mộng | SQLite ACID đơn giản |
| Debug | Cross-service trace | 1 stack trace |
| Cost | $$$ | $0 free tier |

→ **Hexagonal architecture** là modular monolith đúng pattern: modules tách biệt qua Protocols, deploy chung 1 process.

### 2.2 Hexagonal Architecture (Ports & Adapters)

```
                  ┌──────────────────────┐
                  │    core_handler.py   │  ← business logic
                  │   handle_message()   │     channel-agnostic
                  └──────────┬───────────┘
                             │ depends only on Protocols
              ┌──────────────┼──────────────┐
              ▼              ▼              ▼
         LLMClient      NoteStore      WikiStore
         EmbedClient    ChannelAdapter
              ▲              ▲              ▲
              │  implemented by             │
              │                             │
       AnthropicLLM    DriveNoteStore  DriveWikiStore
       TelegramAdapter
       (future: OllamaLLM, LocalFSNoteStore, DiscordAdapter, WebAdapter, ...)
```

**Core không bao giờ import concrete class.** Adapter wiring xảy ra ở `main.py`.

### 2.3 File layout (sau FR-1)

| File | Vai trò |
|------|---------|
| `main.py` | Wiring layer — instantiate adapters, route webhook |
| `interfaces.py` | Protocols + ChannelMessage dataclass |
| `core_handler.py` | Business logic, command dispatch, pending state |
| `channel_telegram.py` | TelegramAdapter |
| `claude_client.py` | AnthropicLLM |
| `drive_client.py` | DriveNoteStore + shared Drive helpers |
| `wiki_client.py` | DriveWikiStore (nhận LLMClient qua DI) |
| `cost_monitor.py` | Quota & cost tracking |
| `config.py` | Env vars |

---

## 3. Stack & Infrastructure

### 3.1 Runtime
- Python 3.11
- FastAPI + Uvicorn
- APScheduler (đã có; sẽ mở rộng cho reminders)
- httpx async

### 3.2 Storage
| Giai đoạn | Storage |
|-----------|---------|
| Hiện tại | Google Drive (notes, wiki) |
| FR-3 trở đi | SQLite (users, parent_links, scope, L1 memory) |
| FR-9 | SQLite (ledger) |
| Cloud production | SQLite + **Litestream** (continuous backup to S3-compatible) |
| Local mode (tương lai) | SQLite file thuần, không Litestream |

### 3.3 LLM & Embeddings
- **LLM:** Anthropic Claude (qua `AnthropicLLM` adapter)
- **Embeddings:** Voyage AI `voyage-3-lite` (dùng cho L3 vector — FR sau)
- **Quota:** $5/tháng hard limit cho cả family (sẽ chia per-user trong FR-2)

### 3.4 Hosting
- **Production:** Render.com, auto-deploy từ branch `main`
- **Staging:** Render.com service riêng, auto-deploy từ branch `dev`
- **Telegram bot test** riêng dùng cho staging
- **UptimeRobot** ping production (staging cứ để sleep tiết kiệm 750h/tháng free pool)

### 3.5 Git workflow
```
feature/xxx ──┐
              ├─→ PR vào dev ──→ test staging ──→ PR dev vào main ──→ production
claude/xxx ───┘
```
- `main`: production-clean, mỗi commit đã verify
- `dev`: integration branch, chấp nhận unstable
- `feature/*` hoặc `claude/*`: feature branches

---

## 4. Family & Permission Model

### 4.1 Roles (4 cấp)

| Role | Đối tượng | Quyền |
|------|-----------|-------|
| `admin` | Bố/mẹ chính | Full control, đọc private under-18 (stealth), recycle bin access |
| `manager` | Ông/bà, người thân lớn tuổi | Phê duyệt birthdate, giám sát; KHÔNG đọc private |
| `member` | Các con, thành viên thường | Đọc/ghi data của mình, scope group/everyone |
| `readonly` | Khách | Chỉ đọc scope everyone |

### 4.2 Parent-child relationship (FR-2)

Quan hệ **many-to-many** qua bảng `parent_links`:

```sql
parent_links
  parent_user_id       FK → users.id
  child_user_id        FK → users.id
  digest_frequency     TEXT     -- 'daily' | 'weekly' | 'monthly' | 'off'
  digest_time          TEXT     -- 'daily'   → '21:00'
                                -- 'weekly'  → 'SUN 20:00'
                                -- 'monthly' → '1 20:00' hoặc 'LAST 20:00'
  adult_optin_enabled  BOOLEAN DEFAULT FALSE  -- chỉ có nghĩa khi child >= 18
  PRIMARY KEY (parent_user_id, child_user_id)
```

Lý do many-to-many: con có thể có 2 cha mẹ; gia đình ly hôn; ông/bà đứng tên cha mẹ; người giúp việc làm guardian.

### 4.3 Notification mirroring (2 tier)

**Tier 1 — Reminder real-time** (task, lịch học, cảnh báo)
- Luôn mirror sang parent
- **Không cấu hình được** (đây là core value)

**Tier 2 — Digest tổng kết hoạt động**
- Cấu hình `digest_frequency`: `daily` / `weekly` / `monthly` / `off`
- Cấu hình `digest_time` theo schema ở 4.2
- Default khi tạo link: `daily` 21:00 (parent có thể giảm dần khi con lớn)

**Lệnh cấu hình (sketch):**
```
cấu hình tổng kết: con An, hàng tuần, chủ nhật 20:00
cấu hình tổng kết: con Bình, hàng tháng, ngày 1 lúc 20:00
cấu hình tổng kết: con Cường, tắt
```

### 4.4 Auto-rules ở tuổi 18

**Enforce ở runtime, KHÔNG mutate DB.** Single source of truth = `users.birthdate`.

Khi `age(child) >= 18`, các thứ sau **tự động tắt**:
- Mirror reminder real-time
- Digest tổng kết
- Stealth-read của admin
- Recycle bin access của admin với data user này → data auto-purge vĩnh viễn

**Notification one-time ở sinh nhật 18:**
- Gửi con: *"Bạn tròn 18 tuổi. Thông báo tự động về bạn cho cha mẹ đã tắt. Dùng `chia sẻ với cha mẹ: bật` nếu muốn bật lại."*
- Gửi parent: *"Con [tên] tròn 18 tuổi. Theo chính sách quyền riêng tư, thông báo tự động đã tắt. Config cũ vẫn giữ — nếu con tự nguyện bật lại sẽ áp dụng tiếp."*

### 4.5 Adult-child opt-in (post-18)

```
chia sẻ với cha mẹ: bật           → bật lại, dùng config cũ trong parent_links
chia sẻ với cha mẹ: tắt
chia sẻ với cha mẹ: chỉ bố        → chỉ một parent cụ thể
trạng thái chia sẻ                 → xem hiện trạng
```

Mọi lần bật/tắt **đều ghi audit log** (FR-4) — bằng chứng nếu có tranh chấp gia đình về quyền riêng tư.

### 4.6 Birthdate changes
- User đổi birthdate → cần **admin HOẶC manager xác nhận** mới có hiệu lực
- Ngăn lách luật (vd con đổi birthdate sớm để trốn giám sát)

### 4.7 Quota
- $5/tháng hard limit cho cả family (hiện tại)
- FR-2: tách per-user budget do admin set
- Đo qua `cost_monitor.py` hiện có

---

## 5. Roadmap (Foundation Releases)

### FR-1 — Hexagonal Refactor ✅ DONE (đang test runtime)
**Status:** Merged vào `dev`, chờ test trên Render staging.
**Scope:**
- Tách `main.py` thành các adapter qua Protocol
- 8 file thay đổi/tạo
- Quy ước: code/comment English; user-facing string Vietnamese
**Verification (static):**
- AST parse pass tất cả file
- Module imports OK
- `isinstance(adapter, Protocol)` pass
- `main.deps` wiring OK
**Test plan (runtime, trên staging):**
- Webhook nhận message OK
- Authorization: reject non-allowed chat_id
- Vietnamese prefix commands hoạt động
- Free-form question qua agentic loop
- Wiki retrieval + cost tracking
- Drive connection at startup
- APScheduler job registered

---

### FR-2 — Users + Roles + Auth + Quota + Birthdate + Parent Links
**Status:** PENDING (làm sau khi FR-1 verify OK)
**Scope:**
- SQLite schema: `users`, `parent_links`, `channel_bindings`
- Argon2id password hash (web auth — chưa expose qua Telegram)
- Magic link forgot-password
- Roles enforcement middleware
- Per-user quota
- Birthdate flow + manager approval
- Registration flow qua Telegram (admin add)
**Dependencies:** None (FR-1 đủ)

---

### FR-3 — SQLite + Scope + L1 Memory
**Status:** PENDING
**Scope:**
- Litestream config (cloud backup)
- Note/wiki thêm field `scope`: `private` | `group:<id>` | `everyone`
- ACL filter trong retrieval
- L1 memory: `MEMORY.md`, `USER.md` per user (frozen snapshot pattern)
- Agentic curation cho L1

---

### FR-4 — Audit + Under-18 + Recycle Bin + Notifications
**Status:** PENDING
**Scope:**
- Audit log table (immutable, append-only)
- Under-18 stealth-read cho admin (silent to member)
- Recycle bin: disclosed, 180 ngày retention, admin-only access
- Auto-purge ở tuổi 18
- Notification framework (qua channel adapter, không hardcode Telegram)

---

### FR-5 — Web UI + Password Auth
**Status:** PENDING
**Scope:**
- HTMX + Alpine.js + SSE
- Argon2id password
- Force-reset on first login (no plaintext)
- Session cookies
- Web là channel mới: `WebChannelAdapter`

---

### FR-6 — Backup / Restore Tooling
**Status:** PENDING
**Scope:**
- Export toàn bộ data của 1 user (JSON + attachments)
- Import / restore từ backup
- Migration tool cho local mode (clone SQLite + Drive → local FS)

---

### FR-7 — Tasks + Reminders + Daily Summary + Parent Digest
**Status:** PENDING
**Scope:**
- `task_store` (SQLite table)
- Reminders ở mốc: **2h, 1h, 30m, 15m** trước deadline
- Daily summary cuối ngày: việc đã hoàn thành / chưa hoàn thành
- **Kids' study schedule** (gộp vào FR-7 như task category `study`, recurring weekly)
- Mirror reminder real-time cho parent (theo 4.3 Tier 1)
- Digest cho parent theo cấu hình ở 4.3 Tier 2
**Note:** FR-8 (kids' study) **đã gộp vào FR-7** — cùng engine reminder, study chỉ là category đặc biệt.

---

### FR-9 — Expense Tracking (Ledger)
**Status:** PENDING
**Scope:**
- `ledger_entries` table (schema bên dưới)
- Fast-path keyword detection:
  - **Income:** "nhận", "thu"
  - **Expense:** "chi", "trả", "mua", ...
  - **Không xác định được** → hỏi lại user
- LLM hỗ trợ phân loại category khi user mô tả mơ hồ
- Reports: tổng tháng, by category, trend
- Per-user wallet (chưa hỗ trợ shared family wallet)
- Tương lai: budget alert qua reminder system (FR-7)

**Schema:**
```sql
ledger_entries
  id            INTEGER PK
  user_id       INTEGER FK → users.id
  kind          TEXT      -- 'income' | 'expense'
  amount        INTEGER   -- VND, KHÔNG dùng FLOAT (tránh sai số)
  category_id   INTEGER FK → categories.id
  note          TEXT      -- mô tả raw từ user
  occurred_at   DATETIME
  created_at    DATETIME
  source        TEXT      -- 'telegram' | 'web' | ...
  voided_at     DATETIME NULL  -- soft-delete, không DELETE thật

categories
  id          INTEGER PK
  user_id     INTEGER NULL  -- NULL = family-shared
  name        TEXT
  kind        TEXT          -- 'income' | 'expense'
  parent_id   INTEGER NULL  -- nested categories

INDEX (user_id, occurred_at)
INDEX (user_id, category_id, occurred_at)
```

---

### Future (post FR-9)

| Feature | Code effort | External cost |
|---------|-------------|---------------|
| Discord adapter | ~0.5 ngày | Free, instant |
| WhatsApp Business adapter | ~1 ngày | Meta verification + phí/message |
| Signal adapter | ~1-2 ngày | Cần VPS riêng (signal-cli daemon) |
| Messenger (Facebook) | ~1 ngày | Meta app review |
| L2 Graph layer | TBD | Memgraph/Neo4j embedded |
| L3 Vector layer | TBD | Voyage embeddings + sqlite-vss hoặc Qdrant |
| Skills system | TBD | Inspired by Hermes |
| Self-evolution cron | TBD | Periodic reflection trên data |
| Local deployment mode | TBD | Đóng gói chạy máy nhà |
| Local LLM | TBD | Ollama integration |

---

## 6. Decision Log

| # | Topic | Decision | Rationale | Date |
|---|-------|----------|-----------|------|
| 1 | Architecture | Modular monolith hexagonal, KHÔNG microservice | ~10 user, 1 dev, Render free tier | 2026-05-11 |
| 2 | Memory model | 3-tier: L1 text / L2 graph / L3 vector | Inspired Hermes; passive + agentic curation | 2026-05-11 |
| 3 | Number of users | ~10 (gia đình) | User confirmed | 2026-05-11 |
| 4 | Roles | 4: admin / manager / member / readonly | User confirmed | 2026-05-11 |
| 5 | Journal exception | Admin đọc journal của under-18 | User confirmed | 2026-05-11 |
| 6 | Stealth read | Admin đọc private của under-18 silent, có audit log | Option A | 2026-05-11 |
| 7 | Group chat | Schema sẵn sàng, chưa implement ngay | User confirmed | 2026-05-11 |
| 8 | Cost quota | $5/tháng hard limit | User confirmed | 2026-05-11 |
| 9 | Web tech | HTMX + Alpine.js + SSE | Option A | 2026-05-11 |
| 10 | Auth | Argon2id + force reset; magic link forgot-pw | User confirmed | 2026-05-11 |
| 11 | Storage cloud | SQLite + Litestream | Option A | 2026-05-11 |
| 12 | Embeddings | Voyage AI `voyage-3-lite` | User confirmed | 2026-05-11 |
| 13 | Command pattern | Hybrid: fast-path prefix + agentic free-form | User confirmed | 2026-05-11 |
| 14 | Recycle bin | Disclosed, 180 ngày, admin-only | User confirmed | 2026-05-11 |
| 15 | Birthdate change | Cần admin/manager xác nhận | User confirmed | 2026-05-11 |
| 16 | Notification target | Cả 2: con + parent | User confirmed | 2026-05-11 |
| 17 | Code convention | English code/comments; VN strings & VN prefix giữ nguyên | User confirmed | 2026-05-11 |
| 18 | Parent-child relation | Many-to-many qua `parent_links` | Hỗ trợ 2 cha mẹ, ly hôn, ông/bà | 2026-05-12 |
| 19 | Reminder mirror | Real-time reminder LUÔN mirror, không cấu hình | Core value của feature | 2026-05-12 |
| 20 | Digest frequency | `daily` / `weekly` / `monthly` / `off` | Linh hoạt theo giai đoạn | 2026-05-12 |
| 21 | Digest default | `daily` 21:00 khi tạo link | Mặc định mạnh, parent giảm dần khi con lớn | 2026-05-12 |
| 22 | Auto-off ở 18 | Enforce runtime, KHÔNG mutate DB | Source of truth = birthdate | 2026-05-12 |
| 23 | Adult-child opt-in | Có (Option A), lệnh `chia sẻ với cha mẹ: bật/tắt/chỉ <bố/mẹ>` | Tôn trọng tự do + gia đình VN nhiều thế hệ | 2026-05-12 |
| 24 | Audit opt-in/out | Mọi lần bật/tắt đều ghi audit log | Bằng chứng nếu tranh chấp | 2026-05-12 |
| 25 | Ledger storage | RDBMS (SQLite) | ACID, SQL reports, scale dư vài thập kỷ | 2026-05-12 |
| 26 | Ledger amount type | INTEGER VND (KHÔNG FLOAT) | Tránh sai số float làm mất tiền lẻ | 2026-05-12 |
| 27 | Channel priority | Telegram → Web → Discord → WhatsApp → Signal/Messenger | Cost + setup complexity | 2026-05-12 |
| 28 | Roadmap order | Sequential FR-2 → FR-9 | Mỗi FR đứng trên nền vững (Option A) | 2026-05-12 |
| 29 | Test strategy | Branch `dev` + Render staging service + Telegram bot test | User setup | 2026-05-12 |
| 30 | FR-7 + FR-8 | Gộp; kids' study là task category trong FR-7 | Cùng engine reminder | 2026-05-12 |

---

## 7. Open Questions

*(Hiện tại không có. Khi phát sinh thì thêm vào đây.)*

---

## 8. Current Status & Next Action

### Right now (2026-05-12)
- ✅ **FR-1** đã merge vào `dev`
- 🧪 Đang chờ user **test runtime trên Render staging** (bot test Telegram)
- 📄 `docs/ROADMAP.md` vừa được tạo (file này)

### Immediate next steps
1. User hoàn thành test FR-1 trên staging (theo checklist trong PR FR-1)
2. Nếu OK → tạo PR `dev` → `main` để deploy production
3. Sau khi FR-1 lên production: viết **plan FR-2** chi tiết (users + roles + parent_links + auth + quota)

### Pending FRs
- FR-2 (next) — Users + Roles + Auth + Quota + Birthdate + Parent Links
- FR-3..FR-9 — sequential theo Section 5

---

## 9. Cross-Machine Workflow

### Chuyển máy (desktop ↔ laptop)

```bash
git clone https://github.com/cronocks/telegram-claude-bot.git
cd telegram-claude-bot
git checkout dev
# Cài Claude Code trên máy mới, login cùng account
claude
```

Sau đó nói với Claude:
> *"Đọc `docs/ROADMAP.md` và `CLAUDE.md`, sau đó tiếp tục từ Section 8 (Current Status)."*

Claude sẽ có đầy đủ context để continue.

### Khi quyết định mới phát sinh trong lúc làm việc
- Update section liên quan trong file này
- Thêm một row mới vào **Section 6 (Decision Log)**
- Nếu trạng thái FR thay đổi → cập nhật **Section 8**
- Commit thẳng vào branch hiện tại đang làm

### Quy ước cập nhật ROADMAP
- Một quyết định = một row trong Decision Log
- Status FR: `PENDING` / `IN PROGRESS` / `DONE`
- Khi FR `DONE`: ghi commit hash hoặc PR link

### Phiên Claude mới mở cửa sổ
Câu mở đầu chuẩn:
> *"Đọc CLAUDE.md và docs/ROADMAP.md trước khi trả lời. Chúng ta đang ở [section 8 hiện tại]. Tiếp tục công việc."*
