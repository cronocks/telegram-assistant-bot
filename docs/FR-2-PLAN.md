# FR-2 — Detailed Implementation Plan

> **Status:** APPROVED, ready to execute on branch `feature/FR2`
> **Created:** 2026-05-12
> **Mode:** Local-only development (Phương án 5) — không deploy Render trong FR-2

---

## 1. Goal

Chuyển hệ thống từ **single-user** (chat_id allowlist hardcode) sang **multi-user** với SQLite-backed user registry, roles, parent-child relationships, per-user quota, birthdate flow, username (login identifier), và password infra (chuẩn bị cho FR-5, chưa expose UI).

Đây là **foundation** cho mọi tính năng đa người dùng sau này (FR-3 scope, FR-4 audit, FR-5 web UI, FR-7 tasks/reminders, FR-9 expense per-user).

---

## 2. Context & Decisions

### 2.1 Mode: Local-only (Phương án 5)

- Code và test trên máy local. Không deploy lên Render staging/production trong FR-2.
- Lý do: Render free tier không hỗ trợ Persistent Disk → SQLite data sẽ mất mỗi lần redeploy.
- Khi FR-2 code xong, quyết định persist strategy (Litestream / Drive / paid plan) ở pre-FR-3.
- Bot production hiện tại (single-user) vẫn chạy bình thường trên `main` branch, không bị FR-2 ảnh hưởng cho đến khi merge.

### 2.2 Tests included

- Project chưa có `tests/` directory → tạo mới trong FR-2.
- Sử dụng `pytest`. Coverage cho: `user_store`, `permissions`, `auth`, `text_utils` (normalize), `cost_monitor` (per-user).
- Add `pytest`, `pytest-asyncio` vào `requirements-dev.txt`.

### 2.3 Cross-machine workflow

- Cuối mỗi day session: commit + push lên `feature/FR2` để máy còn lại pull về làm tiếp.
- Đầu mỗi day session: `git pull origin feature/FR2` trước khi code.

### 2.4 Fast-path matching

- Thêm `text_utils.normalize_vn(text)` — strip Vietnamese diacritics + lowercase.
- Áp dụng cho fast-path prefix match.
- Mỗi command có **list prefixes** (Vietnamese + optional English alias).
- Match theo longest-prefix-first để tránh ambiguity.

---

## 3. Scope Breakdown (sub-deliverables)

| Sub | Tên | Mục đích |
|-----|-----|---------|
| 2.0 | SQLite infrastructure | Connection, migration runner, env var |
| 2.1 | Users + bootstrap admin | `users` table + UserStore adapter |
| 2.2 | Channel bindings + registration | Invite-code flow qua Telegram |
| 2.3 | Roles + permissions middleware | Admin / manager / member / readonly |
| 2.4 | Birthdate + manager approval | `birthdate_changes` table + flow |
| 2.5 | Parent-child links + digest config | `parent_links` table + commands |
| 2.6 | Per-user quota | Sửa `cost_monitor` từ family-level → per-user |
| 2.7 | Argon2id password hash | Chuẩn bị cho FR-5 (web auth), chưa expose |
| 2.8 | Username + change flow | `username` column + `username_changes` queue (admin approval, rate-limit 30 ngày) |
| 2.9 | Documentation update | ROADMAP Section 8 + Decision Log |

---

## 4. File Changes Summary

### 4.1 New files

