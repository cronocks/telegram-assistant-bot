# System Architecture

> This document describes the architecture of the Telegram Claude Bot as of **FR-8** (Anniversary / Memorial Reminders).
> For the full feature roadmap, see [`ROADMAP.md`](ROADMAP.md).

---

## 1. Overview

A personal and family **knowledge management system** delivered primarily over Telegram, powered by Anthropic Claude. Designed for a single family (~10 users), not public SaaS.

**Core goals:**
- Note-taking, journal, wiki Q&A via natural language
- Multi-user with roles and parental oversight
- Channel-agnostic core: same business logic works over Telegram today, Web UI or Discord tomorrow
- Self-hostable, low cost (Render free tier + Cloudflare R2 free tier)

---

## 2. Architecture — Hexagonal (Ports & Adapters)

The system uses a **Modular Monolith** with a hexagonal architecture. Business logic is organized across `cmd_*.py` modules and depends only on *Protocols* (interfaces), never on concrete adapters. `core_handler.py` acts as the command dispatcher — routing messages to the correct `cmd_*` handler. Concrete adapters are wired in `main.py`.

```
                  ┌──────────────────────┐
                  │    core_handler.py   │  ← business logic
                  │   handle_message()   │     channel-agnostic
                  └──────────┬───────────┘
                             │ depends only on Protocols (interfaces.py)
              ┌──────────────┼──────────────┐
              ▼              ▼              ▼
         LLMClient      NoteStore      WikiStore
         UserStore      ChannelAdapter
              ▲              ▲              ▲
              │  implemented by             │
              │                             │
       AnthropicLLM    DriveNoteStore  DriveWikiStore
       TelegramAdapter  UserStore(SQLite)
       (future: OllamaLLM, LocalFSNoteStore, DiscordAdapter, WebAdapter)
```

**Key rule:** `cmd_*.py` modules never import a concrete class. All adapter wiring happens in `main.py`.

**Why Modular Monolith (not Microservices):**

| Concern | Microservices | Modular Monolith (chosen) |
|---------|--------------|--------------------------|
| User scale | Thousands | ~10 family members |
| Team size | Multiple teams | 1 developer |
| Free tier | Multiple RAM slots | 1 process |
| Transactions | Distributed complexity | SQLite ACID |
| Debugging | Cross-service tracing | Single stack trace |

---

## 3. File Layout

| File | Role |
|------|------|
| `main.py` | Wiring layer — instantiates adapters, routes webhook, health check |
| `interfaces.py` | Protocols + `ChannelMessage` dataclass — the contract layer |
| `core_handler.py` | Command dispatcher + `/start` + `/help`; routes messages to `cmd_*` handlers (FR-7 refactor) |
| `deps.py` | `CoreDeps` dataclass — collects all dependencies injected into handlers (FR-4 refactor) |
| `cmd_utils.py` | Shared helpers: pending state machine, ACL filter helpers, parsing utilities (FR-7) |
| `cmd_user.py` | User management handlers: `them user`, `xoa user`, `doi role`, `dat birthdate`, `dat cha`, etc. (FR-7) |
| `cmd_audit.py` | Audit + recycle bin handlers: `xem audit`, `xem thung rac`, `khoi phuc`, `xoa han` (FR-7) |
| `cmd_notes.py` | Note/journal handlers: `ghi nho`, `nhat ky`, `xem`, `liet ke`, `tim`, `chia se` (FR-7) |
| `cmd_sudo.py` | Sudo handlers: `sudo`, `thoat sudo`, `dat mat khau`, `dat web pass` (FR-7) |
| `cmd_wiki.py` | Wiki + memory handlers: `wiki`, `hoi wiki`, `xem tri nho`, `cap nhat tri nho` (FR-7) |
| `cmd_task.py` | Task + study schedule handlers + inline keyboard callback dispatcher (FR-7) |
| `anniversary_store.py` | `SqliteAnniversaryStore` — anniversary CRUD + soft-delete + validation (FR-8) |
| `anniversary_engine.py` | `AnniversaryEngine` — `compute_year()`, `tick()`, `cancel_all_for_anniversary()`; fires at 08:00 VN, 12h grace window (FR-8) |
| `lunar_utils.py` | `lunar_to_solar()` + `compute_anniversary_solar_date()`; uses `lunardate==0.2.2` (FR-8) |
| `cmd_anniversary.py` | 5 Telegram handlers: `them ky niem`, `danh sach ky niem`, `ky niem <id>`, `xoa ky niem`, `sua ky niem` (FR-8) |
| `channel_telegram.py` | `TelegramAdapter` — parses Telegram webhook payloads, sends replies, `send_with_inline_keyboard` |
| `claude_client.py` | `AnthropicLLM` — wraps Anthropic SDK |
| `drive_client.py` | `DriveNoteStore` — Google Drive notes storage |
| `wiki_client.py` | `DriveWikiStore` — Google Drive wiki, uses LLM via DI |
| `user_store.py` | `UserStore` — SQLite user registry, quota, parent links, password, task preferences |
| `note_index.py` | `SqliteNoteIndex` — SQLite ACL/index layer mapping Drive file IDs to owner + scope |
| `memory_store.py` | `SqliteMemoryStore` — L1 memory (`memory` and `user` slots per user) |
| `task_store.py` | `SqliteTaskStore` — task CRUD, query by user/status/category, soft-delete (FR-7) |
| `reminder_store.py` | `SqliteReminderStore` — reminder CRUD, ready-to-fire query, cancel by task (FR-7) |
| `reminder_engine.py` | `ReminderEngine` — scan + emit + lazy recurring expansion + parent mirror + grace window (FR-7) |
| `task_parser.py` | `TaskParser` — Haiku 4.5 tool-use; parses free-form Vietnamese → `{title, deadline, recurring_rule}` (FR-7) |
| `elevation_store.py` | `SqliteElevationStore` — sudo elevation sessions + failed-attempt rate-limit (FR-3.5) |
| `audit.py` | `SqliteAuditLog` — append-only audit event writer; `AuditLog` Protocol (FR-4) |
| `notification_store.py` | `SqliteNotificationStore` — persistent notification queue CRUD (FR-4) |
| `notification_service.py` | `NotificationService` — bridges store ↔ `ChannelAdapter`; `enqueue()` + `flush_pending()` (FR-4) |
| `scheduled_jobs.py` | APScheduler jobs: 180d purge, purge-at-18, notification flush, scan_reminders, daily_summary, parent_digest, anniversary_tick, compute_anniversary_year (FR-4, FR-7, FR-8) |
| `web_session_store.py` | `SqliteWebSessionStore` — DB-revocable web sessions (no JWT); find/revoke/create (FR-5) |
| `web_channel.py` | `WebChannelAdapter` — SSE queue per `conversation_id`; `send_with_inline_keyboard` fallback (FR-5, FR-5.5, FR-7) |
| `web_router.py` | FastAPI web router: auth, chat, conversations API, task CRUD, anniversary CRUD routes (FR-5, FR-5.5, FR-7, FR-8) |
| `web_conversation_store.py` | `SqliteWebConversationStore` — conversation + message CRUD; LIKE search; admin stealth-read path (FR-5.5) |
| `backup_engine.py` | `BackupEngine` — in-memory ZIP export, transactional parse/apply import, Drive upload to `Claude-Notes/Backups/`, 5-min/user rate-limit (FR-6) |
| `tools/local_migrate.py` | Standalone CLI: copy SQLite + mirror Drive files → local FS; `--dry-run`, `--users`, `--include-deleted` (FR-6) |
| `templates/` | Jinja2 templates: `login.html`, `setup_password.html`, `chat.html`, `import.html`, `tasks.html`, `task_form.html`, `task_view.html`, `anniversaries.html`, `anniversary_form.html`, `anniversary_view.html` (FR-5 → FR-8) |
| `acl.py` | ACL helpers (`can_read`, `filter_visible`) consumed by retrieval paths |
| `auth.py` | Argon2id password hashing (FR-2 infrastructure; consumed by FR-3.5 to verify sudo password) |
| `permissions.py` | Role-based permission helpers |
| `text_utils.py` | Vietnamese diacritic normalization, multi-prefix command matcher |
| `timeutils.py` | UTC+7 helpers |
| `cost_monitor.py` | LLM spend tracking, budget alerts |
| `security.py` | Drive folder access control, rate limiting |
| `config.py` | Environment variable loading |
| `db/connection.py` | SQLite connection factory |
| `db/migrations.py` | File-based idempotent migration runner |
| `db/migrations/*.sql` | Plain SQL migration files (001–024) |

