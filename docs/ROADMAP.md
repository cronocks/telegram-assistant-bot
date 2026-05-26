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

**Quy trình 4 bước (tuần tự, bắt buộc):**
1. Commit thay đổi lên `feature/xxx`
2. Merge `feature/xxx` → `dev`, push lên **staging** để test
3. Xác nhận staging **không còn lỗi**
4. Merge `feature/xxx` → `main`, push lên **production**

**Quy tắc bất biến:**
- Feature branch **luôn xuất phát từ `main`** (không bao giờ từ `dev`)
- **Không merge thẳng vào `main` khi chưa test trên staging** — mọi feature phải qua `dev` trước
- Feature branch **giữ sống** cho đến khi đã merge vào `main`; chỉ xóa sau đó

**Diagram:**
```
         main  (canonical source — branch off từ đây)
           │
           ▼
      feature/xxx  ──── 1. commit changes here
           │
           │ 2. merge → dev
           ▼
          dev  (Render staging)
           │
           │ 3. test — confirm no errors
           │
           │ 4. merge → main (only after staging OK)
           ▼
          main  (Render production)
           │
        (delete feature branch after merge to main)
```

- `main`: production-clean, mỗi commit đã verify trên staging
- `dev`: integration buffer cho staging — không bao giờ là base của feature mới
- `feature/*`: feature branches, sống từ lúc branch off cho tới khi vào main

**Lưu ý — dev drift:**
`dev` có thể tích lũy commit từ feature bị abandon hoặc chưa vào main → drift khỏi `main`. Khi drift quá lớn: **reset dev về main** + re-merge các feature đang test. Tần suất reset tùy: định kỳ hàng tháng, hoặc khi thấy lú.

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
**Status:** ✅ DONE — code + staging test pass (checklist 3.1–3.7, 2026-05-19); sẵn sàng merge → `main`
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

### FR-3.5 — Privilege Elevation (sudo)
**Status:** ✅ DONE — merged to `main` 2026-05-21 (kế hoạch chi tiết giữ tại `docs/FR-3.5-PLAN.md` như reference)
**Lý do:** Production KHÔNG dùng admin làm tài khoản mặc định. Tài khoản Telegram chính chạy role `manager`; cần cơ chế nâng quyền tạm thời lên `admin` khi cần thao tác quản trị.
**Scope delivered:**
- Migration 013: bảng `elevation_sessions` (phiên nâng quyền theo `(channel, chat_id)`, TTL 15 phút, lazy expiry) + `sudo_attempts` (đếm fail + lockout)
- `elevation_store.py` — `SqliteElevationStore` + Protocol `ElevationStore`
- Role override ở `main.py`: nếu có phiên elevation còn hạn → `dataclasses.replace(user, role="admin")` (KHÔNG đổi `id`/`name` — audit ghi đúng người thật)
- Lệnh `dat mat khau: <mật khẩu>` — đặt/đổi mật khẩu admin, chỉ từ tài khoản natively-admin (cũng là cơ chế recovery; không làm "quên mật khẩu" riêng)
- Lệnh `sudo: <mật khẩu>` — nâng quyền (gated role `manager` + verify Argon2id qua hash của bất kỳ user role `admin` nào)
- Lệnh `thoat sudo` — hạ quyền ngay; `toi la ai` bổ sung dòng trạng thái elevation
- Rate-limit: 5 fail → khóa 15 phút; bot tự xóa message chứa mật khẩu qua `delete_message`; audit stdout (`sudo_elevate` / `sudo_drop` / `sudo_fail` / `sudo_locked` / `password_set`)
- `delete_message` thêm vào Protocol `ChannelAdapter` + implement trên `TelegramAdapter`
**Bổ sung kèm theo:** lệnh `doi role: <tên/id> <role mới>` — admin đổi role của user đã tồn tại (safety guard: không cho admin tự hạ role chính mình)
**Dependencies:** FR-2 (hạ tầng Argon2id), FR-3

---