| # | File | Purpose |
|---|------|---------|
| 1 | `db/__init__.py` | Package init |
| 2 | `db/connection.py` | SQLite connection singleton, WAL mode |
| 3 | `db/migrations.py` | File-based migration runner, idempotent |
| 4 | `db/migrations/001_initial.sql` | Migrations metadata table |
| 5 | `db/migrations/002_users.sql` | `users` table |
| 6 | `db/migrations/003_channel_bindings.sql` | `channel_bindings` + `invite_codes` |
| 7 | `db/migrations/004_birthdate_changes.sql` | `birthdate_changes` |
| 8 | `db/migrations/005_username_changes.sql` | `username_changes` (approval queue) |
| 9 | `db/migrations/006_parent_links.sql` | `parent_links` |
| 10 | `db/migrations/007_quota.sql` | `user_quotas` |
| 11 | `db/migrations/008_password.sql` | Add password columns vào `users` |
| 12 | `user_store.py` | `SqliteUserStore` — CRUD users + bindings + invites + username |
| 13 | `permissions.py` | Role-based ACL helpers + decorators |
| 14 | `auth.py` | argon2id hash/verify + token generation |
| 15 | `text_utils.py` | `normalize_vn()` + prefix matcher + `validate_username()` + reserved-name list |
| 16 | `tests/__init__.py` | Tests package init |
| 17 | `tests/conftest.py` | Pytest fixtures (in-memory SQLite, sample users) |
| 18 | `tests/test_user_store.py` | UserStore CRUD tests |
| 19 | `tests/test_permissions.py` | Role enforcement tests |
| 20 | `tests/test_auth.py` | Argon2 hash/verify tests |
| 21 | `tests/test_text_utils.py` | Normalize + matcher tests |
| 22 | `tests/test_cost_monitor.py` | Per-user quota tests |
| 23 | `tests/test_username.py` | Username validator + change flow + rate-limit tests |
| 24 | `requirements-dev.txt` | Dev dependencies (pytest, pytest-asyncio) |

### 4.2 Edited files

| # | File | Change |
|---|------|--------|
| 25 | `interfaces.py` | Thêm `User` dataclass (gồm field `username`), `UserStore` Protocol, `AuthClient` Protocol |
| 26 | `main.py` | DB init + migrations on startup; bootstrap admin; CoreDeps update |
| 27 | `core_handler.py` | Replace chat_id check với user lookup; thêm 12 commands mới (gồm `dat username` / `duyet username`); pass `User` xuống mọi `_cmd_*` handler |
| 28 | `channel_telegram.py` | Bỏ `is_authorized()` (logic chuyển sang core) |
| 29 | `cost_monitor.py` | Track theo `user_id`; check quota trước LLM call |
| 30 | `config.py` | Thêm `SQLITE_PATH` env var (default `./bot.db`) |
| 31 | `requirements.txt` | Thêm `argon2-cffi` |
| 32 | `docs/ROADMAP.md` | Update Section 8 status; add Decision Log entries phát sinh |

**Total: 32 file changes.**

---

## 5. Database Schema

### 5.1 `001_initial.sql`

```sql
CREATE TABLE IF NOT EXISTS _schema_version (
    version INTEGER PRIMARY KEY,
    applied_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```

### 5.2 `002_users.sql`

```sql
CREATE TABLE users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT UNIQUE COLLATE NOCASE
                  CHECK (
                      username IS NULL
                      OR (length(username) BETWEEN 3 AND 32
                          AND username GLOB '[A-Za-z0-9_.-]*')
                  ),                                    -- login identifier; nullable until user sets it
    name          TEXT NOT NULL,                        -- display name
    role          TEXT NOT NULL CHECK (role IN ('admin', 'manager', 'member', 'readonly')),
    birthdate     DATE,                                  -- NULL until set + approved
    created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    deleted_at    DATETIME                               -- soft delete
);

CREATE INDEX idx_users_role ON users(role) WHERE deleted_at IS NULL;
-- UNIQUE on username already creates an index (case-insensitive via COLLATE NOCASE).
```

### 5.3 `003_channel_bindings.sql`

```sql
CREATE TABLE channel_bindings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id),
    channel     TEXT NOT NULL,                          -- 'telegram' | 'discord' | 'web'
    chat_id     TEXT NOT NULL,
    bound_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(channel, chat_id)
);

CREATE INDEX idx_bindings_user ON channel_bindings(user_id);

CREATE TABLE invite_codes (
    code              TEXT PRIMARY KEY,                 -- short UUID, 8 chars
    intended_user_id  INTEGER NOT NULL REFERENCES users(id),
    created_by        INTEGER NOT NULL REFERENCES users(id),
    created_at        DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at        DATETIME NOT NULL,
    used_at           DATETIME,                         -- NULL = unused
    used_channel      TEXT,
    used_chat_id      TEXT
);

CREATE INDEX idx_invite_unused ON invite_codes(expires_at) WHERE used_at IS NULL;
```

### 5.4 `004_birthdate_changes.sql`

```sql
CREATE TABLE birthdate_changes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL REFERENCES users(id),
    new_birthdate   DATE NOT NULL,
    requested_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    approved_by     INTEGER REFERENCES users(id),     -- NULL = pending
    approved_at     DATETIME,
    rejected_at     DATETIME,
    rejection_note  TEXT
);

CREATE INDEX idx_birthdate_pending ON birthdate_changes(user_id)
    WHERE approved_at IS NULL AND rejected_at IS NULL;
```

