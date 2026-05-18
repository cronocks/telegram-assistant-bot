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
| Cloud production | SQLite + **Litestream** → Cloudflare R2 (S3-compatible, egress free) |
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

**Quy tắc:**
1. Feature branch **luôn xuất phát từ `main`** (không bao giờ từ `dev`)
2. Feature merge **song song và độc lập** vào cả `dev` (để test trên staging) và `main` (để deploy production)
3. Feature branch **giữ sống** cho đến khi đã merge vào `main`; chỉ xóa sau đó

**Diagram:**
```
                     main  (canonical source — branch off từ đây)
                       │
                       ▼
                  feature/xxx
                       │
              ┌────────┴────────┐
              ▼                 ▼
        merge → dev       merge → main
       (Render staging)   (Render production)
              │                 │
        manual test ────────────┘
        nếu OK → promote        (sau khi vào main mới xóa feature)
```

- `main`: production-clean, mỗi commit đã verify trên staging
- `dev`: integration buffer cho staging — không bao giờ là base của feature mới
- `feature/*` hoặc `claude/*`: feature branches, sống từ lúc branch off cho tới khi vào main

**Lưu ý — dev drift:**
Vì feature merge song song chứ không tuần tự (`dev → main`), `dev` có thể tích lũy commit từ feature đang test, hoặc feature bị abandon → drift khỏi `main`. Khi drift quá lớn (vd có commit trên dev không bao giờ vào main): **reset dev về main** + re-merge các feature đang test. Tần suất reset tùy: định kỳ hàng tháng, hoặc khi thấy lú.

### 3.6 🚨 MANDATORY — Git operations là của user

> **Đây là quy tắc bắt buộc, mức độ cao nhất. Vi phạm = treat as critical incident.**

**Nguyên tắc tổng quát:**
User thực hiện **TẤT CẢ** thao tác liên quan đến git. Claude **mặc định = không chạm git**.

#### A. Phân vai trò (mặc định)

| Loại việc | Người làm |
|-----------|-----------|
| Tạo / checkout / xóa branch (local hoặc remote) | **User** |
| `git fetch` / `git pull` | **User** |
| `git add` / `git commit` | **User** |
| `git push` (mọi loại) | **User** |
| `git merge` / `git rebase` (vào dev, main, hay đâu khác) | **User** |
| `git reset` / `git revert` / `git cherry-pick` / `git tag` | **User** |
| Edit file trong working tree (source code, doc, config) | **Claude** |
| Đề xuất nội dung commit message, PR title/body | **Claude** |
| Giải thích diff, conflict, history, root cause | **Claude** |

#### B. Trường hợp ngoại lệ — user nhờ Claude chạy lệnh git

Đôi khi user có thể yêu cầu Claude chạy một lệnh git cụ thể (vd để tiết kiệm thời gian gõ). Khi đó **bắt buộc** quy trình 4 bước:

1. **Show** — Claude hiển thị chính xác lệnh sẽ chạy (bao gồm tất cả flags, arguments)
2. **Confirm** — Claude **dừng lại, chờ user xác nhận lại** ("go", "ok", "xác nhận", ...) — kể cả khi user vừa mới yêu cầu chạy lệnh đó
3. **Execute** — Sau khi có xác nhận, chạy đúng lệnh đã show, **không thêm / bớt / sửa gì**
4. **Report** — Hiển thị output thật của lệnh, không tóm tắt sai

Yêu cầu xác nhận **2 lần** (1 lần khi user yêu cầu + 1 lần Claude show và hỏi lại) là **chủ ý** — để chặn các tai nạn kiểu "Claude hiểu sai lệnh và tự chạy".

#### C. Examples — Good vs Bad

**✅ Good (Claude phản ứng đúng):**
> User: "Push branch lên giúp tôi."
>
> Claude: *Tôi sẽ chạy:*
> ```bash
> git push origin feature/rule
> ```
> *Xác nhận giúp tôi (reply "go") để tôi chạy lệnh này?*
>
> User: "go"
>
> Claude: [chạy lệnh] [báo output]

**❌ Bad (Claude tự ý thao tác git):**
> User: "Sửa giúp file ROADMAP.md, thêm Section 3.6."
>
> Claude: [edit file] [tự chạy `git add` + `git commit` + `git push` mà user không yêu cầu]

