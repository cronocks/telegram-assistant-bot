# FR-5 — Web UI + Password Auth — Detailed Implementation Plan

> **Status:** IN PROGRESS
> **Created:** 2026-05-22
> **Branch:** `feature/FR5` (branch off từ `main`)
> **Approach:** 1 PR duy nhất `feature/FR5` → `main`

---

## 1. Goal

FR-5 thêm kênh Web vào hệ thống: người dùng có thể chat với bot qua browser, không cần Telegram. Web UI dùng HTMX + Alpine.js + SSE (Server-Sent Events). Auth dùng Argon2id (hạ tầng từ FR-2) + session cookie server-side. Force-reset password on first web login.

FR-5 KHÔNG bao gồm: 2FA, magic link forgot-password (để FR-6), per-session activity log chi tiết (để FR-6), Web digest/reminder UI (để FR-7).

---

## 2. Context & Decisions

### 2.1 Decision references

| ROADMAP ref | Nội dung |
|---|---|
| Decision #9 | Web tech: HTMX + Alpine.js + SSE |
| Decision #10 | Auth: Argon2id + force reset; magic link forgot-pw |
| Decision #27 | Channel priority: Telegram → Web → Discord |

### 2.2 Quyết định nền tảng

| # | Quyết định |
|---|-----------|
| D1 | **Session storage**: SQLite bảng `web_sessions` (token + user_id + expiry + revoked_at) — revocable server-side; logout thật sự vô hiệu hóa session |
| D2 | **SSE delivery**: `WebChannelAdapter` giữ `Dict[str, asyncio.Queue]` keyed by `user_id` (string). `send()` push vào queue. SSE route drain queue. Nếu không có active connection → messages drop (synchronous chat flow, user luôn có SSE open) |
| D3 | **SSE connection**: 1 queue per user_id; tab mới override tab cũ. Tránh leak queue vô hạn khi user mở nhiều tab |
| D4 | **Force-reset flow**: thêm cột `must_change_password BOOLEAN` vào `users`. Admin set temp password qua Telegram command `dat web pass: <ten_user>, <mat_khau>` → set hash + must_change_password=1. Web login detect flag → redirect `/setup-password` |
| D5 | **Frontend**: HTMX + Alpine.js từ CDN. Không build step. SSE qua native `EventSource` API. Jinja2 templates (FastAPI built-in) |
| D6 | **CSRF**: `SameSite=Lax` cookie flag là đủ cho single-domain family system. Không cần CSRF token trong FR-5. Migration path sang CSRF token sau này: thêm `web_csrf.py` + hidden field vào forms — không đụng kiến trúc |
| D7 | **Login brute-force**: tái dùng bảng `sudo_attempts` với `channel="web"` — 5 fail → lock 15 phút |
| D8 | **New packages**: `jinja2`, `sse-starlette`, `python-multipart` |
| D9 | **Audit**: thêm `web_login` / `web_logout` / `web_password_set` vào audit taxonomy |

---

## 3. Scope Breakdown

| Sub | Tên | File chính |
|-----|-----|-----------|
| 5.1 | Config + packages | `requirements.txt`, `config.py` |
| 5.2 | Migration 016 + WebSessionStore | `016_web_sessions.sql`, `interfaces.py`, `web_session_store.py` |
| 5.3 | WebChannelAdapter | `web_channel.py` |
| 5.4 | Web router + templates | `web_router.py`, `templates/` |
| 5.5 | Admin command + wiring | `core_handler.py`, `deps.py`, `main.py` |
| 5.6 | Tests | `tests/test_web_*.py` |

---

## 4. Database Schema

### 4.1 `016_web_sessions.sql`

```sql
-- Add must_change_password flag to users
ALTER TABLE users ADD COLUMN must_change_password INTEGER NOT NULL DEFAULT 0;

-- Web session table
CREATE TABLE web_sessions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id),
    token       TEXT NOT NULL UNIQUE,           -- 32-byte random hex (256-bit entropy)
    created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at  DATETIME NOT NULL,              -- created_at + WEB_SESSION_TTL_DAYS
    revoked_at  DATETIME                        -- NULL = active; set on logout
);

CREATE INDEX idx_web_sessions_token ON web_sessions(token);
CREATE INDEX idx_web_sessions_user  ON web_sessions(user_id);
```