---

## 4. Tech Stack

| Layer | Technology |
|-------|-----------|
| Runtime | Python 3.11 |
| Web framework | FastAPI + Uvicorn |
| Scheduler | APScheduler |
| HTTP client | httpx (async) |
| LLM | Anthropic Claude (via `AnthropicLLM` adapter) |
| Embeddings | Voyage AI `voyage-3-lite` (planned — L3 vector, future FR) |
| Note / Wiki store | Google Drive |
| User database | SQLite (via `sqlite3` stdlib) |
| DB replication | Litestream → Cloudflare R2 (WAL streaming, ~1s lag) |
| Password hashing | argon2-cffi (Argon2id) |
| Primary channel | Telegram Bot API (webhook mode) |
| Hosting | Render.com (Docker, free tier) |
| Object storage | Cloudflare R2 (S3-compatible, egress-free) |

---

## 5. Data Model

All user data is stored in SQLite. Migrations run automatically on startup via `db/migrations.py`.

### Tables

#### `users`
Core identity table. One row per registered user. Soft-deleted users have `deleted_at` set; a unique index on `name` excludes soft-deleted rows so names can be reused.

| Column | Type | Notes |
|--------|------|-------|
| `id` | INTEGER PK | Auto-increment |
| `name` | TEXT NOT NULL | Display name; unique among active users |
| `username` | TEXT UNIQUE NOCASE | Optional handle; CHECK regex `[A-Za-z0-9_.-]{3,32}` |
| `role` | TEXT NOT NULL | `admin` \| `manager` \| `member` \| `readonly` |
| `birthdate` | DATE | ISO date, nullable |
| `password_hash` | TEXT | Argon2id hash; NULL until set |
| `must_change_password` | INTEGER | 0 = normal; 1 = force-reset on next web login (FR-5) |
| `created_at` | DATETIME | Default `CURRENT_TIMESTAMP` |
| `deleted_at` | DATETIME | Soft-delete marker; NULL = active |
| `daily_summary_time` | TEXT | `NULL` = default 21:00; `'off'` = disabled; `'HH:MM'` = custom *(FR-7)* |
| `morning_default_time` | TEXT | `NULL` = default 09:00; `'HH:MM'` = custom — used when task has no explicit time *(FR-7)* |

#### `channel_bindings`
Maps a Telegram `chat_id` (or other channel identifier) to a user.