**❌ Bad (Claude nhảy bước):**
> User: "Push branch giúp tôi."
>
> Claude: [chạy `git push origin feature/rule` ngay, không show + chờ confirm]

**❌ Bad (Claude tự ý chọn lệnh mở rộng):**
> User: "Commit + push giúp tôi."
>
> Claude: *Tôi sẽ chạy:*
> ```bash
> git add . && git commit -m "..." && git push --force origin feature/rule
> ```
> *(Tự thêm `--force` mà user không yêu cầu — sai)*

#### D. Tại sao quy tắc này quan trọng

Đã có **incident thực tế** ngày 2026-05-12: Claude tự tạo branch `docs/roadmap` từ `dev` (thay vì `main`), khiến khi user merge branch đó vào `main` thì FR-1 (đang nằm trên dev) bị kéo theo lên production **ngoài ý muốn** của user.

Bài học: ngay cả những thao tác git "vô hại" như `git checkout -b` cũng có thể tạo hệ quả lớn ở downstream. **Chỉ user mới có context đầy đủ để quyết định.**

#### E. Edit file vs git operations

File editing (Edit/Write tool) **không phải là git operation** — Claude vẫn được phép edit file trong working tree theo Workflow trong `CLAUDE.md` (plan → confirm → execute). Việc edit file **không tự động đẩy lên git**; user vẫn là người `add`/`commit`/`push`.

#### F. Read-only operations — miễn protocol

Các thao tác **không thay đổi state** được Claude tự do thực hiện, **không cần show + confirm**:

| Thao tác | Lý do miễn |
|----------|-----------|
| `Read` tool (xem file content) | Chỉ đọc working tree |
| `Glob` (tìm file theo pattern) | Chỉ list path |
| `Grep` (tìm string trong file) | Chỉ đọc nội dung |
| `git log`, `git show`, `git diff` (view-only) | Chỉ đọc history / refs |
| `git status`, `git branch` (list), `git branch -r`, `git ls-remote` | Chỉ list state |
| `git fetch`, `git fetch -p` | Chỉ update remote-tracking refs (`origin/*`); không đụng working tree, không đổi branch local đang ở |
| Bash view-only: `ls`, `cat`, `head`, `tail`, `find` (list) | Đọc filesystem, không write |

**Tiêu chí chung:**
- Không thay đổi working tree
- Không thay đổi branch state local (HEAD, branch hiện tại, commit local)
- Không push / pull commit
- Không xóa / sửa / tạo ref

**Khi nghi ngờ:** áp dụng protocol 4 bước (an toàn hơn là phá quy tắc).