### FR-4 — Audit + Under-18 + Recycle Bin + Notifications
**Status:** ✅ DONE — merged to `main` 2026-05-22 (commits `e76a98c` → `3496f3e`); plan chi tiết tại `docs/FR-4-PLAN.md`
**Scope delivered:**
- Sub 4.1a — Audit log infrastructure: migration 014 (`audit_log` append-only, không UPDATE/DELETE bằng trigger), `audit.py` + `SqliteAuditLog` + Protocol `AuditLog`, wire vào `CoreDeps`
- Sub 4.1b — Migrate sudo events (`sudo_elevate` / `sudo_drop` / `sudo_fail` / `sudo_locked` / `password_set`) từ stdout sang bảng `audit_log` (giữ stdout làm dev log song song)
- Sub 4.2 — Under-18 stealth-read: ACL cho admin đọc note/wiki/journal của user under-18 **không thông báo cho member**; mỗi lần đọc emit audit `stealth_read` với `target_user_id` + `resource_id`
- Sub 4.3 — Recycle bin: migration 015 (`recycle_bin`), lệnh admin `xem thung rac`, `khoi phuc <id>`, `xoa vinh vien <id>`; mọi soft-delete đi qua recycle bin, retention 180 ngày
- Sub 4.4 — Auto-purge scheduled jobs: APScheduler chạy daily — purge entry recycle bin >180 ngày + tự hạ role/disable cờ stealth-read khi user đủ 18 tuổi (runtime check theo birthdate, không mutate DB)
- Sub 4.5 — Persistent notification queue: migration mới `pending_notifications`; `notification_service.py` enqueue thay vì trực tiếp `channel.send()` → flush job APScheduler retry; survive process crash
- Refactor `deps.py`: tách `CoreDeps` ra file riêng để chuẩn bị split `cmd_*` modules
**Dependencies:** FR-2 (users + birthdate), FR-3 (ACL framework), FR-3.5 (audit precedent)

---

### FR-5 — Web UI + Password Auth
**Status:** ✅ DONE — merged to `main`; commits `b80e836` → `2cc9833`; 40/40 tests passing. Plan chi tiết tại `docs/FR-5-PLAN.md`.
**Scope delivered:**
- Migration 016 (`web_sessions` + `users.must_change_password`); `web_session_store.py` (SqliteWebSessionStore, DB-revocable, không JWT)
- `web_channel.py` — `WebChannelAdapter` channel mới, in-memory `asyncio.Queue` per-user cho SSE push
- `web_router.py` — FastAPI router: `/login`, `/logout`, `/setup-password`, `/chat`, `/chat/send`, `/chat/stream` (SSE qua `sse-starlette`); cookie `web_session` HttpOnly + SameSite=Lax + Secure (non-local)
- Brute-force protection: reuse `sudo_attempts` table với `channel="web"`, 5 fails → khóa 15 phút; audit events `web_login` / `web_logout` / `web_login_failed` / `web_password_set`
- Templates Jinja2 + HTMX + Alpine.js (CDN, no build step); design **glass/dark mode** (CSS variables, backdrop-blur, gradient indigo→violet, toggle 🌙/☀️ floating hoặc trong nav, persist localStorage)
- Lệnh admin `dat web pass: <user>, <password>` — admin đặt mật khẩu + set `must_change_password=1` → user đăng nhập lần đầu bị force-reset
- `CoreDeps` riêng cho web (`web_deps` trên `app.state`) với `WebChannelAdapter` thay `TelegramAdapter` — share rest của adapter pool
- Tests: `test_web_session.py` (14), `test_web_channel.py` (16), `test_web_auth.py` (10) = 40/40 PASS, 0 warnings
**Dependencies:** FR-2 (Argon2id), FR-3.5 (sudo password infra reused)

---

### FR-5.5 — Web Chat History Sidebar
**Status:** ✅ DONE — merged to `main` 2026-05-23; commits `1c6373d` → `8d3d19f`; 58 tests passing. Plan chi tiết tại `docs/FR-5.5-PLAN.md`.
**Lý do:** FR-5 hiện tại chat web là single-thread, reload page mất hội thoại trước. Thêm sidebar bên trái liệt kê các phiên cũ — UX giống Claude.ai/ChatGPT.
**Scope:**
- Migration mới (017): bảng `web_conversations` (id, user_id, title, created_at, updated_at) + `web_messages` (id, conversation_id, role, text, created_at)
- `web_conversation_store.py` + Protocol `WebConversationStore`
- Modify `WebChannelAdapter` để persist inbound + outbound message vào DB theo conversation
- Modify `web_router.py`: API CRUD conversations, route `/chat/<id>`, search endpoint (LIKE-based)
- Modify templates: sidebar collapsible (mặc định collapsed trên mobile), conversation list, new chat button, rename inline, search box
- LLM title generation: 1 call Haiku 4.5 sau message đầu tiên → set title; user có thể rename
- Admin stealth-read: extend FR-4 ACL cho hội thoại web user under-18, emit audit `stealth_read` với `target_type=web_conversation`
- Retention: vĩnh viễn, không auto-purge (KHÔNG tích hợp với recycle bin FR-4)
- Scope hội thoại: chỉ kênh web, KHÔNG gộp lịch sử Telegram (Telegram không persist message; effort không xứng)
- Search: `LIKE '%query%'` đơn giản trên `web_messages.text`, không dùng FTS5 (scale gia đình ~10 user × vài trăm message → đủ nhanh); migration sang FTS5 sau là additive
- Tests: store CRUD, conversation isolation per user, title generation flow, search, stealth-read audit
**Dependencies:** FR-5 (web channel + session infra), FR-4 (audit log cho stealth-read)
**Note:** Tách riêng khỏi FR-5 vì scope đáng kể (~1.5-2 ngày work); FR-5 đã testable đủ để ship trước. FR-5.5 chỉ là additive — không phá API FR-5.