| Column | Type | Notes |
|--------|------|-------|
| `user_id` | INTEGER FK → users | |
| `channel` | TEXT | `telegram` \| `web` \| … |
| `chat_id` | TEXT | Channel-side conversation id (e.g. Telegram `chat_id`) |
| PRIMARY KEY | `(channel, chat_id)` | |

#### `invite_codes`
One-time codes issued by admin for new user registration.

| Column | Type | Notes |
|--------|------|-------|
| `code` | TEXT PK | Random hex |
| `role` | TEXT | Role to assign on use |
| `name` | TEXT | Suggested display name |
| `expires_at` | DATETIME | 7-day TTL |
| `used_at` | DATETIME NULL | Set on consumption |

#### `birthdate_changes`
Pending birthdate change requests awaiting admin/manager approval.

| Column | Type | Notes |
|--------|------|-------|
| `user_id` | INTEGER FK → users | |
| `requested_date` | TEXT | ISO date |
| `status` | TEXT | `pending` \| `approved` \| `rejected` |
| `reviewed_by` | INTEGER FK → users | Nullable |

#### `username_changes`
Queued username change requests (first-set is direct; subsequent changes need admin approval + 30-day cooldown).

| Column | Type | Notes |
|--------|------|-------|
| `user_id` | INTEGER FK → users | |
| `requested_username` | TEXT | |
| `status` | TEXT | `pending` \| `approved` \| `rejected` |

#### `parent_links`
Many-to-many parent-child relationships. Supports multi-parent families, divorce scenarios, grandparents as guardians.

| Column | Type | Notes |
|--------|------|-------|
| `parent_user_id` | INTEGER FK → users | |
| `child_user_id` | INTEGER FK → users | |
| `digest_frequency` | TEXT | `daily` \| `weekly` \| `monthly` \| `off` |
| `digest_time` | TEXT | e.g. `21:00`, `SUN 20:00`, `1 20:00` |
| `adult_optin_enabled` | BOOLEAN | Only meaningful when child ≥ 18 |
| PRIMARY KEY | `(parent_user_id, child_user_id)` | |

#### `user_quotas`
Per-user monthly LLM token quota. One row per user; `month` resets lazily on first write of a new month. `monthly_token_limit = 0` means unlimited.

| Column | Type | Notes |
|--------|------|-------|
| `user_id` | INTEGER PK FK → users | One row per user |
| `monthly_token_limit` | INTEGER | 0 = unlimited |
| `used_tokens` | INTEGER | Accumulated this month |
| `month` | TEXT | `YYYY-MM` — used for lazy auto-reset |
| `updated_at` | TEXT | ISO timestamp |

#### `notes` *(FR-3)*
SQLite ACL/index layer for notes and journal files stored on Google Drive. Drive holds content; this table tracks owner + scope for access control.

| Column | Type | Notes |
|--------|------|-------|
| `id` | INTEGER PK | Auto-increment |
| `drive_file_id` | TEXT UNIQUE NOT NULL | Google Drive file ID |
| `owner_user_id` | INTEGER FK → users | |
| `scope` | TEXT NOT NULL | `private` \| `everyone` — default `private` |
| `kind` | TEXT NOT NULL | `note` \| `journal` — default `note` |
| `title` | TEXT | Optional |
| `created_at` / `updated_at` / `deleted_at` | TEXT | ISO timestamps |

Indexes: `(owner_user_id)`, `(scope)`.

#### `wiki_pages` *(FR-3)*
SQLite ACL/index layer for wiki pages stored on Google Drive. Default scope `everyone` — wiki is shared family knowledge by default.

| Column | Type | Notes |
|--------|------|-------|
| `id` | INTEGER PK | Auto-increment |
| `drive_file_id` | TEXT UNIQUE NOT NULL | Google Drive file ID |
| `owner_user_id` | INTEGER FK → users | |
| `scope` | TEXT NOT NULL | `private` \| `everyone` — default `everyone` |
| `topic` | TEXT NOT NULL | Human-readable topic name |
| `slug` | TEXT NOT NULL | Filesystem-safe identifier |
| `created_at` / `updated_at` / `deleted_at` | TEXT | ISO timestamps |

Indexes: `(owner_user_id)`, `(scope)`, `(slug)`.

#### `user_memory` *(FR-3)*
L1 memory store. Two named slots per user, populated by LLM curation on demand.

| Column | Type | Notes |
|--------|------|-------|
| `user_id` | INTEGER FK → users | |
| `kind` | TEXT NOT NULL | `memory` (rolling facts) \| `user` (stable profile) |
| `content` | TEXT NOT NULL | Default empty string |
| `updated_at` | TEXT | ISO timestamp |
| `curated_at` | TEXT | Timestamp of last LLM curation; NULL = never curated |
| PRIMARY KEY | `(user_id, kind)` | |

#### `elevation_sessions` *(FR-3.5)*
Sudo elevation sessions, one row per `(channel, chat_id)`. Re-elevating refreshes `expires_at`. Expiry handled lazily (`get_active_session` only returns rows still within TTL).

| Column | Type | Notes |
|--------|------|-------|
| `channel` | TEXT NOT NULL | `telegram` \| `web` \| … |
| `chat_id` | TEXT NOT NULL | Channel-side conversation identifier |
| `base_user_id` | INTEGER FK → users | The real user behind the session (a manager); identity is NOT swapped |
| `started_at` | DATETIME | Default `CURRENT_TIMESTAMP` |
| `expires_at` | DATETIME NOT NULL | 15 minutes after elevation |
| PRIMARY KEY | `(channel, chat_id)` | |

