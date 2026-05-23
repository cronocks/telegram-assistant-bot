# FR-5.5 — Web Chat History Sidebar — Detailed Implementation Plan

> **Status:** PENDING
> **Created:** 2026-05-23
> **Branch:** `feature/FR5_5` (branch off từ `main` sau khi FR-5 merge)
> **Approach:** 1 PR duy nhất `feature/FR5_5` → `main`

---

## 1. Goal

FR-5.5 thêm sidebar lịch sử hội thoại vào web UI (giống Claude.ai/ChatGPT): user có thể (1) xem lại các phiên chat cũ, (2) bấm vào để mở lại, (3) đặt tên / đổi tên hội thoại, (4) tìm kiếm trong lịch sử, (5) tạo conversation mới. Admin có view riêng để stealth-read hội thoại của user under-18.

FR-5.5 KHÔNG bao gồm: delete UI (Decision #74 — để FR sau), FTS5 search (Decision #73), cross-channel Telegram (Decision #71), shareable conversation link.

---

## 2. Context & Decisions

### 2.1 ROADMAP references

| Decision | Nội dung |
|---|---|
| #70 | FR-5.5 tách riêng khỏi FR-5 |
| #71 | Web-only — KHÔNG gộp Telegram |
| #72 | Title gen = LLM Haiku 4.5 + user có thể rename |
| #73 | Search = LIKE đơn giản v1 (không FTS5) |
| #74 | Retention vĩnh viễn, không recycle bin, không delete UI v1 |
| #75 | Admin stealth-read hội thoại user under-18, audit `stealth_read_web_conversation` |

### 2.2 Quyết định kiến trúc bổ sung (chốt khi lập plan)

| # | Quyết định |
|---|-----------|
| K1 | **SSE queue keyed by `conversation_id`** thay vì `user_id`. FE mở SSE với `?conversation_id=X`. Reply route về đúng tab/conversation. Multi-tab cùng conversation: tab mới override tab cũ (giữ pattern FR-5) |
| K2 | **Lazy create conversation**: chỉ INSERT vào `web_conversations` khi user gửi message đầu — tránh rác DB khi user mở "New chat" rồi rời đi |
| K3 | **Title generation async**: sau khi bot trả lời xong message đầu tiên, spawn `asyncio.create_task` gọi `LLMClient.generate_chat_title()` → UPDATE `web_conversations.title` → push SSE event `{type: "title_update"}` để FE refresh sidebar. Trước khi title sẵn sàng → hiển thị "New chat" |
| K4 | **Sidebar load all** v1: query toàn bộ conversations của user 1 lần (không pagination/infinite scroll). Order: `updated_at DESC`. Scale gia đình ~10 user → hàng trăm conversation max, OK |
| K5 | **Stealth-read tách route riêng**: `/admin/users/<id>/conversations` (admin only) + `/admin/conversations/<id>/messages`. KHÔNG mix vào sidebar bình thường để rõ ràng "view as admin" vs "chat bình thường" |
| K6 | **Ownership strict ở BE**: mọi query đọc/ghi conversation/message check `user_id = current_user.id` ở store layer (trừ stealth-read path đi qua hàm khác). FE không cần check |
| K7 | **Delete UI = OUT OF SCOPE** v1 (Decision #74) — ghi nhận vào "Out of scope" để FR sau pick up |
| K8 | **Conversation rename** inline trong sidebar (double-click tên → input editable → blur/Enter save) → PATCH `/api/conversations/<id>` |
| K9 | **URL pattern bookmarkable**: `/chat/<conversation_id>` mở conversation cụ thể; `/chat` (không id) tạo conversation mới lazy khi user gửi message đầu |

---

## 3. Scope Breakdown

| Sub | Tên | File chính |
|-----|-----|-----------|
| 5.5.1 | Migration 017 + WebConversationStore | `017_web_conversations.sql`, `web_conversation_store.py`, `interfaces.py` |
| 5.5.2 | WebChannelAdapter refactor (queue per `conversation_id`) | `web_channel.py` |
| 5.5.3 | Router endpoints + persist messages + `/chat/<id>` route | `web_router.py` |
| 5.5.4 | LLM title generation (async) | `claude_client.py`, `interfaces.py`, hook trong `web_router` |
| 5.5.5 | Templates: sidebar, new chat, rename, search | `templates/chat.html`, optional `templates/_sidebar.html` |
| 5.5.6 | Admin stealth-read view + audit | `web_router.py`, `templates/admin_*.html` |
| 5.5.7 | Tests | `tests/test_web_conversation_store.py`, `tests/test_web_conversation_router.py`, `tests/test_web_title_gen.py` |

---

## 4. Database Schema

### 4.1 `017_web_conversations.sql`

```sql
-- FR-5.5: Web chat history — conversations + messages tables

CREATE TABLE web_conversations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id),
    title       TEXT,                            -- NULL until LLM generates; FE shows "New chat"
    created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP  -- bump on every new message
);

CREATE INDEX idx_web_conversations_user_updated
    ON web_conversations(user_id, updated_at DESC);

CREATE TABLE web_messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL REFERENCES web_conversations(id),
    role            TEXT    NOT NULL,           -- 'user' | 'bot'
    text            TEXT    NOT NULL,
    created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_web_messages_conv_time
    ON web_messages(conversation_id, created_at);

-- For LIKE search later (v1 = scan); kept simple, no FTS5
CREATE INDEX idx_web_messages_text
    ON web_messages(conversation_id, text);
```

**Notes:**
- `title` nullable: cho phép message đầu được lưu trước khi LLM gen title xong.
- `updated_at` được bump bằng app code (không trigger SQL) khi insert message — đơn giản, đủ chính xác.
- Không trigger immutable: messages có thể vẫn cần edit/delete trong tương lai (vd user xóa lỗi gõ). FR-5.5 KHÔNG implement edit, nhưng không khóa cứng schema.

---

## 5. Architecture

### 5.1 Chat send flow

```
Browser (active conversation_id=X)
    │
    │  POST /chat/<X>/send  {text: "..."}
    ▼
web_router: send_message(X, text)
    │  ownership check: conv.user_id == current_user.id
    │  INSERT web_messages(X, role='user', text)
    │  UPDATE web_conversations.updated_at = now
    │
    ▼
core_handler.handle_message(
    ChannelMessage(channel="web", chat_id="X", text=...),
    user, web_deps
)
    │
    ▼
WebChannelAdapter.send(chat_id="X", text=reply)
    │  → push to queue[conversation_id="X"]
    │  → also INSERT web_messages(X, role='bot', text=reply)
    │  → UPDATE conv.updated_at
    │
    ▼
SSE route drains queue[X] → Browser EventSource (filtered by conv_id)
```

**Lưu ý lifecycle "New chat":**
- FE state: nếu chưa có `conversation_id`, gọi POST `/chat/send` (route mới, không có id) → BE tạo conversation lazy → trả lại `conversation_id` trong response → FE update URL state thành `/chat/<new_id>` (history.pushState) + reconnect SSE với conv_id mới.

### 5.2 SSE flow (per conversation)

```
Browser opens EventSource("/chat/stream?conversation_id=X")
    │  (FE chỉ reconnect khi switch conversation)
    ▼
web_router: chat_stream(conversation_id=X)
    │  ownership check
    │  web_channel.connect(conv_id="X") → Queue
    │
    │  async for:
    │      event = await queue.get(timeout=30s)
    │      yield {"data": event_json}
    │  finally:
    │      web_channel.disconnect("X")
```

**Event payload format (JSON-encoded):**
```json
{"type": "message", "text": "..."}
{"type": "title_update", "title": "Hỏi về cách trồng cây"}
```

FE parse `event.data` JSON → dispatch theo `type`.

### 5.3 Title generation flow

```
After bot reply for FIRST message of a conversation:
    │  (detect: SELECT COUNT(*) FROM web_messages WHERE conv_id=X = 2)
    ▼
asyncio.create_task(generate_title_async(conv_id, first_user_text, first_bot_text))
    │
    ▼
LLMClient.generate_chat_title(user_msg, bot_reply) → "Hỏi cách trồng cây..."
    │  (uses Haiku model — cheap, ~$0.0001 per call)
    │
    ▼
UPDATE web_conversations SET title = ? WHERE id = ?
    │
    ▼
Push SSE event {"type": "title_update", "title": "..."} qua queue của conv_id
    │
    ▼
FE update sidebar entry
```

### 5.4 Sidebar load flow

```
GET /chat (or /chat/<id>) renders chat.html
    │  Server-side: SELECT * FROM web_conversations
    │                WHERE user_id = current_user.id
    │                ORDER BY updated_at DESC
    │  Pass list to Jinja template
    ▼
Sidebar renders with full list
    │
    │  User clicks conversation Y → push history /chat/Y
    │  → FE fetch GET /api/conversations/Y/messages
    │  → render messages area
    │  → reconnect SSE with conv_id=Y
```

### 5.5 Stealth-read flow (admin under-18)

```
Admin → /admin/users  (list users under-18, role-gated)
       → /admin/users/<id>/conversations  (list user's convs)
       → /admin/conversations/<id>  (view messages, READ-ONLY)
          │
          │  emit audit_log:
          │    actor_user_id = admin.id
          │    action = "stealth_read_web_conversation"
          │    target_type = "web_conversation"
          │    target_id = <conv_id>
          │    payload = {"target_user_id": <child_id>}
```

Runtime check `is_under_18(target_user)` qua birthdate (consistent với FR-4); KHÔNG mutate DB.

---

## 6. New Routes & Endpoints

| Method | Path | Auth | Mô tả |
|--------|------|------|-------|
| GET    | `/chat` | user | Tạo new chat lazy; render template + load sidebar |
| GET    | `/chat/<conv_id>` | user (own conv) | Render template với conv active + load messages |
| POST   | `/chat/send` | user | Send message khi chưa có conv (lazy create) |
| POST   | `/chat/<conv_id>/send` | user (own conv) | Send message vào conv hiện có |
| GET    | `/chat/stream` | user | SSE cho conversation chưa được tạo (mới lazy) |
| GET    | `/chat/stream?conversation_id=<id>` | user (own conv) | SSE per conversation |
| GET    | `/api/conversations` | user | JSON list conversations của user (cho sidebar refresh) |
| GET    | `/api/conversations/<id>/messages` | user (own conv) | JSON list messages |
| PATCH  | `/api/conversations/<id>` | user (own conv) | Rename: body `{title: "..."}` |
| GET    | `/api/conversations/search?q=...` | user | LIKE search trong messages của user |
| GET    | `/admin/users` | admin | List users (focus under-18 cho stealth-read entry point) |
| GET    | `/admin/users/<id>/conversations` | admin | List conversations của user; chỉ cho phép xem nếu user under-18 |
| GET    | `/admin/conversations/<id>` | admin | Xem messages (emit audit `stealth_read_web_conversation`) |

---

## 7. WebConversationStore Protocol

```python
class WebConversationStore(Protocol):
    def create(self, user_id: int) -> int:
        """Create empty conversation, return id."""

    def get(self, conv_id: int) -> dict | None:
        """Return {id, user_id, title, created_at, updated_at} or None."""

    def list_for_user(self, user_id: int) -> list[dict]:
        """Return list ordered by updated_at DESC."""

    def rename(self, conv_id: int, new_title: str) -> bool:
        """Update title. Returns True if exists."""

    def set_title_if_null(self, conv_id: int, title: str) -> bool:
        """Set title only if currently NULL (idempotent for title gen)."""

    def add_message(self, conv_id: int, role: str, text: str) -> int:
        """Insert message + bump conversation.updated_at. Return message id."""

    def list_messages(self, conv_id: int) -> list[dict]:
        """Return [{role, text, created_at}] in chronological order."""

    def count_messages(self, conv_id: int) -> int:
        """For detecting "first message" trigger title gen."""

    def search(self, user_id: int, query: str, limit: int = 50) -> list[dict]:
        """LIKE search: returns [{conv_id, conv_title, snippet, created_at}]."""

    # Admin stealth-read path (separate to make audit boundary explicit)
    def admin_list_for_user(self, target_user_id: int) -> list[dict]:
        """Admin-scope: list conversations of any user (caller responsible for ACL)."""
```

---

## 8. LLM Title Generation

Thêm method vào `LLMClient` Protocol:

```python
def generate_chat_title(self, user_msg: str, bot_reply: str) -> tuple[str, int]:
    """Generate ~3-7 word title for a conversation from its first exchange.
    Returns (title, total_tokens). Should use a cheap model (Haiku 4.5)."""
```

**AnthropicLLM impl:**
- Model: `claude-haiku-4-5-20251001` (hardcode trong method, không dùng MODEL env vì luôn cần model rẻ)
- Prompt: ngắn gọn, ép output ≤ 7 từ, tiếng Việt nếu user message tiếng Việt, không có dấu ngoặc kép
- Max tokens: 30
- Fallback nếu lỗi/timeout: dùng `user_msg[:40].strip() + "..."` làm title tạm

**Cost tracking:** vẫn đi qua `cost_monitor` như các call khác.

---

## 9. Audit Events (bổ sung)

| `action` | `target_type` | `target_id` | `payload` | Khi nào |
|---|---|---|---|---|
| `web_conversation_created` | `web_conversation` | conv_id | `null` | Lazy create khi user gửi message đầu |
| `web_conversation_renamed` | `web_conversation` | conv_id | `{"old": "...", "new": "..."}` | User rename |
| `stealth_read_web_conversation` | `web_conversation` | conv_id | `{"target_user_id": N}` | Admin xem hội thoại user under-18 |

**KHÔNG ghi audit cho:**
- Mỗi message gửi đi/đến (quá nhiều noise)
- Title auto-gen (system action, không quan trọng)

---

## 10. Frontend Changes (templates/chat.html)

### 10.1 Layout

```
┌─────────────────────────────────────────────────────┐
│ Nav (Family Assistant, theme toggle, logout)        │
├──────────┬──────────────────────────────────────────┤
│ Sidebar  │  Messages area                           │
│          │                                          │
│ [+ Mới]  │  ┌──────────┐                            │
│          │  │ user msg │                            │
│ 🔍 ___   │  └──────────┘                            │
│          │              ┌──────────┐                │
│ • Conv1  │              │ bot msg  │                │
│ • Conv2  │              └──────────┘                │
│ • Conv3  │                                          │
│ ...      │  ─────────────────────                   │
│          │  [textarea] [Gửi]                        │
└──────────┴──────────────────────────────────────────┘
```

### 10.2 Sidebar behavior

- **Width:** ~260px desktop; collapsible toggle để ẩn (cho không gian chat rộng hơn)
- **Mobile (<768px):** mặc định collapsed, overlay khi mở
- **Active conversation:** highlight bằng background tinted
- **Title display:** dùng `title || "New chat"`
- **Rename:** double-click title → input inline → Enter/blur save (PATCH API) → Esc cancel
- **Search:** input ở top sidebar → debounce 300ms → call `/api/conversations/search?q=` → hiển thị result list thay thế conversation list (clear search → quay về list)
- **New chat button:** clear active conv_id, focus input, URL → `/chat`

### 10.3 Alpine state mở rộng

```js
function chatApp() {
  return {
    conversations: [],        // loaded server-side initial
    activeConvId: null,       // from URL or null = new
    messages: [],
    draft: '',
    sending: false,
    searchQuery: '',
    searchResults: null,      // null = not searching
    renamingId: null,
    _es: null,

    init() { ... }
    sendMessage() { ... }     // POST /chat/<id>/send or /chat/send
    switchConversation(id) { ... }  // load messages + reconnect SSE
    rename(id, newTitle) { ... }
    onSseEvent(e) {
      const data = JSON.parse(e.data);
      if (data.type === 'message') { ... }
      if (data.type === 'title_update') {
        const c = this.conversations.find(c => c.id === this.activeConvId);
        if (c) c.title = data.title;
      }
    }
    // ...
  };
}
```

---

## 11. Security

| Biện pháp | Chi tiết |
|---|---|
| Ownership check | Mọi query đọc/ghi conv/message ở store layer check `user_id = current_user.id` |
| Stealth-read | Tách path riêng (`admin_list_for_user`, `admin_get_messages`) — caller route phải verify `role=admin` + `is_under_18(target)` trước khi gọi |
| Audit | Mọi stealth-read emit `stealth_read_web_conversation`; rename emit `web_conversation_renamed` |
| SQL injection | Mọi query dùng parameterized; search escape `%` `_` trong query string |
| XSS | Jinja2 auto-escape bật mặc định; message render qua `x-text` (Alpine) — không innerHTML |
| Rate limit search | FE debounce 300ms; BE không cần lock vì query nhẹ |
| SSE auth | Mỗi request `/chat/stream` check session cookie + ownership |

---

## 12. Dependencies

- **Python packages:** không thêm gói mới (dùng lại jinja2, sse-starlette từ FR-5; LLM dùng anthropic SDK đã có)
- **Migration:** 017 tự động chạy lúc startup
- **FR phụ thuộc:** FR-4 (audit log), FR-5 (web channel + session infra)
- **Env vars:** không thêm mới

---

## 13. Test Plan

| Module | Coverage |
|---|---|
| `web_conversation_store` (~12 cases) | create + get + list_for_user; rename + set_title_if_null idempotency; add_message bumps updated_at; count_messages; list_messages order; search LIKE + escape; ownership isolation (user A không thấy conv của user B); admin_list_for_user bypasses ownership |
| `web_channel` (refactor, ~6 cases) | queue keyed by conv_id (không phải user_id); connect/disconnect per conv; multi-conv same user → isolated queues; send to non-existent conv → drop + warn |
| `web_router` HTTP (~15 cases) | GET /chat new → no conv created; POST /chat/send lazy create + return conv_id; POST /chat/<id>/send ownership check (403 if not owner); GET /chat/<id> render với active; GET /api/conversations list của user; PATCH rename + audit; GET search results; SSE stream filter by conv |
| `claude_client.generate_chat_title` (~3 cases) | Vietnamese input → Vietnamese title; English input → English; timeout/error → fallback truncate |
| Title gen integration (~3 cases) | After 1st reply → title async generated + SSE pushed; idempotent (không gen lại nếu đã có); cost tracked |
| Stealth-read (~5 cases) | Non-admin → 403; admin xem user >=18 → 403; admin xem under-18 → OK + audit emitted; audit payload đúng `target_user_id`; auto-OFF khi child birthdate reaches 18 (runtime check) |

**Target:** ≥ 40 test cases, all pass, 0 warnings.

---

## 14. Migration & Deployment

1. Migration 017 chạy auto khi deploy
2. Conversations cũ: không có (FR-5 không lưu) — sidebar trống cho user đã có account; lần chat đầu trên FR-5.5 sẽ tạo conv mới
3. KHÔNG cần env var mới
4. Render staging deploy `dev` branch trước → e2e test → merge `main`

---

## 15. Out of Scope (đề cập rõ)

| Item | Lý do hoãn |
|---|---|
| Delete conversation UI | Decision #74: vĩnh viễn, user tự delete sẽ làm sau |
| FTS5 search | Decision #73: LIKE đủ cho gia đình; FTS5 migration sau là additive |
| Cross-channel Telegram lịch sử | Decision #71: Telegram không persist; effort không xứng |
| Edit message | Không cần; user gửi nhầm thì gửi lại |
| Share conversation link | Privacy concern; gia đình không có nhu cầu rõ |
| Export conversation | Để FR-6 (Backup/Restore Tooling) làm chung |
| Conversation pinning/folders | YAGNI v1 |

---

## 16. Estimated Effort

| Sub | Effort |
|-----|--------|
| 5.5.1 Migration + store | 0.25 ngày |
| 5.5.2 Channel refactor | 0.15 ngày |
| 5.5.3 Router + persist | 0.3 ngày |
| 5.5.4 LLM title gen async | 0.2 ngày |
| 5.5.5 Templates sidebar + UX | 0.5 ngày |
| 5.5.6 Admin stealth-read | 0.25 ngày |
| 5.5.7 Tests | 0.4 ngày |
| Doc + manual test | 0.2 ngày |
| **Tổng** | **~2.25 ngày** |

---

**End of FR-5.5 Plan**
