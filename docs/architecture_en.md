# System Architecture

> This document describes the architecture of the Telegram Claude Bot as of **FR-3.5** (Privilege Elevation / sudo).
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

The system uses a **Modular Monolith** with a hexagonal architecture. All business logic lives in `core_handler.py` and depends only on *Protocols* (interfaces), never on concrete adapters. Concrete adapters are wired in `main.py`.

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

**Key rule:** `core_handler.py` never imports a concrete class. All adapter wiring happens in `main.py`.

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
| `core_handler.py` | Business logic, command dispatch, pending state machine |
| `channel_telegram.py` | `TelegramAdapter` — parses Telegram webhook payloads, sends replies |
| `claude_client.py` | `AnthropicLLM` — wraps Anthropic SDK |
| `drive_client.py` | `DriveNoteStore` — Google Drive notes storage |
| `wiki_client.py` | `DriveWikiStore` — Google Drive wiki, uses LLM via DI |
| `user_store.py` | `UserStore` — SQLite user registry, quota, parent links, password |
| `note_index.py` | `SqliteNoteIndex` — SQLite ACL/index layer mapping Drive file IDs to owner + scope |
| `memory_store.py` | `SqliteMemoryStore` — L1 memory (`memory` and `user` slots per user) |
| `elevation_store.py` | `SqliteElevationStore` — sudo elevation sessions + failed-attempt rate-limit (FR-3.5) |
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
| `db/migrations/*.sql` | Plain SQL migration files (001–013) |

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
| `password_hash` | TEXT | Argon2id hash; NULL until set (web auth — not yet exposed via Telegram) |
| `created_at` | DATETIME | Default `CURRENT_TIMESTAMP` |
| `deleted_at` | DATETIME | Soft-delete marker; NULL = active |

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

**Admin and private data:** in FR-3, admin **does not** read other users' private notes (Decision #52). Stealth-read with audit logging is deferred to FR-4.

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
- Audit goes to stdout: `sudo_elevate`, `sudo_drop`, `sudo_fail`, `sudo_locked`, `password_set` (a formal audit table arrives in FR-4).
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

Feature branches merge **in parallel** into both `dev` (for staging test) and `main` (for production), and are deleted only after landing in `main`.

See [`ROADMAP.md`](ROADMAP.md) Section 3.5 for full git workflow details.