### 5.5 `005_username_changes.sql`

```sql
CREATE TABLE username_changes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL REFERENCES users(id),
    old_username    TEXT,                              -- snapshot at request time (may be NULL if first set never approved)
    new_username    TEXT NOT NULL,
    requested_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    approved_by     INTEGER REFERENCES users(id),       -- NULL = pending
    approved_at     DATETIME,
    rejected_at     DATETIME,
    rejection_note  TEXT
);

CREATE INDEX idx_username_pending ON username_changes(user_id)
    WHERE approved_at IS NULL AND rejected_at IS NULL;
```

### 5.6 `006_parent_links.sql`

```sql
CREATE TABLE parent_links (
    parent_user_id        INTEGER NOT NULL REFERENCES users(id),
    child_user_id         INTEGER NOT NULL REFERENCES users(id),
    digest_frequency      TEXT NOT NULL DEFAULT 'daily'
                            CHECK (digest_frequency IN ('daily', 'weekly', 'monthly', 'off')),
    digest_time           TEXT NOT NULL DEFAULT '21:00',
    adult_optin_enabled   BOOLEAN NOT NULL DEFAULT 0,    -- only matters when child >= 18
    created_at            DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (parent_user_id, child_user_id),
    CHECK (parent_user_id != child_user_id)
);

CREATE INDEX idx_parent_links_child ON parent_links(child_user_id);
```

### 5.7 `007_quota.sql`

```sql
CREATE TABLE user_quotas (
    user_id            INTEGER NOT NULL REFERENCES users(id),
    period_month       TEXT NOT NULL,                   -- 'YYYY-MM'
    limit_usd_cents    INTEGER NOT NULL,                -- e.g., 100 = $1.00
    used_usd_cents     INTEGER NOT NULL DEFAULT 0,
    last_updated_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, period_month)
);
```

### 5.8 `008_password.sql`

```sql
ALTER TABLE users ADD COLUMN password_hash TEXT;
ALTER TABLE users ADD COLUMN reset_token TEXT;
ALTER TABLE users ADD COLUMN reset_token_expires_at DATETIME;
ALTER TABLE users ADD COLUMN must_reset_password BOOLEAN NOT NULL DEFAULT 0;
```

---

## 6. Vietnamese & English Command Prefixes

Mỗi command có **list prefixes**. Match longest-first sau khi normalize.

| Command ID | VN prefix (canonical) | EN alias |
|------------|----------------------|----------|
| `THEM_USER` | `them user` | `add user` |
| `XEM_DANH_SACH_USER` | `xem danh sach user` | `list users` |
| `XOA_USER` | `xoa user` | `delete user` |
| `GAN_QUOTA` | `gan quota` | `set quota` |
| `DANG_KY` | `dang ky` | `register` |
| `DAT_BIRTHDATE` | `dat birthdate` | `set birthdate` |
| `DUYET_BIRTHDATE` | `duyet birthdate` | `approve birthdate` |
| `DAT_USERNAME` | `dat username` | `set username` |
| `DUYET_USERNAME` | `duyet username` | `approve username` |
| `LIEN_KET_CHA_ME` | `lien ket cha me` | — (family-facing, VN only) |
| `XEM_CHA_ME_CUA` | `xem cha me cua` | — |
| `CAU_HINH_TONG_KET` | `cau hinh tong ket` | `config digest` |
| `CHIA_SE_VOI_CHA_ME` | `chia se voi cha me` | — |
| `TRANG_THAI_CHIA_SE` | `trang thai chia se` | — |

**Existing commands (giữ nguyên):**
- `ghi nho vao`, `xem nhat ky`, etc. — Vietnamese only (family-facing, không cần EN alias).
- Sau FR-2 sẽ áp dụng `normalize_vn` cho TẤT CẢ command để xử lý input có dấu.

### Helper signature

```python
# text_utils.py

def normalize_vn(text: str) -> str:
    """Lowercase + strip Vietnamese diacritics + collapse whitespace."""
    ...

def match_command(text: str, command_table: dict[str, list[str]]) -> tuple[str, str] | None:
    """
    Returns (command_id, remainder_text) or None.
    Tries longest prefix first across all commands.
    remainder_text is from the ORIGINAL text (preserving diacritics in content).
    """
    ...
```

