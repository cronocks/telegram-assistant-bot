# System Architecture

> This document describes the architecture of the Telegram Claude Bot as of **FR-2** (Users + Roles + Auth + Quota).
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
| `user_store.py` | `UserStore` — SQLite user registry, quota, parent links |
| `auth.py` | Argon2id password hashing (web auth infrastructure, not yet exposed) |
| `permissions.py` | Role-based permission helpers |
| `text_utils.py` | Vietnamese diacritic normalization, multi-prefix command matcher |
| `timeutils.py` | UTC+7 helpers |
| `cost_monitor.py` | LLM spend tracking, budget alerts |
| `security.py` | Drive folder access control, rate limiting |
| `config.py` | Environment variable loading |
| `db/connection.py` | SQLite connection factory |
| `db/migrations.py` | File-based idempotent migration runner |
| `db/migrations/*.sql` | Plain SQL migration files (001–008) |

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
Core identity table. One row per registered user.

| Column | Type | Notes |
|--------|------|-------|
| `id` | INTEGER PK | Auto-increment |
| `name` | TEXT | Display name |
| `username` | TEXT UNIQUE | Optional handle |
| `role` | TEXT | `admin` \| `manager` \| `member` \| `readonly` |
| `birthdate` | TEXT | ISO date, nullable |
| `monthly_token_limit` | INTEGER | Per-user LLM quota |
| `is_active` | BOOLEAN | Soft-delete flag |
| `created_at` | DATETIME | |

#### `channel_bindings`
Maps a Telegram `chat_id` (or other channel identifier) to a user.

| Column | Type | Notes |
|--------|------|-------|
| `user_id` | INTEGER FK → users | |
| `channel` | TEXT | `telegram` \| `web` \| … |
| `channel_user_id` | TEXT | Telegram `chat_id` |
| PRIMARY KEY | `(channel, channel_user_id)` | |

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
Monthly token usage per user. Auto-resets lazily on first write of a new month.

| Column | Type | Notes |
|--------|------|-------|
| `user_id` | INTEGER FK → users | |
| `month` | TEXT | `YYYY-MM` |
| `used_tokens` | INTEGER | Accumulated this month |

#### `password_hash`
Argon2id password hash for web auth (infrastructure in place; not yet exposed via commands).

| Column | Type | Notes |
|--------|------|-------|
| `user_id` | INTEGER FK → users | |
| `hash` | TEXT | Argon2id hash string |
| `created_at` | DATETIME | |

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
| `/start` | Show available commands |
| `/cost` | Show current LLM spend |
| `/test` | Connectivity test |
| `/security` | Show Drive security status |

### User management (admin only)
| Command | Description |
|---------|-------------|
| `thêm user: <name>, <role>` | Generate invite code for new user |
| `xem danh sách user` | List all registered users |
| `xóa user: <name>` | Soft-delete a user |
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
| L1 | Text files (`MEMORY.md`, `USER.md` per user) | Frozen snapshot; agentic curation | FR-3 |
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