#### `sudo_attempts` *(FR-3.5)*
Per-chat failed-password counter; locks after threshold. Reset on a successful sudo.

| Column | Type | Notes |
|--------|------|-------|
| `channel` | TEXT NOT NULL | |
| `chat_id` | TEXT NOT NULL | |
| `failed_count` | INTEGER | Default 0; ≥ `SUDO_MAX_FAILS` (5) → sets `locked_until` |
| `locked_until` | DATETIME NULL | Lockout end timestamp (15 minutes after the last fail) |
| `last_attempt_at` | DATETIME NULL | |
| PRIMARY KEY | `(channel, chat_id)` | |

#### `audit_log` *(FR-4)*
Append-only table recording every event with legal or administrative significance. INSERT only — never UPDATE or DELETE. `actor_user_id` is nullable for system events (scheduled jobs, etc.).

| Column | Type | Notes |
|--------|------|-------|
| `id` | INTEGER PK | Auto-increment |
| `actor_user_id` | INTEGER FK → users | NULL = system event (scheduled job) |
| `action` | TEXT NOT NULL | Event name (see taxonomy in Section 6) |
| `target_type` | TEXT | `note` \| `wiki_page` \| `user` \| `notification` \| NULL |
| `target_id` | TEXT | Drive file ID or integer id; TEXT for flexibility |
| `payload` | TEXT | JSON string; NULL if no additional metadata |
| `created_at` | DATETIME | Default `CURRENT_TIMESTAMP` |

Indexes: `(actor_user_id, created_at DESC)`, `(target_type, target_id, created_at DESC)`, `(action, created_at DESC)`.

#### `pending_notifications` *(FR-4)*
Persistent notification queue. Survives restarts (no in-memory queue). The `flush_pending_notifications` job reads this table every 30 seconds.

| Column | Type | Notes |
|--------|------|-------|
| `id` | INTEGER PK | Auto-increment |
| `user_id` | INTEGER FK → users | Recipient |
| `channel` | TEXT NOT NULL | `telegram` \| `web` \| … |
| `payload` | TEXT NOT NULL | JSON: `{kind, text, extra}` — shape defined by service |
| `status` | TEXT | `pending` \| `delivered` \| `failed` — default `pending` |
| `attempts` | INTEGER | Default 0; ≥ 5 → `failed` |
| `last_error` | TEXT | Truncated error message (max 500 chars) |
| `next_retry_at` | DATETIME | NULL = ready immediately; set during backoff |
| `created_at` / `delivered_at` | DATETIME | Timestamps |

Partial index: `(status, next_retry_at) WHERE status = 'pending'` — retry job scans only pending rows.

#### `web_sessions` *(FR-5)*
Server-side DB-revocable web sessions. One row per login; logout sets `revoked_at`. The cookie holds a 32-byte hex opaque token (256-bit entropy) — JWT is avoided so sessions can be force-invalidated immediately.

| Column | Type | Notes |
|--------|------|-------|
| `id` | INTEGER PK | Auto-increment |
| `user_id` | INTEGER FK → users | |
| `token` | TEXT UNIQUE NOT NULL | 32-byte random hex |
| `created_at` | DATETIME | Default `CURRENT_TIMESTAMP` |
| `expires_at` | DATETIME NOT NULL | `created_at + WEB_SESSION_TTL_DAYS` (default 7 days) |
| `revoked_at` | DATETIME | NULL = active; set on logout or password change |

Indexes: `(token)`, `(user_id)`.

#### `web_conversations` *(FR-5.5)*
Each web chat session is one conversation. Created lazily when the user sends the first message — opening "New chat" and navigating away does not pollute the DB.

| Column | Type | Notes |
|--------|------|-------|
| `id` | INTEGER PK | Auto-increment |
| `user_id` | INTEGER FK → users | Conversation owner |
| `title` | TEXT | NULL until LLM generates; frontend shows "New chat" |
| `created_at` | DATETIME | Default `CURRENT_TIMESTAMP` |
| `updated_at` | DATETIME | Bumped on every new message |

Index: `(user_id, updated_at DESC)`.