---

## 7. Approach Decisions

### 7.1 SQLite path

- `config.SQLITE_PATH` env var, default `./bot.db`
- Local dev: `./bot.db` in repo root (add to `.gitignore`)
- Future production: `/data/bot.db` (Render persistent disk) — config decision deferred

### 7.2 Migration runner

- Đơn giản: scan `db/migrations/*.sql` theo thứ tự numeric, skip những version đã apply, INSERT vào `_schema_version` sau khi thành công.
- Idempotent: chạy lại trên DB đã apply migration — no-op.
- Single transaction per migration file.

### 7.3 Bootstrap admin

- Khi `users` rỗng và `TELEGRAM_CHAT_ID` env var có giá trị:
  - Tạo user "Bot Owner" với role `admin`
  - Bind chat_id vào `channel_bindings`
- Backward compatible với deployment hiện tại — user hiện tại tự động thành admin.

### 7.4 Authorization mới

Thay vì:
```python
if not channel.is_authorized(msg):
    return
```

Bây giờ:
```python
user = await user_store.find_by_channel(msg.channel, msg.chat_id)
if user is None:
    await channel.send(msg.chat_id,
        "Bạn chưa được đăng ký. Liên hệ admin để được mời.")
    return
if user.deleted_at is not None:
    await channel.send(msg.chat_id, "Tài khoản đã bị vô hiệu.")
    return
await handle_message(msg, user, deps)
```

Mọi `_cmd_*` handler thêm `user: User` parameter.

### 7.5 Invite code

- Format: 8 ký tự alphanumeric (case-insensitive), generated từ `secrets.token_hex(4)`
- TTL 7 ngày
- Single-use: set `used_at` + `used_channel` + `used_chat_id` khi consumed
- Admin tạo bằng `them user: <ten>, <role>` → bot reply code → admin gửi out-of-band cho người nhận
- Người nhận gửi `dang ky: <code>` qua Telegram → bind chat_id

### 7.6 Per-user quota

- Schema `user_quotas` (user_id, period_month, limit_usd_cents, used_usd_cents)
- Mỗi LLM call: check `used + estimated_cost <= limit` trước khi gọi
- Vượt → reply "Bạn đã hết hạn mức tháng này (đã dùng $X / $Y). Liên hệ admin."
- Default limit: split $5/10 user = $0.50/user (admin override được bằng `gan quota`)
- Bootstrap admin: limit $5 (cả family quota)

### 7.7 Runtime age check

```python
# permissions.py
def age_of(user: User, today: date = None) -> int:
    today = today or date.today()
    if user.birthdate is None:
        return -1                       # unknown
    delta = today - user.birthdate
    return delta.days // 365            # rough; sẽ chính xác hóa nếu cần

def is_adult(user: User) -> bool:
    return age_of(user) >= 18
```

Dùng sẵn từ FR-2 cho:
- Auto-disable adult-child notifications (FR-7+)
- Birthdate change flow (under-18 vs adult differs)

### 7.8 Argon2id password (FR-5 prep)

- `auth.hash_password(plaintext) -> str` dùng argon2-cffi defaults
- `auth.verify_password(plaintext, hash) -> bool`
- `auth.generate_reset_token() -> str`
- KHÔNG expose qua Telegram — sẽ dùng từ FR-5 (web auth)
- Schema migration `008_password.sql` thêm cột vào `users`

### 7.9 Username flow

- **Nullable.** `users.username` có thể NULL — bootstrap admin và user mới register qua Telegram chưa cần set ngay. User chỉ login web (FR-5) sau khi đã có username.
- **Set lần đầu** (`users.username IS NULL`): UPDATE trực tiếp, không cần duyệt. Lệnh `dat username: <name>`.
- **Đổi username** (đã có): tạo record trong `username_changes` (pending), cần admin duyệt bằng `duyet username: <user>`. Workflow song song với `birthdate_changes`.
- **Rate-limit 30 ngày** (application layer):
  ```sql
  SELECT MAX(approved_at) FROM username_changes
  WHERE user_id = ? AND approved_at IS NOT NULL
  -- nếu < 30 ngày trước now → reject, không tạo request
  ```