---

### FR-6 — Backup / Restore Tooling
**Status:** DONE (2026-05-23) — branch `feature/FR6`
**Scope:**
- Export toàn bộ data của 1 user (JSON + attachments)
- Import / restore từ backup
- Migration tool cho local mode (clone SQLite + Drive → local FS)

**Implementation summary (7 sub-tasks + staging fixes + Option C):**
- **6.1** `BackupEngine` + `generate_export()`: ZIP in-memory (BytesIO), rate-limit 5 phút/user, audit `data_export`
- **6.2** `parse_import()` + `apply_import()`: validate ZIP, transactional restore (user → bindings → quota → notes → wiki → memory → conversations → parent_links), best-effort Drive rollback
- **6.3** Web routes + `templates/import.html`: `GET /settings/export`, `GET /admin/users/{id}/export`, `GET /admin/import`, `POST /admin/import/preview`, `POST /admin/import/apply`; import preview token 5 phút
- **6.4** Telegram commands: `xuat du lieu` (self) + `xuat du lieu: <tên>` (admin); Drive upload vào `Claude-Notes/Backups/`; wire `BackupEngine` vào `CoreDeps`
- **6.5** `tools/local_migrate.py`: CLI standalone, `sqlite3.backup()` read-only, idempotent file mirror từ Drive, `--dry-run`/`--users`/`--include-deleted`
- **6.6** Wiring: instantiate `BackupEngine` trong `main.py`, pass vào `deps`, `web_deps`, `init_web_router()`
- **6.7** Tests: 82 test cases (`test_backup_engine.py`, `test_backup_routes.py`, `test_local_migrate.py`)
- **Staging fix — IDM double-request**: download token redirect pattern (D81); `GET /settings/export/download?token=` route mới
- **Staging fix — self-export guard**: `xuat du lieu` self guard khi có trailing text để tránh nhầm với admin command
- **Option C — self-service password**: lệnh `doi web pass: <mat_khau>` (mọi user, D82) + web form `GET/POST /settings/export/password` + `templates/settings_password.html`; link trong sidebar chat

---

### FR-7 — Tasks + Reminders + Daily Summary + Parent Digest
**Status:** ✅ DONE — branch `feature/FR7`, 867 tests passing; plan chi tiết tại `docs/FR-7-PLAN.md`
**Scope delivered:**
- Migrations 018–020: bảng `tasks`, `task_reminders`; 2 cột `users.daily_summary_time` + `users.morning_default_time`
- `task_store.py` — `SqliteTaskStore` CRUD (list by user/status/category, soft-delete)
- `reminder_store.py` — `SqliteReminderStore` CRUD + ready-to-fire query
- `reminder_engine.py` — scan + emit + lazy recurring expansion + parent mirror runtime (D7) + grace 1h missed (D12)
- `task_parser.py` — `TaskParser` Haiku 4.5 tool-use; cải thiện system prompt cho Vietnamese time formats (`10h tối`, `22h`, `chiều thứ 3`, v.v.)
- Telegram commands: `tao task:`, `xong task:`, `huy task:`, `danh sach task`, `task <id>`, `lich hoc:`, `hoan task:`, `tom tat hom nay`, `cau hinh tong ket:`, `cau hinh gio mac dinh:`
- Study schedule management (bổ sung ngoài plan gốc): `danh sach lich hoc`, `sua lich hoc:`, `huy lich hoc:`
- Web routes + templates: `GET/POST /tasks`, `/tasks/new`, `/tasks/{id}`, `/tasks/{id}/edit`, `/tasks/{id}/complete`, `/tasks/{id}/delete`
- Scheduled jobs: `scan_reminders` mỗi 1 phút, `send_daily_summary`, `send_parent_digest`
- `core_handler.py` refactor: tách business logic vào 7 cmd_* modules (`cmd_utils`, `cmd_user`, `cmd_audit`, `cmd_notes`, `cmd_sudo`, `cmd_wiki`, `cmd_task`); `core_handler.py` giữ vai trò dispatcher + help/start
- Fix `WebChannelAdapter.send_with_inline_keyboard` (fallback silent khi web channel nhận inline keyboard)
- FR-7 group thêm vào `/start` menu + `/help cong viec`
**Note:** Kids' study **đã gộp vào FR-7** — cùng engine reminder, study chỉ là category đặc biệt. Slot `FR-8` nay được tái dùng cho Anniversary Reminders (xem bên dưới).

---