---

## 5. Architecture

### 5.1 Request flow — Chat

```
Browser (HTMX)
    │
    │  POST /chat/send  {text: "..."}
    ▼
web_router.py: send_message()
    │  get_current_user() → session cookie → web_session_store.find_active(token) → User
    │
    ▼
core_handler.handle_message(ChannelMessage(channel="web", chat_id=str(user_id)), user, web_deps)
    │
    ▼
WebChannelAdapter.send(user_id, reply_text)
    │
    ▼
asyncio.Queue keyed by user_id ──► SSE route drains ──► Browser EventSource
```

### 5.2 SSE flow

```
Browser opens EventSource("/chat/stream")
    │
    ▼
web_router.py: stream()
    │  get_current_user() → user
    │  web_channel.connect(user_id) → Queue
    │
    │  async for:
    │      event = await queue.get(timeout=30s)
    │      yield f"data: {event}\n\n"
    │
    │  [client disconnects]
    │  web_channel.disconnect(user_id)
```

### 5.3 Login flow

```
GET /login → login.html
POST /login {username, password}
    ├── find user by username/name
    ├── check brute-force lock (sudo_attempts channel="web")
    ├── verify Argon2id
    ├── if must_change_password=1 → redirect /setup-password
    └── create web_session token → set HttpOnly cookie → redirect /chat

GET /setup-password → setup_password.html
POST /setup-password {new_password, confirm_password}
    ├── verify current session (temp)
    ├── set_password() → Argon2id hash
    ├── must_change_password=0
    └── create full web_session → redirect /chat
```

---

## 6. Commands

| Command | Mô tả | Ai dùng |
|---------|-------|---------|
| `dat web pass: <ten_user>, <mat_khau>` | Set web password cho user chỉ định + must_change_password=1 | admin |

---

## 7. Audit Events (bổ sung vào taxonomy FR-4)

| `action` | `target_type` | Khi nào |
|---|---|---|
| `web_login` | `user` | Login web thành công |
| `web_logout` | `user` | Logout |
| `web_password_set` | `user` | Admin set web password cho user |
| `web_login_failed` | `user` | Login thất bại (user tồn tại nhưng sai pass) |

---

## 8. Security

| Biện pháp | Chi tiết |
|---|---|
| Password hashing | Argon2id (argon2-cffi, memory-hard) |
| Session token entropy | 32 bytes random hex = 256 bits |
| Cookie flags | `HttpOnly=True`, `SameSite=Lax`, `Secure=True` khi APP_ENV != local |
| CSRF | SameSite=Lax đủ cho single-domain; migration path sang CSRF token rõ ràng |
| Brute-force | Tái dùng sudo_attempts (channel="web") — 5 fail → lock 15 phút |
| Force-reset | must_change_password flag; không thể bypass qua redirect |
| Logout | Server-side revoke (revoked_at set); cookie xóa client-side |
| Audit | web_login / web_logout / web_password_set / web_login_failed |

---

## 9. Dependencies

- **Python packages:** `jinja2`, `sse-starlette`, `python-multipart`
- **Env vars:** `WEB_SECRET_KEY` (fail-fast nếu thiếu ở staging/prod), `WEB_SESSION_TTL_DAYS` (default 7)
- **Migration:** 016 chạy tự động lúc startup
- **FR phụ thuộc:** FR-2 (Argon2id infra, user store), FR-3.5 (sudo_attempts reuse), FR-4 (audit log)

---

## 10. Test Plan

| Module | Coverage |
|---|---|
| `web_session_store` | create → find_active; revoke → find_active returns None; expired → find_active returns None |
| `web_channel` | send → queue has item; connect/disconnect cleanup; multi-user isolation |
| `web_auth` (HTTP) | GET /login → 200; POST /login OK → cookie set; POST /login wrong pass → error; POST /login 5 fails → locked; logout → cookie cleared + session revoked; must_change_password → redirect to setup-password |

---

**End of FR-5 Plan**