- **Reserved names** (chặn ở `text_utils.validate_username`):
  `admin`, `root`, `bot`, `system`, `support`, `owner`, `null`, `undefined`, `me`, `you`.
- **Format:** `[A-Za-z0-9_.-]`, length 3-32. CHECK constraint enforce ở DB, validator báo lỗi rõ ràng ở app.
- **Case-insensitive uniqueness:** `COLLATE NOCASE` ở cột → `'An' = 'an' = 'AN'` khi check UNIQUE.

### 7.10 Foreign key — hybrid pattern

- **Declare:** mọi FK relationship ghi rõ `REFERENCES users(id)` (hoặc bảng khác) trong CREATE TABLE → document quan hệ cho future-self.
- **Enforce:** bật `PRAGMA foreign_keys = ON` cho mọi connection trong `db/connection.py`. SQLite mặc định bỏ enforcement — không bật pragma = declarative only.
- **KHÔNG dùng `ON DELETE CASCADE`:** soft-delete bằng `deleted_at` column, không DELETE thật. Mặc định `ON DELETE NO ACTION` ép code application handle xóa rõ ràng.
- **Index FK column:** SQLite không auto-index FK (giống Oracle). Index riêng nếu query có pattern JOIN/lookup theo FK (vd `idx_bindings_user`, `idx_parent_links_child`).

---

## 8. Risk & Impact

**Risk: `high`**

### Impact

- **Security layer thay đổi:** Replace chat_id allowlist (`channel.is_authorized`) bằng user registry lookup. Nếu bootstrap admin fail → user mất quyền truy cập bot.
  - Mitigation: bootstrap chạy idempotent, có test case cho "users rỗng + chat_id env có" scenario.

- **DB dependency:** Bot không start được nếu SQLite path không writable hoặc migration fail.
  - Mitigation: clear error message at startup, fail-fast.

- **Handler signature thay đổi:** Mọi `_cmd_*` handler thêm `user: User` parameter — touching ~15+ handler trong `core_handler.py`.
  - Mitigation: tests cho command dispatch verify user được pass đúng.

- **cost_monitor schema change:** Family-level → per-user. Migration cần map data hiện tại vào `user_id` của bootstrap admin.
  - Mitigation: viết migration script logic trong startup nếu cần.

- **Free-form questions vẫn gọi LLM:** Per-user quota check phải áp dụng cho cả fast-path commands gọi LLM (vd summarize) và free-form questions.

- **Username uniqueness gotcha:** `COLLATE NOCASE` áp dụng trên cả UNIQUE constraint và CHECK — phải test cả 2 user nhập `'An'` và `'an'` xem reject đúng không. Reserved-names list phải normalize lowercase trước khi compare.

---

## 9. Dependencies

### 9.1 Python packages

Thêm vào `requirements.txt`:
- `argon2-cffi` (password hash)

Thêm vào `requirements-dev.txt` (mới):
- `pytest`
- `pytest-asyncio`

### 9.2 Env vars

| Var | Mới? | Default | Purpose |
|-----|------|---------|---------|
| `SQLITE_PATH` | Mới | `./bot.db` | SQLite file location |
| `TELEGRAM_CHAT_ID` | Có sẵn | — | Bootstrap admin chat_id |
| `TELEGRAM_TOKEN` | Có sẵn | — | Unchanged |
| `ANTHROPIC_API_KEY` | Có sẵn | — | Unchanged |

### 9.3 `.gitignore`

Thêm:
```
bot.db
bot.db-journal
bot.db-wal
bot.db-shm
```

---

## 10. Suggested Commit Order

Trong branch `feature/FR2`, mỗi sub-deliverable một commit (hoặc gộp logic):