### FR-8 — Anniversary / Memorial Reminders (Ngày kỷ niệm)
**Status:** ✅ DONE — branch `feature/FR8`, 968 tests passing (101 FR-8 tests); plan chi tiết tại `docs/FR-8-PLAN.md`
**Scope delivered:**
- Migrations 022–023: bảng `anniversaries` (với `year` column optional) + `anniversary_reminders` (UNIQUE constraint idempotent)
- `lunar_utils.py` — `lunar_to_solar()` + `compute_anniversary_solar_date()`; lib `lunardate==0.2.2`
- `anniversary_store.py` — `SqliteAnniversaryStore` CRUD + soft-delete + validation
- `anniversary_engine.py` — `AnniversaryEngine.compute_year()`, `tick()`, `cancel_all_for_anniversary()`; fire at 08:00 VN, grace 12h
- `cmd_anniversary.py` — 5 Telegram handlers: `them ky niem`, `danh sach ky niem`, `ky niem <id>`, `xoa ky niem`, `sua ky niem`
- Web routes: 7 routes `/anniversaries/*` + `templates/anniversaries.html`, `anniversary_form.html`, `anniversary_view.html`
- Scheduled jobs: `anniversary_tick` (60s) + `compute_anniversary_year` (startup + Jan 1 00:05 VN)
- Audit events: `anniversary_created`, `anniversary_updated`, `anniversary_deleted`, `anniversary_reminder_fired`, `anniversary_reminder_missed`
- Parent mirror runtime (under-18 check, Decision #22 consistent)
**Dependencies:** FR-7 (reminder engine infra)
**Note:** Đây KHÔNG phải "kids' study" cũ — kids' study đã gộp vào FR-7 (Decision #30). Slot FR-8 được tái dùng (Decision #45).
**⚠️ Pending before merge:** Tạo `db/migrations/024_add_year_to_anniversaries.sql` (`ALTER TABLE anniversaries ADD COLUMN year INTEGER`) — production DB đã chạy 022 cũ chưa có `year` column.

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
| 31 | Git workflow | Feature LUÔN từ `main`; merge tuần tự: feature → dev (staging test) → xác nhận OK → feature → main (prod); xóa feature chỉ sau khi vào main | User định nghĩa chuẩn workflow; `dev` không bao giờ là base; không merge thẳng main khi chưa qua staging | 2026-05-26 |
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
| 57 | sudo = nâng role, không đổi danh tính | FR-3.5 elevation override `role`→`admin` tạm thời; user vẫn là chính mình (id/name giữ nguyên) | Audit ghi đúng người thật thực hiện; `created_by` chính xác; không cần re-bind channel | 2026-05-19 |
| 58 | sudo TTL + gating | Phiên elevation hết hạn sau 15 phút (lazy expiry); lệnh `sudo` chỉ role `manager` dùng được, cổng chính là mật khẩu admin | 15p đủ cho thao tác quản trị gia đình; gating role + password + rate-limit là phòng thủ nhiều tầng | 2026-05-19 |
| 59 | Recovery mật khẩu admin | KHÔNG làm tính năng quên mật khẩu riêng; `dat mat khau` chỉ chạy từ tài khoản natively-admin (admin qua channel binding) → vừa đặt lần đầu vừa là recovery | Tài khoản bootstrap admin vốn đã là admin nhờ binding, không cần mật khẩu để chứng minh → là đường recovery sẵn có | 2026-05-19 |
| 60 | `liet ke` sort theo createdTime | Danh sách file sắp xếp `createdTime desc` (không phải `modifiedTime`) | Đúng nghĩa "file mới tạo lên trên"; journal cũ append hằng ngày không nhảy lên đầu | 2026-05-19 |
| 61 | Audit log immutable | Bảng `audit_log` chống UPDATE/DELETE bằng SQLite trigger (RAISE FAIL); chỉ INSERT | Audit trail không được sửa retroactively; bằng chứng pháp lý/gia đình nếu tranh chấp | 2026-05-21 |
| 62 | Under-18 stealth không notify | Admin đọc note/wiki/journal của user under-18 KHÔNG gửi notification cho member; chỉ emit audit `stealth_read` | Bản chất stealth-read là silent; thông báo sẽ làm vô hiệu hóa feature; audit đủ để truy vết | 2026-05-21 |
| 63 | Recycle bin admin-only + 180d | `recycle_bin` chỉ admin xem/restore/purge; retention 180 ngày fix cứng; mọi soft-delete đi qua đây | Member không cần quyền tự khôi phục (admin gia đình quản); 180 ngày đủ dài để phát hiện mất nhầm; cố định để tránh config phức tạp | 2026-05-21 |
| 64 | Auto-purge tuổi 18 = scheduled job + runtime check | Daily APScheduler job chạy purge + check tuổi; quyền under-18 (stealth-read) enforce theo birthdate tại runtime, KHÔNG mutate DB | Source of truth = birthdate; mutate DB tạo dual-source dễ drift; scheduled job chỉ là cleanup, ACL tự hết hiệu lực | 2026-05-21 |
| 65 | Notification queue persistent | `pending_notifications` table thay vì in-memory queue; flush job APScheduler retry với backoff | Channel adapter có thể fail (Telegram timeout, rate-limit); process restart không mất notification; survive crash | 2026-05-22 |
| 66 | Web session = DB-revocable cookie (không JWT) | Cookie chứa opaque token 32 byte hex; `web_sessions` table có `revoked_at`; logout = set `revoked_at` | JWT không revoke được giữa lúc TTL; gia đình ~10 user — DB lookup mỗi request không phải bottleneck; cần force-logout khi đổi mật khẩu | 2026-05-22 |
| 67 | CSRF = SameSite=Lax thay token | Cookie `SameSite=Lax` + `HttpOnly` + `Secure` (non-local); KHÔNG implement explicit CSRF token | Đủ chống CSRF cho gia đình ~10 user; giảm complexity (không cần token rotation, hidden form field, header check); migration path sang token sau này là additive nếu cần | 2026-05-22 |
| 68 | Admin đặt mật khẩu + force-reset flag | Lệnh `dat web pass: <user>, <pw>` admin-only; set `users.must_change_password=1`; user login lần đầu bị redirect `/setup-password` đổi mật khẩu mới trước khi vào chat | Tránh admin biết password thật của user (admin chỉ biết temp); pattern chuẩn cho first-login setup | 2026-05-22 |
| 69 | Web `CoreDeps` riêng | `web_deps` instance riêng trên `app.state` với `WebChannelAdapter` thay `TelegramAdapter`; share rest của adapter pool (DB, store, audit, ...) | Channel khác nhau cần channel adapter khác; nhưng auth/storage/audit cùng pool → tách `CoreDeps` riêng là pattern sạch nhất | 2026-05-22 |
| 70 | FR-5.5 tách riêng khỏi FR-5 | Web chat history sidebar tách thành FR-5.5 riêng, làm sau khi FR-5 merge `main`; KHÔNG gộp vào FR-5 | Scope đáng kể (~1.5-2 ngày: migration mới, store mới, modify channel + router + templates, LLM title, stealth-read); FR-5 đã testable đủ để ship; PR nhỏ dễ review hơn; FR-5.5 chỉ additive | 2026-05-23 |
| 71 | Web chat history web-only | Sidebar chỉ liệt kê hội thoại từ kênh web, KHÔNG gộp lịch sử Telegram | Telegram hiện không persist message (chỉ webhook → reply); persist từ đầu tốn refactor `core_handler` + không lấy lại được lịch sử cũ; Telegram client đã có UX lịch sử riêng | 2026-05-23 |
| 72 | Title generation = LLM Haiku | Sau message đầu tiên gọi Haiku 4.5 generate title ~3-7 từ; user có thể rename inline | Cost ~$0.0001/conversation (1000 chat = $0.10) — không đáng kể với gia đình; truncate first message vô dụng khi user gõ "Hi"; pattern chuẩn của Claude.ai/ChatGPT | 2026-05-23 |
| 73 | Search = LIKE đơn giản v1 | Search lịch sử dùng `WHERE text LIKE '%query%'`, không dùng FTS5 | Scale gia đình ~10 user × vài trăm message → LIKE dưới 50ms; FTS5 thêm virtual table + trigger sync mỗi insert phức tạp không cần thiết; migration sang FTS5 sau là additive | 2026-05-23 |
| 74 | Retention vĩnh viễn | Hội thoại web giữ vĩnh viễn, không auto-purge, KHÔNG tích hợp với recycle bin FR-4 | User muốn giữ lâu dài làm reference (giống Claude.ai); volume nhỏ với gia đình ~10 user nên không lo storage; user tự delete nếu muốn (sẽ thêm UI delete sau) | 2026-05-23 |
| 75 | Admin stealth-read hội thoại web | Extend ACL FR-4: admin đọc được hội thoại web của user under-18 không thông báo cho member; emit audit `stealth_read` với `target_type=web_conversation` | Consistent với policy FR-4 cho note/wiki/journal under-18; hội thoại web bản chất là private content; auto-tắt khi user đủ 18 theo runtime check birthdate | 2026-05-23 |
| 76 | BackupEngine là concrete class, không phải Protocol | `BackupEngine` không implement Protocol; inject trực tiếp vào `CoreDeps` với type annotation `"BackupEngine \| None"` | Backup là singleton service (không có multiple implementation); Protocol sẽ over-engineer; TYPE_CHECKING guard tránh circular import | 2026-05-23 |
| 77 | ZIP export in-memory, không temp file | `_build_zip()` dùng `io.BytesIO`, không ghi ra disk | Render ephemeral FS không đáng tin cậy; BytesIO sạch hơn, không cần cleanup, no partial file nếu crash | 2026-05-23 |
| 78 | Rate-limit export = in-memory dict | `_last_export_at: dict[int, datetime]` trên instance `BackupEngine`; 5 phút/user | Đủ cho quy mô gia đình; không cần DB table chỉ để lưu timestamp rate-limit; reset khi restart là acceptable | 2026-05-23 |
| 79 | Import preview token = in-memory UUID, TTL 5 phút | `_import_tokens: dict[str, dict]` trong `web_router.py`; token 1 lần dùng; cleanup lazy | Tránh DB table chỉ để bridge preview → apply (2 request liên tiếp); TTL 5 phút đủ cho UX; không cần persist qua restart | 2026-05-23 |
| 80 | Drive upload backup dùng API trực tiếp, không qua NoteStore Protocol | `BackupEngine.upload_to_drive()` gọi thẳng `_get_service()` từ `drive_client.py`; tạo subfolder `Claude-Notes/Backups/` | `NoteStore.save_note()` chỉ nhận string content (encode UTF-8), không xử lý được ZIP binary; BackupEngine là concrete class (D76) nên không vi phạm hexagonal — chỉ core_handler mới bắt buộc qua Protocol | 2026-05-23 |
| 81 | Web export dùng download token redirect để tránh IDM double-request | `GET /settings/export` generate ZIP → lưu vào `_download_tokens` dict (TTL 60s) → redirect 303 tới `GET /settings/export/download?token=xxx`; token dùng `get` (không `pop`) để IDM retry trong TTL không bị 410 | IDM và download manager thường gửi 2 request HEAD+GET tới cùng URL; redirect tách URL generate (rate-limited) khỏi URL download (token-gated); token TTL-based thay vì one-time-use để support retry trong window | 2026-05-23 |
| 82 | Self-service web password qua Telegram channel binding | Lệnh `doi web pass: <mat_khau>` cho mọi user (không cần admin); channel binding là identity proof — không cần current password; set `must_change_password=False`; auto-delete message chứa password; web form `/settings/password` dùng current password để xác nhận | User thường không có admin để set password ban đầu; Telegram channel binding đủ để chứng minh identity; web form cần current password vì không có channel binding identity proof | 2026-05-23 |
| 83 | Refactor `core_handler.py` → 7 cmd_* modules | Business logic tách vào `cmd_utils`, `cmd_user`, `cmd_audit`, `cmd_notes`, `cmd_sudo`, `cmd_wiki`, `cmd_task`; `core_handler.py` giữ vai trò dispatcher + `/start` + `/help`. Mỗi module import `CoreDeps` từ `deps.py` | File 3662 dòng khó maintain, test import chậm; tách theo domain giúp locate code nhanh, test isolation tốt hơn, PR review nhỏ hơn | 2026-05-24 |
| 84 | Bổ sung 3 lệnh quản lý lịch học ngoài scope FR-7 gốc | `danh sach lich hoc`, `huy lich hoc: <id>`, `sua lich hoc: <id> <mo ta moi>` — thêm khi test thấy thiếu UX để xem/sửa/xóa lịch học đã tạo. `sua lich hoc` re-parse qua LLM + update task + reschedule reminder | FR-7 gốc chỉ có `lich hoc:` để tạo; không có cách xem hay sửa → UX bỏ ngỏ; phát hiện khi test production | 2026-05-24 |
| 85 | Cải thiện system prompt task_parser cho Vietnamese time formats | Thêm bảng quy đổi buổi → giờ (`10h tối` = 22:00, `chiều` = 15:00, `trưa` = 12:00, v.v.) và ví dụ phong phú vào `_SYSTEM_PROMPT` của `task_parser.py` | Haiku 4.5 không tự suy luận `10h tối` = 22:00 khi prompt chỉ dùng ví dụ `5h chiều mai`; explicit mapping loại bỏ ambiguity | 2026-05-24 |

---

## 7. Open Questions

### Q1 — Web chat history sidebar (giống Claude.ai) — ✅ RESOLVED 2026-05-23
**Quyết định cuối cùng:** Tách thành **FR-5.5** (xem Section 5), làm sau khi FR-5 merge `main`.

**Tóm tắt chốt:**
1. **Storage:** Bảng mới `web_conversations` + `web_messages` (migration 017)
2. **Scope:** Web-only — KHÔNG gộp Telegram (xem Decision #71)
3. **Search:** LIKE đơn giản v1, không FTS5 (Decision #73)
4. **Title:** LLM Haiku 4.5 generate sau message đầu + user rename (Decision #72)
5. **UI:** Sidebar collapsible, mặc định collapsed trên mobile
6. **Retention:** Vĩnh viễn, không recycle bin (Decision #74)
7. **Privacy:** Admin stealth-read user under-18, audit `stealth_read` (Decision #75)
8. **Scope FR:** FR-5.5 riêng, không gộp FR-5 (Decision #70)

Chi tiết scope: xem **Section 5 → FR-5.5**. Decision rationale: xem **Section 6 → #70-#75**.

---

## 8. Current Status & Next Action

### Right now (2026-05-26)
- ✅ **FR-1** merged to `main` (production)
- ✅ **FR-2** merged to `main` (production) 2026-05-18 — 11 commits, 148 tests passing
- ✅ **FR-3** merged to `main` (production) 2026-05-19 — Scope (private/everyone), ACL, L1 Memory, `/help [nhom]`
- ✅ **FR-3.5** merged to `main` 2026-05-21 — Privilege Elevation (sudo); commits `b06e61a`, `f9511eb`
  - `elevation_sessions` + `sudo_attempts` tables; `sudo` / `thoat sudo` / `dat mat khau` commands
  - Role override runtime (không mutate user.role), TTL 15 phút lazy expiry, rate-limit 5 fails → 15p lockout
  - Bổ sung: `doi role` admin command để đổi role user đã tồn tại
- ✅ **FR-4** merged to `main` 2026-05-22 — Audit + Stealth-read + Recycle Bin + Notifications; commits `e76a98c` → `3496f3e`
  - Audit log append-only (trigger chống UPDATE/DELETE), wire sudo events vào table
  - Under-18 stealth-read cho admin, emit audit `stealth_read`
  - Recycle bin admin-only + 180 ngày retention; lệnh `xem thung rac`, `khoi phuc`, `xoa vinh vien`
  - Auto-purge scheduled jobs (APScheduler daily): purge recycle bin >180d + check tuổi 18
  - Notification queue persistent (`pending_notifications` + flush job retry)
  - Refactor `deps.py` (tách `CoreDeps` ra file riêng)
- ✅ **FR-5** merged to `main` 2026-05-23 — Web UI + Password Auth; commits `b80e836` → `2cc9833`; 40 tests passing
  - Migration 016, `web_session_store`, `web_channel` (SSE per user), `web_router` (FastAPI)
  - Templates Jinja2 + HTMX + Alpine.js, glass/dark mode UI, IME-aware Enter-to-send
  - `dat web pass` admin command, force-reset on first login, brute-force lockout
  - `web_deps` instance riêng trên `app.state` với `WebChannelAdapter`
- ✅ **FR-5.5** merged to `main` 2026-05-23 — Web Chat History Sidebar; commits `1c6373d` → `8d3d19f`; 58 tests passing
  - Migration 017: `web_conversations` + `web_messages`; `SqliteWebConversationStore`
  - SSE queue refactor: keyed by `conversation_id` (không phải `user_id`)
  - Sidebar collapsible, conversation list, rename inline, search (LIKE), new chat lazy create
  - LLM title generation async (Haiku 4.5) sau message đầu; SSE `title_update` event
  - Admin stealth-read hội thoại web của user under-18; audit `stealth_read_web_conversation`
- ✅ **FR-6** merged to `main` — Backup / Restore Tooling
  - `backup_engine.py`: `BackupEngine` concrete class; export ZIP in-memory, rate-limit 5 phút; parse/apply import transactional với Drive rollback
  - Web: 6 routes (`/settings/export`, `/settings/export/download`, `/admin/users/{id}/export`, `/admin/import`, `/admin/import/preview`, `/admin/import/apply`); download token redirect (D81); `templates/import.html`
  - Telegram: `xuat du lieu` (self, guard trailing text) + `xuat du lieu: <tên>` (admin); `doi web pass: <mat_khau>` self-service (D82)
  - `tools/local_migrate.py`: CLI standalone migrate SQLite + Drive → local FS
  - Option C: web form `GET/POST /settings/password` + `templates/settings_password.html`; sidebar link trong chat
  - 82 tests passing
- ✅ **FR-7** merged to `main` 2026-05-24 — Tasks + Reminders + Daily Summary + Kids' Study; 867 tests passing
  - Migrations 018–020: `tasks`, `task_reminders`, `users.daily_summary_time/morning_default_time`
  - `task_store.py`, `reminder_store.py`, `reminder_engine.py`, `task_parser.py` (Haiku 4.5 tool-use)
  - Telegram: 10 lệnh task + 3 lệnh lịch học (`danh sach lich hoc`, `sua lich hoc:`, `huy lich hoc:`)
  - Web: CRUD `/tasks` routes + `templates/tasks.html`, `task_form.html`, `task_view.html`
  - Scheduled jobs: `scan_reminders` (1 phút), `send_daily_summary`, `send_parent_digest`
  - `core_handler.py` refactor → 7 cmd_* modules; `WebChannelAdapter.send_with_inline_keyboard` fix
- 🔄 **FR-8** DONE on branch `feature/FR8` — Anniversary / Memorial Reminders; 968 tests passing
  - Migrations 022–023: `anniversaries` (với `year` column) + `anniversary_reminders`
  - `lunar_utils.py`, `anniversary_store.py`, `anniversary_engine.py`, `cmd_anniversary.py`
  - 5 Telegram commands + 7 web routes + 3 templates
  - Scheduled jobs: `anniversary_tick` (60s) + `compute_anniversary_year` (startup + Jan 1)
  - **⚠️ Pending:** migration 024 `ALTER TABLE anniversaries ADD COLUMN year INTEGER` trước khi merge (production DB đã chạy 022 cũ)

---

### 🔔 Next session reminder (cho phiên tiếp theo)

**FR-8 DONE — branch `feature/FR8`, 968 tests passing.**

**Tiếp theo (theo thứ tự ưu tiên):**
1. Tạo `db/migrations/024_add_year_to_anniversaries.sql` (`ALTER TABLE anniversaries ADD COLUMN year INTEGER`) — fix production DB trước khi merge
2. Merge **FR-8** → `main` (và `dev` để test trên staging)
3. Bắt đầu **FR-9** (Expense Tracking / Ledger)

---

### Staging test checklist (feature/FR3 → dev, 2026-05-18)

#### 3.1 Startup & backfill
- [x] Bot phản hồi `/start` → menu mới có nhóm **Tri nho**
- [x] `/help tri nho` → hiện `xem tri nho`, `xem ho so`, `cap nhat tri nho`
- [x] Render log xác nhận: `Note index backfill complete — N rows inserted`

#### 3.2 L1 Memory
- [x] `xem tri nho` khi chưa có data → "Bộ nhớ của bạn chưa có gì" ✅
- [x] `xem ho so` khi chưa có data → "Hồ sơ của bạn chưa có gì" ✅
- [x] `cap nhat tri nho` → bug phát hiện (lưu content rỗng mà báo thành công) → **đã fix**
- [x] `cap nhat tri nho` → **re-test sau khi fix** (chưa test lại trên staging)
- [x] `xem tri nho` sau curation → hiện nội dung snapshot
- [x] `xem ho so` sau curation → hiện nội dung hồ sơ
- [x] Hỏi tự do → bot dùng context memory trong câu trả lời

#### 3.3 Scope — note private
- [x] `ghi nho <nội dung>` → tạo file, scope mặc định = `private`
- [x] (2 user) User B hỏi tự do → không thấy note private của User A

#### 3.4 Scope — chia sẻ / bỏ chia sẻ
- [x] `chia se <tên-file>` → bot xác nhận scope = `everyone`
- [x] `bo chia se <tên-file>` → bot xác nhận scope = `private`
- [x] `chia se` file của người khác → "Bạn không phải chủ file này"

#### 3.5 Wiki scope
- [x] `wiki <nội dung>` → ingest thành công, scope mặc định = `everyone`
- [x] `hoi wiki <câu hỏi>` → trả lời được từ wiki

#### 3.6 Regression — luồng cũ
- [x] `ghi nho vao <file>: <nội dung>` → append OK
- [x] `nhat ky <nội dung>` → journal OK
- [x] `xem nhat ky` → đọc được journal hôm nay
- [x] `tom tat tuan nay` → tóm tắt được

#### 3.7 ACL fix + lệnh mới (bổ sung 2026-05-19)
- [x] (2 user) `xem <file-private-cua-A>` từ user B → "Khong tim thay" (ACL chặn)
- [x] `xem wiki <topic-private>` từ user không phải owner → "Khong tim thay"
- [x] `xem wiki` (liệt kê) → không hiện wiki page private của người khác
- [x] `xem scope <file>` → hiện đúng scope/owner/loại/ngày
- [x] `liet ke` → phân trang đúng, icon 🔒/🌐 đúng scope; `liet ke 2` → trang sau
- [x] `toi la ai` → hiện đúng tên/username/role/id
- [x] `them user: <ten>, <role>` → nhận được invite code (regression sau fix)

> _Checklist trên là lịch sử FR-3 (giữ làm tham khảo). FR-3, FR-3.5, FR-4 đã merge `main` — xem trạng thái mới nhất ở mục "Right now"._

---

### Immediate next steps
1. Tạo `db/migrations/024_add_year_to_anniversaries.sql` — production fix trước khi merge FR-8
2. Merge **FR-8** → `main` (branch `feature/FR8`, 968 tests passing)
3. Bắt đầu **FR-9**: Expense Tracking (Ledger)
   - Branch off từ `main`
   - Lập `docs/FR-9-PLAN.md` chi tiết trước khi code

### Pending FRs
- **FR-9** (next) — sequential theo Section 5

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