**Lưu ý về `git fetch`:** Một số người coi đây là "đụng vào git state" vì nó update refs trong `.git/`. Tuy nhiên fetch chỉ ghi vào remote-tracking refs (`refs/remotes/origin/*`), không đụng local branch refs (`refs/heads/*`), không đụng working tree, không đổi HEAD. Vì vậy ở project này coi nó là read-only — quyết định ngày 2026-05-12 (Decision Log #34).

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
**Status:** ✅ DONE — merged to `main` (production) 2026-05-18; 11 commits, 148 tests passing
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
**Status:** ✅ DONE — merged to `main` (production) 2026-05-18; 9 commits, branch `feature/FR3`
**Scope delivered:**
- SQLite schema: `notes`, `wiki_pages`, `user_memory` (migrations 009–011)
- `acl.py` — `can_read()` + `filter_visible()` helpers
- `note_index.py` — `SqliteNoteIndex` ACL/index layer + `NoteIndex` Protocol
- Dual-write on note/wiki/journal create (Drive first → SQLite second, rollback on fail)
- ACL filter on all retrieval paths (`smart_search`, `get_recent_notes`, `get_current_week_notes`, wiki `retrieve_pages`)
- `chia se` / `bo chia se` scope commands (owner-only)
- Backfill at startup: existing Drive files get SQLite rows (owner = bootstrap admin, default scope)
- `memory_store.py` — `SqliteMemoryStore` + `MemoryStore` Protocol
- `curate_memory()` trên `LLMClient` / `AnthropicLLM`
- 3 lệnh: `xem tri nho`, `xem ho so`, `cap nhat tri nho`
- L1 memory inject vào free-form Q&A (`notes_context` prepend)
- `/start` redesign + `/help [nhom]` (Decision #55)
**Dependencies:** FR-2

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
- **Reminder engine tổng quát:** offset cấu hình tùy ý + hỗ trợ recurring event — KHÔNG hardcode 4 mốc, để FR-8 plug vào dùng lại
- Daily summary cuối ngày: việc đã hoàn thành / chưa hoàn thành
- **Kids' study schedule** (gộp vào FR-7 như task category `study`, recurring weekly)
- Mirror reminder real-time cho parent (theo 4.3 Tier 1)
- Digest cho parent theo cấu hình ở 4.3 Tier 2
**Note:** Kids' study **đã gộp vào FR-7** — cùng engine reminder, study chỉ là category đặc biệt. Slot `FR-8` nay được tái dùng cho Anniversary Reminders (xem bên dưới).

---

### FR-8 — Anniversary / Memorial Reminders (Ngày kỷ niệm)
**Status:** PENDING
**Scope:**
- Bảng `anniversaries`: `user_id`, `name`, `date_type` (`lunar` | `solar`), `month`, `day`, `category` (`giỗ` | `cưới` | `khác`)
- Lịch âm → dương: dùng thư viện lunar calendar VN; **lưu nguyên ngày âm, recompute ngày dương mỗi năm tại runtime**
- Nhắc trước nhiều mốc: **30 / 15 / 7 / 3 / 1 ngày** + thêm 1 lần vào đúng ngày kỷ niệm
- Cấu hình per-anniversary: bật/tắt từng mốc hoặc đổi tần suất
- Tái dùng reminder engine tổng quát của FR-7
**Dependencies:** FR-7 (reminder engine); thư viện âm lịch (chọn lib ở giai đoạn implement)
**Note:** Đây KHÔNG phải "kids' study" cũ — kids' study đã gộp vào FR-7 (Decision #30). Slot FR-8 được tái dùng (Decision #45).

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
| 31 | Git workflow | Feature LUÔN từ `main`; merge song song vào dev (test) + main (prod); xóa feature chỉ sau khi vào main | User định nghĩa chuẩn workflow; `dev` không bao giờ là base | 2026-05-13 |
| 32 | Git role split | Claude tạo branch + commit + push + đưa link; **user merge** (git bash local) + **xóa feature branch**; Claude chỉ hỗ trợ git khi user nhờ | User giữ quyền kiểm soát tuyệt đối với main/dev | 2026-05-12 |
| 33 | 🚨 Git ops 100% user-owned (replaces #32) | User làm **TẤT CẢ** thao tác git. Claude mặc định = không chạm git. Khi user nhờ chạy lệnh git: bắt buộc protocol 4 bước (show → confirm → execute → report). Edit file không phải git op. | Incident 2026-05-12: Claude tự branch off `dev` → khi merge vào main đã kéo FR-1 lên production ngoài ý muốn. Quy tắc cũ (#32) cho Claude tự create branch → vẫn còn footgun. | 2026-05-12 |
| 34 | Read-only ops miễn protocol | Claude tự do chạy: Read/Glob/Grep, `git log/show/diff/status/branch/ls-remote`, `git fetch` (cả `-p`), Bash view-only — không cần show + confirm | Read-only không thay đổi state nào nguy hiểm; ép protocol cho chúng làm chậm việc mà không đem lại safety | 2026-05-12 |
| 35 | Shell content English only | Commit messages, inline bash comments, shell script content, và lệnh bash gợi ý trong chat reply — **bắt buộc English**, không có Vietnamese (kể cả không dấu) | Git bash trên Windows không render Vietnamese diacritics tốt → vỡ font khi user paste. Tránh ambiguity bằng cách dùng English duy nhất. | 2026-05-12 |
| 36 | Migration runner | File-based idempotent runner (`db/migrations/*.sql`, sorted numerically, `_schema_version` table) thay vì inline Python | Dễ audit, dễ replay trên máy mới, migrations là plain SQL không cần import app code | 2026-05-15 |
| 37 | `detect_types` bị loại | Bỏ `sqlite3.PARSE_DECLTYPES` khỏi connection config | Auto-convert DATE → `date` object làm `date.fromisoformat()` nhận `date` thay vì `str` → TypeError. Manual ISO parsing an toàn hơn. | 2026-05-15 |
| 38 | Username first-set vs change | First-set (NULL → value) dùng `set_username_direct` không cần approval; đổi lần sau qua `username_changes` queue + admin duyệt + rate-limit 30 ngày | UX tốt cho lần đầu; sau đó cần kiểm soát để tránh impersonation | 2026-05-15 |
| 39 | `_normalize_char` tách riêng | Ba tầng normalize: `_normalize_text` (full), `_normalize_prefix` (giữ trailing space), `_normalize_char` (per-char, không collapse whitespace) | `normalize_vn(" ")` → `""` do `.strip()` → offset sai khi map normalized length → original length. `_normalize_char` fix bằng cách bỏ whitespace collapsing. | 2026-05-15 |
| 40 | `parent_links` bảng riêng | Quan hệ cha-con là bảng `parent_links` thay vì column `parent_id` trên `users` | Hỗ trợ multi-parent, soft history (deactivate thay vì xóa), dễ audit lịch sử thay đổi | 2026-05-15 |
| 41 | Per-user quota store trong SQLite | `user_quotas` bảng riêng, auto-reset khi sang tháng mới (detect bằng `month` column) | Không cần cron reset; reset xảy ra lazy tại lần ghi đầu tiên của tháng mới | 2026-05-15 |
| 42 | argon2-cffi cho password | `argon2-cffi` wrapper (argon2id) thay vì bcrypt hay PBKDF2 | Argon2id là winner của Password Hashing Competition 2015; memory-hard → chống GPU brute-force; đã quyết định từ Decision #10 | 2026-05-15 |
| 43 | Persist strategy production | SQLite + Litestream replicate WAL lên Cloudflare R2 (S3-compatible). Deploy qua Docker. | Render free tier không có Persistent Disk → DB ephemeral. Litestream stream WAL (~1s) lên object storage, restore lúc boot. R2 free tier rộng + egress miễn phí. Google Drive không phải S3-compatible nên không làm target Litestream được. | 2026-05-16 |
| 44 | Tách môi trường qua APP_ENV | `config.py` thêm `APP_ENV` (`local`/`staging`/`production`); staging+production bắt buộc set `SQLITE_PATH` rõ ràng (fail-fast nếu thiếu); mỗi env dùng `LITESTREAM_DB_NAME` riêng trên R2 | Tránh deploy nhầm ghi vào DB local, và tránh staging/production đè data nhau trên R2. Unit test dùng SQLite `:memory:` nên đã cô lập sẵn. | 2026-05-16 |
| 45 | FR-8 tái dùng = Anniversary/Memorial reminders | Slot `FR-8` (cũ = kids' study, đã gộp vào FR-7 theo #30) tái dùng cho tính năng nhắc ngày kỷ niệm (giỗ/cưới/dịp khác), đặt ngay sau FR-7. Là FR riêng, không gộp FR-7 | Nhắc giỗ/kỷ niệm là core value của family knowledge system; kéo theo dependency âm lịch + logic recompute hằng năm nên đủ lớn để đứng riêng; phụ thuộc reminder engine nên xếp sau FR-7 | 2026-05-17 |
| 46 | Reminder engine tổng quát | FR-7 thiết kế reminder engine với offset cấu hình tùy ý + hỗ trợ recurring event, KHÔNG hardcode `2h/1h/30m/15m` | FR-8 cần offset thang ngày (30..1 ngày) và bật/tắt được; thiết kế tổng quát từ đầu tránh rework | 2026-05-17 |
| 47 | Lưu ngày âm lịch nguyên dạng | Anniversary lịch âm lưu `month`/`day` âm; recompute ngày dương mỗi năm tại runtime, không lưu cứng ngày dương | Ánh xạ âm→dương đổi theo từng năm; single source of truth = ngày âm | 2026-05-17 |
| 48 | Scope storage | Option A — SQLite metadata tables (`notes`, `wiki_pages`) làm lớp ACL/index; Drive giữ nội dung | Tận dụng SQLite ACID + Litestream backup; Drive search không hỗ trợ owner/scope filter | 2026-05-18 |
| 49 | Scope values | Chỉ `private` + `everyone`; không `group` | Quy mô gia đình ~10 người, `everyone` = cả nhà là đủ; giảm complexity | 2026-05-18 |
| 50 | Default scope | note/journal → `private`; wiki → `everyone` | Note/journal là cá nhân theo bản chất; wiki là tri thức chung gia đình | 2026-05-18 |
| 51 | L1 Memory storage | SQLite (`user_memory` table, kind: `memory`\|`user`), không lưu file Drive | Litestream đã backup sẵn; query/update đơn giản; không tốn Drive quota | 2026-05-18 |
| 52 | Admin private read | FR-3 ACL strict — admin KHÔNG đọc note private của người khác | Stealth-read cần audit log đầy đủ → để FR-4 làm đúng, không làm nửa vời | 2026-05-18 |
| 53 | Per-person sharing | Hoãn sang FR sau; `acl.py` thiết kế extensible để thêm `note_shares` không phá API | Gia đình hiện tại không cần share với từng người riêng lẻ | 2026-05-18 |
| 54 | L1 curation trigger | FR-3 manual (`cap nhat tri nho`); cron tự động để FR sau | Đơn giản hóa FR-3 scope; tránh cron logic phức tạp trước khi có audit | 2026-05-18 |
| 55 | /start + /help UX | `/start` hiển thị 6 nhóm lệnh tổng quan; `/help [nhom]` cho chi tiết từng nhóm | Số lệnh tăng nhiều sau FR-2 (user/quota/birthdate...); dump 1 block dài không đọc được; `/help` là pattern chuẩn Telegram bot | 2026-05-18 |
| 56 | L1 memory inject | Prepend `memory` snapshot của user vào `notes_context` trước khi gọi `LLMClient.ask()` trong `_handle_general_question` | Cách đơn giản nhất để Claude "biết" người dùng là ai mà không thay đổi signature của `ask()`; notes_context vốn đã là free-form string nên prepend không phá API | 2026-05-18 |

---

## 7. Open Questions

*(Hiện tại không có. Khi phát sinh thì thêm vào đây.)*

---

## 8. Current Status & Next Action

### Right now (2026-05-18)
- ✅ **FR-1** merged to `main` (production)
- ✅ **FR-2** merged to `main` (production) 2026-05-18 — 11 commits, 148 tests passing
  - SQLite schema: users, channel_bindings, invite_codes, birthdate_changes, username_changes, parent_links, user_quotas, password_hash
  - Docker runtime + Litestream → Cloudflare R2 (production + staging)
  - Multi-user registry, roles (admin/manager/member/readonly), soft-delete
  - Invite code registration flow (Telegram)
  - Birthdate change flow (manager approval)
  - Username set + change flow (admin approval, 30-day rate-limit)
  - Parent-child links (soft history)
  - Per-user monthly token quota (lazy monthly reset)
  - Argon2id password infrastructure (not yet exposed via commands)
- ✅ **FR-3** merged to `main` (production) 2026-05-18 — 9 commits, branch `feature/FR3`
  - SQLite schema: notes, wiki_pages, user_memory (migrations 009–011)
  - ACL layer: `acl.py` + `SqliteNoteIndex` + `NoteIndex` Protocol
  - Dual-write + ACL filter trên tất cả retrieval paths
  - `chia se` / `bo chia se` commands + startup backfill
  - L1 Memory: `SqliteMemoryStore`, `curate_memory()`, 3 lệnh tri nhớ, inject vào Q&A
  - `/start` redesign + `/help [nhom]`

### Immediate next steps (FR-4)
- FR-4: Audit + Under-18 Stealth-read + Recycle Bin + Notifications
  - Audit log table (immutable, append-only) — ghi lại mọi thao tác nhạy cảm
  - Under-18 stealth-read cho admin (silent to member), dựa trên birthdate
  - Recycle bin: disclosed, 180 ngày retention, admin-only access
  - Auto-purge data khi child tròn 18
  - Notification framework (channel-agnostic, không hardcode Telegram)
- **Cleanup pending:** xóa 2 Render service cũ (`telegram-claude-bot`, `test-telegram-claude-bot`) sau khi confirm production ổn định trên service mới

### Pending FRs
- FR-4..FR-9 — sequential theo Section 5

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