| # | Commit message (English) | Files | Status |
|---|--------------------------|-------|--------|
| 1 | `feat(db): SQLite infrastructure + migration runner` | `db/*`, `db/migrations/001_initial.sql`, `config.py`, `.gitignore` | ✅ done |
| 2 | `feat(users): users table + UserStore + bootstrap admin` | `db/migrations/002_users.sql`, `user_store.py` (partial), `interfaces.py` (partial), `main.py`, `tests/test_user_store.py` (partial) | ✅ done |
| 3 | `feat(auth): channel bindings + invite-code registration` | `db/migrations/003_channel_bindings.sql`, `user_store.py`, `core_handler.py` (partial), `channel_telegram.py`, `tests/test_user_store.py` | ✅ done |
| 4 | `feat(text): normalize_vn helper + multi-prefix matcher` | `text_utils.py`, `core_handler.py` (dispatcher refactor), `tests/test_text_utils.py` | ✅ done |
| 5 | `feat(perms): roles + permissions middleware` | `permissions.py`, `core_handler.py` (commands gated), `tests/test_permissions.py` | ⬜ |
| 6 | `feat(birthdate): birthdate change flow with manager approval` | `db/migrations/004_birthdate_changes.sql`, `user_store.py`, `core_handler.py`, tests | ⬜ |
| 7 | `feat(username): username field + change flow with admin approval` | `db/migrations/005_username_changes.sql`, `user_store.py`, `core_handler.py`, `text_utils.py` (validator + reserved names), `tests/test_username.py` | ⬜ |
| 8 | `feat(family): parent_links + digest config commands` | `db/migrations/006_parent_links.sql`, `user_store.py`, `core_handler.py`, tests | ⬜ |
| 9 | `feat(quota): per-user quota tracking` | `db/migrations/007_quota.sql`, `cost_monitor.py`, `core_handler.py`, `tests/test_cost_monitor.py` | ⬜ |
| 10 | `feat(auth): argon2id password infrastructure (not yet exposed)` | `db/migrations/008_password.sql`, `auth.py`, `interfaces.py`, `requirements.txt`, `tests/test_auth.py` | ⬜ |
| 11 | `docs: update ROADMAP Section 8 + decision log entries from FR-2` | `docs/ROADMAP.md` | ⬜ |

Mỗi commit độc lập build-pass (import OK, migrations apply, có thể chạy `pytest`). FR-2 "hoàn chỉnh" sau commit 11.

---

## 11. Test Plan

### 11.1 Unit tests (pytest)

| Module | Coverage target |
|--------|----------------|
| `user_store` | CRUD users, bindings, invite codes; soft-delete; duplicate binding rejection |
| `permissions` | Role check matrix (4 roles × actions); age_of edge cases (no birthdate, exact 18) |
| `auth` | hash/verify round-trip; reset token generation; collision resistance (statistical) |
| `text_utils` | normalize_vn cases (đ, ư, ơ, accents); match_command longest-first; empty input; `validate_username` (format, length, reserved names, case-insensitive reserved check) |
| `cost_monitor` | Per-user accumulation; period rollover; quota exceeded; admin override |
| `username` | First-set (no approval) vs change (queue); rate-limit 30 ngày; CI uniqueness (`'An'` vs `'an'`); reject reserved names; approval flow round-trip |

### 11.2 Integration tests

- DB migration: fresh DB → run all migrations → verify schema matches expected
- Bootstrap: empty users + TELEGRAM_CHAT_ID env → admin created + bound
- Registration end-to-end: admin creates user → invite code → another chat_id consumes → binding created
- Authorization: unknown chat_id rejected; bound chat_id allowed

### 11.3 Manual smoke test (local, before commit 10)

Chạy `python main.py` local với fresh `bot.db`:
1. Bot start, log "DB migrated to version N"
2. Bot start, log "Bootstrapped admin: Bot Owner (id=1)"
3. Send `ghi nho vao test: hello` → save note OK (existing behavior preserved)
4. Send `xem danh sach user` → list 1 user (admin)
5. Send `them user: An, member` → reply with invite code
6. (Out-of-band) Use another Telegram account to send `dang ky: <code>` → bind successful
7. From new user: `ghi nho vao` → works
8. From new user: `xem danh sach user` → reject "không đủ quyền"
9. Quota: cause LLM call → cost recorded under correct user_id

---

## 12. Cross-machine Workflow

### Cuối day session (mọi máy)

```bash
git status
git add <files>
git commit -m "wip(FR-2): <short description>"
git push origin feature/FR2
```

### Đầu day session (máy còn lại)

```bash
git checkout feature/FR2
git pull origin feature/FR2
```

### Nguyên tắc

- Commit theo sub-deliverable hoàn chỉnh khi possible
- WIP commits OK trong day, có thể `git rebase -i` gộp trước khi merge (user thực hiện, không phải Claude)
- Nếu rebase: chỉ rebase commit chưa share — `feature/FR2` đã push thì cẩn thận với force-push

---

## 13. Definition of Done