#### `web_messages` *(FR-5.5)*
Each chat turn (user or bot) is one row. Retained indefinitely — no auto-purge (Decision #74).

| Column | Type | Notes |
|--------|------|-------|
| `id` | INTEGER PK | Auto-increment |
| `conversation_id` | INTEGER FK → web_conversations | |
| `role` | TEXT NOT NULL | `user` \| `bot` |
| `text` | TEXT NOT NULL | Message content |
| `created_at` | DATETIME | Default `CURRENT_TIMESTAMP` |

Indexes: `(conversation_id, created_at)`, `(conversation_id, text)` — text index supports LIKE search.

#### `tasks` *(FR-7)*
Task CRUD. Category `study` covers recurring children's study schedules. Soft-delete via `deleted_at`.

| Column | Type | Notes |
|--------|------|-------|
| `id` | INTEGER PK | Auto-increment |
| `user_id` | INTEGER FK → users | |
| `title` | TEXT NOT NULL | Short task title |
| `description` | TEXT | Optional detail |
| `deadline` | TEXT NOT NULL | ISO datetime +07:00 |
| `category` | TEXT | `task` \| `study` \| `reminder` — default `task` |
| `scope` | TEXT | `private` (v1 only) |
| `recurring_rule` | TEXT | NULL = one-shot; e.g. `weekly:MON,WED@07:00` or `daily@21:00` |
| `reminder_offsets` | TEXT | CSV seconds: default `7200,3600,1800,900` (2h/1h/30m/15m) |
| `status` | TEXT | `pending` \| `completed` \| `cancelled` |
| `completed_at` | TEXT | ISO datetime; NULL if not done |
| `snooze_count` | INTEGER | Number of times snoozed |
| `source` | TEXT | `telegram` \| `web` |
| `created_at` / `updated_at` / `deleted_at` | TEXT | ISO timestamps |

Indexes: `(user_id, status)`, `(deadline)` WHERE pending, `(recurring_rule)` WHERE not null.

#### `task_reminders` *(FR-7)*
One row per reminder fire point per task. When a recurring task's reminder fires, the engine computes the next occurrence and inserts new rows (lazy expansion).

| Column | Type | Notes |
|--------|------|-------|
| `id` | INTEGER PK | Auto-increment |
| `task_id` | INTEGER FK → tasks | |
| `fire_at` | TEXT NOT NULL | ISO datetime +07:00 — when to fire |
| `offset_seconds` | INTEGER | Distance from deadline (e.g. 7200 = 2h before) |
| `kind` | TEXT | `scheduled` \| `snoozed` |
| `status` | TEXT | `pending` \| `fired` \| `missed` \| `cancelled` |
| `fired_at` | TEXT | ISO datetime; NULL if not yet fired |
| `created_at` | TEXT | ISO timestamp |

Partial index: `(fire_at, status) WHERE status = 'pending'` — `scan_reminders` job scans only pending rows.

#### `anniversaries` *(FR-8)*
Annual recurring events: memorials (giỗ), wedding anniversaries, and other yearly dates. Stores the original lunar/solar month-day; the solar date is recomputed each year at runtime (Decision #47).

| Column | Type | Notes |
|--------|------|-------|
| `id` | INTEGER PK | Auto-increment |
| `user_id` | INTEGER FK → users | |
| `name` | TEXT NOT NULL | Event name, e.g. "Giỗ ông nội" |
| `date_type` | TEXT NOT NULL | `lunar` \| `solar` |
| `month` | INTEGER NOT NULL | 1–12 |
| `day` | INTEGER NOT NULL | 1–30 (lunar) or 1–31 (solar) |
| `year` | INTEGER | Original year of the event (optional) |
| `category` | TEXT NOT NULL | `gio` \| `cuoi` \| `khac` — default `khac` |
| `is_leap_month` | INTEGER NOT NULL | 1 = lunar leap month — default 0 |
| `reminder_offsets` | TEXT NOT NULL | CSV days before: default `30,15,7,3,1,0` |
| `enabled` | INTEGER NOT NULL | 1 = active; 0 = paused |
| `note` | TEXT | Optional free-form note |
| `created_at` / `updated_at` / `deleted_at` | TEXT | ISO timestamps |

Indexes: `(user_id)` WHERE deleted_at IS NULL, `(enabled)` WHERE enabled=1 AND deleted_at IS NULL.

#### `anniversary_reminders` *(FR-8)*
One row per reminder fire point per anniversary per year. UNIQUE constraint ensures the annual compute job is idempotent.

| Column | Type | Notes |
|--------|------|-------|
| `id` | INTEGER PK | Auto-increment |
| `anniversary_id` | INTEGER FK → anniversaries | |
| `year` | INTEGER NOT NULL | Solar year of this reminder instance |
| `fire_at` | TEXT NOT NULL | ISO datetime at 08:00 VN |
| `offset_days` | INTEGER NOT NULL | Days before the anniversary date (0 = on the day) |
| `status` | TEXT NOT NULL | `pending` \| `fired` \| `missed` \| `cancelled` — default `pending` |
| `fired_at` | TEXT | ISO datetime; NULL if not yet fired |
| `created_at` | TEXT NOT NULL | ISO timestamp |
| UNIQUE | `(anniversary_id, year, offset_days)` | Idempotent — compute job can run repeatedly |

---

## 6. Permission Model

### Roles

| Role | Who | Capabilities |
|------|-----|-------------|
| `admin` | Primary parent | Full control; read private data of under-18 users; recycle bin access |
| `manager` | Grandparent, senior family member | Approve birthdate changes; supervision; cannot read private data |
| `member` | Children, regular members | Read/write own data; group/everyone-scoped content |
| `readonly` | Guest | Read everyone-scoped content only |

### Parent-child relationship
Configured via `parent_links` table. Supports:
- Real-time reminder mirroring to parent (always on, non-configurable — core value)
- Activity digest (configurable frequency: daily / weekly / monthly / off)
- Auto privacy cutoff at age 18 (enforced at runtime, DB not mutated)
- Adult opt-in: child ≥ 18 can voluntarily re-enable sharing (`chia sẻ với cha mẹ: bật`)

### Scope model *(FR-3)*

Every note and wiki page has a `scope` column in the SQLite ACL layer (`notes`, `wiki_pages`). Drive holds the content; the SQLite row decides who can read it.

| Scope | Visible to |
|-------|-----------|
| `private` | Owner only |
| `everyone` | All active users |

**Defaults on create:**
- `ghi nhớ <text>` / `ghi nhớ vào <file>` → `private`
- `nhật ký <text>` → `private`
- `wiki <text>` → `everyone`

**Ownership change:** `chia sẻ <file>` / `bỏ chia sẻ <file>` (owner only). Non-owners get *"Bạn không phải chủ file này"*.

**ACL enforcement points:** all retrieval paths filter through `acl.can_read` / `acl.filter_visible` — `smart_search`, `get_recent_notes`, `get_current_week_notes`, wiki `retrieve_pages`, and the direct `xem` / `xem wiki` / `liệt kê` commands.

**Admin and private data (FR-4):** admin **can read** `private` notes/wiki belonging to users who are children under 18. Conditions: `reader.role == 'admin'` AND owner has a `parent_links` relationship (is someone's child) AND `age(owner) < 18`. Every read emits an audit row (`stealth_read_note` / `stealth_read_wiki`); the owner receives no notification. When the child turns 18, stealth-read is automatically disabled at runtime (DB is not mutated).

### Recycle Bin *(FR-4)*

Soft-delete has been present since earlier FRs via `deleted_at` on `notes`, `wiki_pages`, and `users`. FR-4 adds admin commands to view, restore, and permanently delete items.

| Command | Behavior |
|---------|----------|
| `xem thung rac` | Lists all items with `deleted_at IS NOT NULL` (notes, wiki pages, users), sorted by `deleted_at` descending. Emits audit `recycle_view`. |
| `khoi phuc: <kind> <id>` | Clears `deleted_at`. Example: `khoi phuc: note 12`. Emits audit `recycle_restore`. |
| `xoa han: <kind> <id>` | Hard deletes immediately, bypassing 180-day retention. For notes/wiki: also removes the Drive file (best-effort). Emits audit `recycle_purge`. |

**Scheduled jobs (run at 03:00 UTC+7 daily):**
- `purge_recycle_bin_180d`: permanently deletes all items with `deleted_at < now − 180 days`.
- `purge_children_turning_18`: when a user turned 18 yesterday, purges all their soft-deleted notes/wiki. Live data is untouched.

### Audit Log Taxonomy *(FR-4)*

| `action` | `target_type` | When |
|---|---|---|
| `stealth_read_note` | `note` | Admin reads a private note belonging to a child <18 |
| `stealth_read_wiki` | `wiki_page` | Admin reads a private wiki page belonging to a child <18 |
| `recycle_view` | — | Admin runs `xem thung rac` |
| `recycle_restore` | `note` / `wiki_page` / `user` | Admin restores an item |
| `recycle_purge` | `note` / `wiki_page` / `user` | Hard delete (manual or auto 180d) |
| `auto_purge_18` | `user` | Daily job detected a user who just turned 18 |
| `sudo_elevate` / `sudo_drop` / `sudo_fail` / `sudo_locked` | — | Sudo events (migrated from stdout in FR-3.5) |
| `password_set` | `user` | Admin password set or changed |
| `role_change` | `user` | Admin changes a user's role |
| `scope_change` | `note` / `wiki_page` | `chia se` / `bo chia se` |
| `notification_enqueued` | `notification` | Notification added to queue |
| `notification_delivered` | `notification` | Successfully sent |
| `notification_retry` | `notification` | Intermediate retry (attempts < 5) |
| `notification_failed` | `notification` | Reached max 5 attempts — no further retries |
| `web_login` | `user` | Successful web login *(FR-5)* |
| `web_logout` | `user` | Web logout *(FR-5)* |
| `web_login_failed` | `user` | Failed web login — wrong password *(FR-5)* |
| `web_password_set` | `user` | Admin sets web password for a user *(FR-5)* |
| `web_conversation_created` | `web_conversation` | Lazy create on first message *(FR-5.5)* |
| `web_conversation_renamed` | `web_conversation` | User renames a conversation *(FR-5.5)* |
| `stealth_read_web_conversation` | `web_conversation` | Admin views web conversation of an under-18 user *(FR-5.5)* |
| `task_created` | `task` | New task created *(FR-7)* |
| `task_updated` | `task` | Task edited *(FR-7)* |
| `task_completed` | `task` | User marks task done *(FR-7)* |
| `task_deleted` | `task` | Task soft-deleted *(FR-7)* |
| `task_snoozed` | `task` | User snoozes a reminder *(FR-7)* |
| `reminder_fired` | `task` | Reminder delivered successfully *(FR-7)* |
| `reminder_missed` | `task` | Reminder past 1h grace — skipped *(FR-7)* |
| `daily_summary_sent` | `user` | Daily summary sent at end of day *(FR-7)* |
| `parent_digest_sent` | `user` | Parent digest sent at configured frequency *(FR-7)* |
| `anniversary_created` | `anniversary` | Anniversary created (Telegram or web) *(FR-8)* |
| `anniversary_updated` | `anniversary` | Anniversary edited *(FR-8)* |
| `anniversary_deleted` | `anniversary` | Anniversary soft-deleted *(FR-8)* |
| `anniversary_reminder_fired` | `anniversary` | Anniversary reminder delivered *(FR-8)* |
| `anniversary_reminder_missed` | `anniversary` | Anniversary reminder past 12h grace — skipped *(FR-8)* |

### Notification Framework *(FR-4)*

Minimal plumbing allowing any module to enqueue notifications delivered via `ChannelAdapter`, with persistent retry/backoff in SQLite.

- **`enqueue(user_id, channel, payload)`** — writes to DB and emits audit `notification_enqueued`. Does not send immediately; non-blocking for the caller.
- **`flush_pending()`** — called by scheduler every 30 seconds; reads the queue, sends via adapter:
  - Success → `status='delivered'`, audit `notification_delivered`.
  - Failure with `attempts < 5` → increments `attempts`, sets `next_retry_at = now + 2^attempts minutes`, audit `notification_retry`.
  - Failure with `attempts >= 5` → `status='failed'`, audit `notification_failed`.
- Payload schema: `{"kind": "text", "text": "...", "extra": {...}}`. FR-7 will define additional kinds (`reminder`, `digest`, …).
- Observability: `xem audit` surfaces the full trace — enqueue → retry × N → delivered/failed — in chronological order.

### Privilege Elevation — sudo *(FR-3.5)*

Production does NOT use admin as the default account. The primary account runs as `manager`; admin power is acquired by **temporary elevation** when needed.

| Concept | Description |
|---------|-------------|
| **Natively-admin** | User with `role='admin'` in DB, bound directly to a chat_id. No elevation session involved. |
| **Elevated-admin** | A `manager` user with a still-valid elevation session — their `role` is overridden to `admin` at resolution time. |

**Mechanics:**
- After `find_by_channel`, `main.py` checks `elevation_store.get_active_session()`. If a valid session exists → `dataclasses.replace(user, role="admin")`. Identity (`id`, `name`) is **not swapped** — audit always records the real user (Decision #57).
- TTL 15 minutes (`SUDO_TTL_MINUTES`), lazy expiry — no cron required.
- Defense in depth: `sudo` is gated to role `manager`; verifies Argon2id against the hash of any user with role `admin`; rate-limited to 5 fails → 15-minute lockout.
- The bot deletes messages containing passwords via `delete_message` on `ChannelAdapter` (implemented through Telegram's `deleteMessage` API).
- Audit table: `sudo_elevate`, `sudo_drop`, `sudo_fail`, `sudo_locked`, `password_set` are written to `audit_log` (migrated from stdout in FR-3.5 to FR-4).
- `dat mat khau` is restricted to **natively-admin** accounts — it covers both initial setup and password recovery (no separate "forgot password" flow — Decision #59).

### Command access by role

| Command | admin | manager | member | readonly |
|---------|-------|---------|--------|----------|
| Add / delete user | ✅ | ❌ | ❌ | ❌ |
| Set quota | ✅ | ❌ | ❌ | ❌ |
| Approve birthdate | ✅ | ✅ | ❌ | ❌ |
| Approve username | ✅ | ❌ | ❌ | ❌ |
| Set parent link | ✅ | ❌ | ❌ | ❌ |
| Notes / journal / wiki | ✅ | ✅ | ✅ | read-only |
| Recycle bin (view / restore / hard delete) | ✅ | ❌ | ❌ | ❌ |
| View audit log | ✅ | ❌ | ❌ | ❌ |

---

## 7. Command Reference

Commands are matched via a diacritic-agnostic prefix matcher — both accented (`ghi nhớ`) and unaccented (`ghi nho`) forms work.

### Slash commands
| Command | Description |
|---------|-------------|
| `/start` | Overview of command groups |
| `/help [nhóm]` | Detail for a group (e.g. `/help tri nho`, `/help wiki`) |
| `/cost` | Show current LLM spend |
| `/test` | Connectivity test |
| `/security` | Show Drive security status |

### User management (admin only)
| Command | Description |
|---------|-------------|
| `thêm user: <name>, <role>` | Generate invite code for new user |
| `xem danh sách user` | List all registered users |
| `xóa user: <name>` | Soft-delete a user |
| `đổi role: <name/id> <new role>` | Change an existing user's role (safety guard: admin cannot self-demote) |
| `đặt quota: <name>, <tokens>` | Set monthly token limit |
| `reset quota: <name>` | Reset current month usage |
| `đặt cha: <parent>, <child>` | Create parent-child link |

### Profile commands
| Command | Description |
|---------|-------------|
| `đặt username: <handle>` | Set / request username change |
| `đặt birthdate: <date>` | Request birthdate change (needs approval) |
| `duyệt username` | Approve pending username change (admin) |
| `duyệt birthdate` | Approve pending birthdate change (admin/manager) |
| `xem cha: <name>` | View parent links for a user |
| `xem quota` | View own quota usage |
| `tôi là ai` | Show own identity (name, username, role, id) |

### Notes & journal
| Command | Description |
|---------|-------------|
| `ghi nhớ <text>` | Save a note |
| `ghi nhớ vào <title>: <text>` | Save note to specific file |
| `nhật ký <text>` | Append to today's journal |
| `xem nhật ký` | Read journal entries |
| `liệt kê` | List recent notes |
| `tìm <query>` | Fuzzy search notes |
| `xem <title>` | Read a specific note |

### Wiki
| Command | Description |
|---------|-------------|
| `wiki <content>` | Ingest content into wiki |
| `hỏi wiki <question>` | Q&A against wiki (LLM-powered) |
| `xem wiki` | List wiki pages |
| `xem wiki <page>` | Read a specific wiki page |

### Scope & sharing *(FR-3)*
| Command | Description |
|---------|-------------|
| `chia sẻ <file>` | Set scope to `everyone` (owner only) |
| `bỏ chia sẻ <file>` | Set scope back to `private` (owner only) |
| `xem scope <file>` | Show scope, owner, kind, timestamps for a file |

### L1 Memory *(FR-3)*
| Command | Description |
|---------|-------------|
| `xem trí nhớ` | Read own `memory` snapshot (rolling facts) |
| `xem hồ sơ` | Read own `user` snapshot (stable profile) |
| `cập nhật trí nhớ` | Trigger LLM curation pass over recent notes |

### Privilege Elevation *(FR-3.5)*
| Command | Description |
|---------|-------------|
| `sudo: <password>` | Elevate `manager` to `admin` for 15 minutes (the bot deletes the message containing the password) |
| `thoát sudo` | Drop elevation immediately |
| `đặt mật khẩu: <password>` | Set/change the admin password — natively-admin accounts only (also the recovery mechanism) |

### Audit & Administration *(FR-4)*
| Command | Description | Who |
|---------|-------------|-----|
| `xem audit` | List the 50 most recent audit events | admin |
| `xem audit <action>` | Filter by event type (e.g. `xem audit sudo_elevate`) | admin |
| `xem thung rac` | List items currently in the recycle bin (soft-deleted) | admin |
| `khoi phuc: <kind> <id>` | Restore an item (e.g. `khoi phuc: note 12`) | admin |
| `xoa han: <kind> <id>` | Hard delete immediately, bypassing 180-day retention | admin |

### Web UI — administration *(FR-5)*
| Telegram Command | Description | Who |
|-----------------|-------------|-----|
| `dat web pass: <username>, <password>` | Set web password for a user + `must_change_password=1` → user is forced to reset on first login | admin |

**Web flows (via browser):**
- `/login` — log in; sets `web_session` cookie HttpOnly + SameSite=Lax + Secure
- `/setup-password` — force-reset password when `must_change_password=1`
- `/logout` — server-side session revocation, immediate
- Brute-force: 5 failures → 15-minute lockout (reuses `sudo_attempts` table with `channel="web"`)

### Web Chat History *(FR-5.5)*
**Routes:**

| Method | Path | Description |
|--------|------|-------------|
| GET | `/chat` | New chat lazy; render sidebar + empty messages |
| GET | `/chat/<id>` | Open a specific conversation |
| POST | `/chat/send` | Send message with no existing conversation (lazy create) |
| POST | `/chat/<id>/send` | Send message into an existing conversation |
| GET | `/chat/stream?conversation_id=<id>` | SSE stream per conversation |
| GET | `/api/conversations` | JSON list of user's conversations |
| GET | `/api/conversations/<id>/messages` | JSON messages for a conversation |
| PATCH | `/api/conversations/<id>` | Rename conversation |
| GET | `/api/conversations/search?q=...` | LIKE search across messages |
| GET | `/admin/users/<id>/conversations` | Admin views conversations of an under-18 user |
| GET | `/admin/conversations/<id>` | Admin views messages (emits audit `stealth_read_web_conversation`) |

**Sidebar features:**
- Collapsible (collapsed by default on mobile)
- Inline rename (double-click title → editable input → Enter/blur to save)
- Search box with 300ms debounce
- New chat button — lazy conversation create on first message

### Anniversaries & Reminders *(FR-8)*
| Command | Description |
|---------|-------------|
| `them ky niem: <name>, âm/dương DD/MM[, <category>]` | Add anniversary. Category: gio / cuoi / khac |
| `danh sach ky niem` | List all user anniversaries |
| `ky niem <id>` | View anniversary detail |
| `xoa ky niem: <id>` | Soft-delete + cancel pending reminders |
| `sua ky niem: <id>, ten=…, ngay=âm/dương DD/MM, loai=…, nhac=<csv>, bat/tat` | Edit anniversary |

**Web routes *(FR-8)*:**

| Method | Path | Description |
|--------|------|-------------|
| GET | `/anniversaries` | List user anniversaries |
| GET | `/anniversaries/new` | Create form |
| POST | `/anniversaries` | Create — redirect to detail |
| GET | `/anniversaries/{id}` | Detail view |
| GET | `/anniversaries/{id}/edit` | Edit form |
| POST | `/anniversaries/{id}` | Update |
| POST | `/anniversaries/{id}/delete` | Soft-delete |

### Registration (pre-auth)
| Command | Description |
|---------|-------------|
| `đăng ký: <code>` | Register using an invite code |

### Other
| Command | Description |
|---------|-------------|
| `tóm tắt tuần này` | Weekly activity summary |
| Free-form text | Handled by agentic LLM loop |

---

## 8. Memory Architecture (Vision)

Inspired by NousResearch Hermes Agent. Three tiers, built progressively across FRs:

| Tier | Storage | Description | Status |
|------|---------|-------------|--------|
| L1 | SQLite (`user_memory` table, kinds: `memory` \| `user`) | Frozen snapshot; LLM curation on demand (`cập nhật trí nhớ`); injected into Q&A context | FR-3 ✅ |
| L2 | Graph DB (Memgraph/Neo4j embedded) | Entity relationships; passive | Future |
| L3 | Vector store (sqlite-vss or Qdrant) | Semantic search via Voyage AI embeddings | Future |

---

## 9. Persistence & Deployment

```
┌─────────────────────────────┐
│   Render.com (Docker)        │
│                              │
│  ┌────────────────────────┐ │
│  │  docker-entrypoint.sh  │ │
│  │  1. litestream restore  │ │  ← restores SQLite from R2 on every boot
│  │  2. litestream replicate│ │  ← streams WAL to R2 continuously (~1s)
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

Since Render free tier uses an **ephemeral filesystem**, SQLite data would be lost on every restart without Litestream. Litestream streams the SQLite WAL to R2 (~1 second lag) and restores from R2 on boot.

---

## 10. Git Workflow

| Branch | Purpose |
|--------|---------|
| `main` | Production — every commit is verified on staging |
| `dev` | Staging integration buffer — never use as feature base |
| `feature/*` | Feature branches — always branch off `main` |

Feature branches merge **sequentially**: feature → `dev` first (staging test) → confirm no errors → feature → `main` (production). Deleted only after landing in `main`.

See [`ROADMAP.md`](ROADMAP.md) Section 3.5 for full git workflow details.