FR-2 coi là DONE khi tất cả các tiêu chí sau pass:

- [ ] Tất cả 11 commit landed trên `feature/FR2`
- [ ] `pytest` pass 100% (không skip, không xfail)
- [ ] Manual smoke test (Section 11.3) pass tất cả các bước
- [ ] `python main.py` start không error trên fresh `bot.db`
- [ ] `python main.py` start không error trên existing `bot.db` (re-run migrations)
- [ ] Existing single-user flow preserved (admin từ bootstrap dùng được tất cả lệnh cũ)
- [ ] `docs/ROADMAP.md` Section 8 updated
- [ ] Code review pass (self-review: không có Python comment nào tiếng Việt, mọi commit message English)

Sau khi DONE: bạn merge `feature/FR2` vào `dev` (test trên Render staging với deploy logic mới — riêng SQLite ephemeral, accept data loss trong test phase). Sau đó merge vào `main` (sau khi đã có plan persist).

---

## 14. Decisions Captured (sẽ thêm vào ROADMAP Decision Log khi hoàn thành)

Pre-emptive entries:

| # | Topic | Decision |
|---|-------|----------|
| 36 | FR-2 dev mode | Local-only development (Plan 5); không deploy Render trong FR-2 |
| 37 | Tests framework | pytest + pytest-asyncio; tests/ directory mới |
| 38 | normalize_vn scope | Áp dụng cho tất cả fast-path command (cả VN có dấu + EN alias) |
| 39 | Command alias policy | Admin/dev-facing có EN alias; family-facing chỉ VN |
| 40 | Bootstrap admin | Auto-create từ TELEGRAM_CHAT_ID nếu users rỗng |
| 41 | Invite code format | 8-char hex, TTL 7 ngày, single-use |
| 42 | Quota default | $5 family / N user = $0.50/user; admin override |
| 43 | Password infra timing | argon2id schema + helpers ở FR-2; expose qua web ở FR-5 |
| 44 | Username field | Thêm `users.username` TEXT UNIQUE COLLATE NOCASE, nullable; format `[A-Za-z0-9_.-]` length 3-32; là login identifier (FR-5 dùng) |
| 45 | Username set policy | First-set (NULL → value) UPDATE trực tiếp; đổi sau đó qua `username_changes` queue + admin duyệt |
| 46 | Username rate-limit | 1 lần / 30 ngày kể từ `approved_at` gần nhất; check ở application layer |
| 47 | FK hybrid pattern | `REFERENCES` declared trong CREATE TABLE + `PRAGMA foreign_keys = ON` ở connection; KHÔNG dùng `ON DELETE CASCADE` (soft-delete bằng `deleted_at`) |

---

## 15. Open Issues (chưa có ý kiến cuối, sẽ resolve trong execution)

1. **Quota period boundary:** Tháng dương lịch (UTC) hay theo timezone Việt Nam (+7)? Đề xuất: VN timezone vì user Việt Nam.
2. **Soft-delete user:** Khi soft-delete, parent_links / channel_bindings có cascade hay giữ? Đề xuất: giữ để audit trail; query layer filter `deleted_at IS NULL`.
3. **Birthdate validation:** Năm sinh range nào hợp lệ? Đề xuất: 1900-01-01 → today (không cho future date).
4. **Invite code rate limit:** 1 admin có thể tạo bao nhiêu invite/ngày? Đề xuất: chưa giới hạn ở FR-2 (10 user gia đình, không có abuse threat).
5. **Username release on soft-delete:** Khi user bị soft-delete, username của họ có cho phép user mới đăng ký lại dùng không? Đề xuất: **giữ** — username unique vĩnh viễn để tránh nhầm trong audit/log; CI uniqueness sẽ block re-use tự động.

Sẽ raise rõ ràng khi gặp lúc code, không tự quyết.

---

## 16. After FR-2 — Next steps

1. Quyết định persist strategy (Litestream / Drive / paid) — pre-FR-3 mini-plan
2. FR-3: SQLite + Scope + L1 Memory (sẽ build trên DB infra của FR-2)
3. FR-4: Audit + under-18 + recycle bin (extends users + parent_links)

---

**End of FR-2 Plan**

> Đọc cùng `docs/ROADMAP.md` (Section 4 family model, Section 5 FR-2 entry) để hiểu context đầy đủ.
