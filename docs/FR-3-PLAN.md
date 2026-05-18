# FR-3 — Detailed Implementation Plan

> **Status:** DRAFT — chờ review lần 2 trước khi execute, branch `feature/FR3`
> **Created:** 2026-05-18
> **Mode:** Local-only development + test trên Render staging (Docker + Litestream đã sẵn sàng từ FR-2)

---

## 1. Goal

Bổ sung 2 lớp lên nền multi-user của FR-2:

1. **Scope + ACL** — mỗi note/wiki page có chủ sở hữu (`owner_user_id`) và phạm vi chia sẻ (`scope`). Retrieval lọc theo quyền đọc của người hỏi.
2. **L1 Memory** — mỗi user có `MEMORY.md` + `USER.md` dạng frozen snapshot, được LLM curate (refine) từ note gần đây; inject vào context khi trả lời câu hỏi tự do.

Đây là nền cho FR-4 (audit + stealth-read under-18 + recycle bin) và lớp L2/L3 memory sau này.

---

## 2. Context & Decisions

### 2.1 Hiện trạng (sau FR-2)

- Note nằm trong **một folder Drive chung** (`Claude-Notes/`); wiki trong `Claude-Notes/Wiki/`. Không có khái niệm "file của ai".
- Retrieval (`search_notes`, `smart_search`, `retrieve_pages`, ...) query thẳng Drive — không lọc theo user.
- `users` table + roles đã có (FR-2). `User` dataclass đã có trong `interfaces.py`.

### 2.2 Quyết định nền tảng (chốt với user 2026-05-18)

| # | Quyết định |
|---|-----------|
| D1 | L1 Memory lưu trong **SQLite** (bảng `user_memory`, cột TEXT), không lưu file Drive. Litestream đã backup sẵn. |
| D2 | Chỉ **2 scope**: `private` + `everyone`. KHÔNG có `group` — quy mô gia đình, `everyone` = cả nhà. |
| D3 | Backfill file Drive cũ: owner = bootstrap admin; scope: note/journal = `private`, wiki = `everyone`. |
| D4 | **Default scope:** note + journal → `private`; wiki → `everyone`. Owner đổi được bằng lệnh. |
| D5 | **Chia sẻ tới người/nhóm cụ thể: hoãn.** `acl.py` thiết kế để thêm `note_shares` sau này không phá vỡ API. |
| D6 | Architecture: **Option A** — Drive giữ nội dung, SQLite làm lớp ACL/index (bảng `notes`, `wiki_pages`). |
| D7 | FR-3 KHÔNG làm admin stealth-read note private của người khác — đó là FR-4 (kèm audit). FR-3 ACL strict: `private` = chỉ owner. |

### 2.3 Mode

- Code + test local. Sau khi xong → merge `dev` test staging (Docker + Litestream restore/replicate) → merge `main`.
- Migration 009–011 chạy tự động lúc startup qua runner sẵn có.

---

## 3. Scope Breakdown (sub-deliverables)

| Sub | Tên | Mục đích |
|-----|-----|---------|
| 3.0 | Schema migrations | `notes`, `wiki_pages`, `user_memory` tables |
| 3.1 | NoteIndex adapter | `SqliteNoteIndex` — CRUD mapping `drive_file_id` ↔ owner/scope |
| 3.2 | ACL layer | `acl.py` — `can_read`, `filter_visible`; thiết kế mở rộng |
| 3.3 | Dual-write on create | `save_note`/`save_page`/journal trả `file_id`; core ghi SQLite row |
| 3.4 | ACL filter on retrieval | Mọi đường search/retrieve lọc theo người hỏi |
| 3.5 | Scope commands | `chia se` / `bo chia se` — owner đổi scope |
| 3.6 | Backfill | Index hóa file Drive hiện có, idempotent |
| 3.7 | L1 Memory store | `SqliteMemoryStore` + commands + LLM curation |
| 3.8 | Documentation | ROADMAP Section 8 + Decision Log |

---

## 4. File Changes Summary

### 4.1 New files

| # | File | Purpose |
|---|------|---------|
| 1 | `db/migrations/009_notes.sql` | `notes` metadata table |
| 2 | `db/migrations/010_wiki_pages.sql` | `wiki_pages` metadata table |
| 3 | `db/migrations/011_user_memory.sql` | `user_memory` table (L1) |
| 4 | `acl.py` | ACL helpers — `can_read`, `filter_visible` |
| 5 | `note_index.py` | `SqliteNoteIndex` — CRUD note/wiki metadata + backfill |
| 6 | `memory_store.py` | `SqliteMemoryStore` — get/set L1 memory per-user |
| 7 | `tests/test_acl.py` | ACL matrix tests |
| 8 | `tests/test_note_index.py` | NoteIndex CRUD tests |
| 9 | `tests/test_memory_store.py` | MemoryStore tests |
| 10 | `tests/test_scope.py` | Scope command + retrieval filtering tests |

### 4.2 Edited files

| # | File | Change |
|---|------|--------|
| 11 | `interfaces.py` | `save_*` trả `file_id`; thêm Protocol `NoteIndex`, `MemoryStore`; LLM method `curate_memory` |
| 12 | `drive_client.py` | `save_note` → `(filename, file_id)`; `add_to_daily_journal` → `(filename, action, file_id)` |
| 13 | `wiki_client.py` | `save_page` → `(filename, file_id)`; `retrieve_pages` nhận `viewer` để lọc ACL |
| 14 | `core_handler.py` | Register SQLite row khi tạo; ACL filter mọi retrieval; lệnh mới; inject L1 memory vào Q&A |
| 15 | `claude_client.py` | Thêm `curate_memory()` |
| 16 | `main.py` | Wire `NoteIndex` + `MemoryStore` vào CoreDeps; gọi backfill 1 lần lúc startup |
| 17 | `docs/ROADMAP.md` | Section 8 + Decision Log |

**Total: 17 file changes.**

---

## 5. Database Schema

### 5.1 `009_notes.sql`

```sql
CREATE TABLE notes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    drive_file_id   TEXT NOT NULL UNIQUE,
    owner_user_id   INTEGER NOT NULL REFERENCES users(id),
    scope           TEXT NOT NULL DEFAULT 'private'
                      CHECK (scope IN ('private', 'everyone')),
    kind            TEXT NOT NULL DEFAULT 'note'
                      CHECK (kind IN ('note', 'journal')),
    title           TEXT,
    created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    deleted_at      DATETIME
);

CREATE INDEX idx_notes_owner ON notes(owner_user_id) WHERE deleted_at IS NULL;
CREATE INDEX idx_notes_scope ON notes(scope) WHERE deleted_at IS NULL;
```

### 5.2 `010_wiki_pages.sql`

```sql
CREATE TABLE wiki_pages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    drive_file_id   TEXT NOT NULL UNIQUE,
    owner_user_id   INTEGER NOT NULL REFERENCES users(id),
    scope           TEXT NOT NULL DEFAULT 'everyone'
                      CHECK (scope IN ('private', 'everyone')),
    topic           TEXT NOT NULL,
    slug            TEXT NOT NULL,
    created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    deleted_at      DATETIME
);

CREATE INDEX idx_wiki_owner ON wiki_pages(owner_user_id) WHERE deleted_at IS NULL;
CREATE INDEX idx_wiki_scope ON wiki_pages(scope) WHERE deleted_at IS NULL;
```

### 5.3 `011_user_memory.sql`

```sql
CREATE TABLE user_memory (
    user_id      INTEGER NOT NULL REFERENCES users(id),
    kind         TEXT NOT NULL CHECK (kind IN ('memory', 'user')),
    content      TEXT NOT NULL DEFAULT '',
    updated_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    curated_at   DATETIME,
    PRIMARY KEY (user_id, kind)
);
```

- `kind='memory'` → `MEMORY.md` (snapshot facts). `kind='user'` → `USER.md` (user profile).
- Row tạo lazy lần đầu curate; không cần seed.

---

## 6. ACL Design (`acl.py`)

```python
def can_read(viewer: User, scope: str, owner_user_id: int) -> bool:
    """Whether `viewer` may read an item with given scope/owner.

    FR-3 rules (strict):
      - scope 'everyone' -> anyone may read
      - scope 'private'  -> only the owner
    Admin stealth-read of others' private items is FR-4 (with audit), NOT here.
    Designed to extend: a future note_shares lookup adds one OR-branch.
    """
    if scope == "everyone":
        return True
    return owner_user_id == viewer.id


def filter_visible(viewer: User, rows: list[dict]) -> list[dict]:
    """Keep only rows the viewer may read. `rows` carry 'scope' + 'owner_user_id'."""
    return [r for r in rows if can_read(viewer, r["scope"], r["owner_user_id"])]
```

- `can_owner_edit_scope(viewer, owner_user_id)` → chỉ owner đổi scope của chính mình (admin có thể được thêm sau).
- Không phụ thuộc DB → test thuần, nhanh.

---

## 7. Retrieval Flow (sau FR-3)

### 7.1 Note search (`search_notes`, `smart_search`, `get_recent_notes`, ...)

1. Drive search trả candidates `[{id, name, ...}]` (như cũ).
2. Lookup SQLite `notes` theo `drive_file_id IN (...)` → lấy `scope` + `owner_user_id`.
3. `acl.filter_visible(viewer, rows)` → bỏ file không được phép.
4. Trả phần còn lại.

> File Drive không có row SQLite (orphan) → coi như **không visible** (an toàn mặc định).

### 7.2 Wiki retrieval (`retrieve_pages`)

`retrieve_pages(question, keywords, viewer)` — thêm tham số `viewer`:

1. Đọc `_index.md`.
2. Query SQLite `wiki_pages` → tập `slug` viewer được đọc.
3. **Lọc các dòng index** xuống còn slug visible → "filtered index".
4. LLM chọn filename từ filtered index (không bao giờ thấy page private của người khác).
5. Đọc page, verify lại ACL trước khi trả.

---

## 8. Dual-write on Create

Thứ tự bắt buộc: **Drive trước (lấy `file_id`) → SQLite sau.**

```
1. Tạo file trên Drive  -> file_id
2. INSERT INTO notes/wiki_pages (drive_file_id=file_id, owner=..., scope=default)
3. Nếu bước 2 fail:
   - best-effort xóa file Drive vừa tạo (rollback)
   - audit_log("note_index_insert_failed", ...)
   - báo user: "Lưu thất bại, vui lòng thử lại."
```

- Append vào file có sẵn → không tạo row mới, chỉ `UPDATE updated_at` theo `drive_file_id`.
- Orphan (file Drive không có row): retrieval coi là không visible → không rò rỉ, chỉ "ẩn". Reconcile thủ công nếu cần (không làm command trong FR-3).

---

## 9. Commands (Vietnamese — family-facing, VN only)

| Command ID | VN prefix | Mô tả |
|------------|-----------|-------|
| `CHIA_SE_FILE` | `chia se` | Đặt scope file = `everyone`. Fuzzy match tên file. |
| `BO_CHIA_SE_FILE` | `bo chia se` | Đặt scope file = `private`. |
| `XEM_TRI_NHO` | `xem tri nho` | Hiển thị `MEMORY.md` của user. |
| `XEM_HO_SO` | `xem ho so` | Hiển thị `USER.md` của user. |
| `CAP_NHAT_TRI_NHO` | `cap nhat tri nho` | Trigger LLM curation → refresh `MEMORY.md` + `USER.md`. |

- Chỉ **owner** đổi scope file của mình → nếu không phải owner: "Bạn không phải chủ file này."
- Lệnh `chia se` / `bo chia se` áp dụng cho cả note và wiki page (lookup cả 2 bảng).

---

## 10. L1 Memory — Curation

### 10.1 `LLMClient.curate_memory`

```python
def curate_memory(
    self, recent_notes: list[dict], current_memory: str, current_user_profile: str
) -> tuple[str, str, int]:
    """Refine L1 memory from recent notes.

    Returns (new_memory_md, new_user_md, total_tokens).
    """
```

### 10.2 Trigger

- FR-3: **manual** — lệnh `cap nhat tri nho`. (Cron tự động để FR sau, tránh phình scope.)
- Curation đọc note gần đây của **chính user** (đã lọc ACL — chỉ note của họ), `user_memory` hiện tại → LLM trả snapshot mới → ghi lại 2 row.

### 10.3 Inject vào Q&A

- Khi user hỏi tự do (free-form qua `LLMClient.ask`): prepend `MEMORY.md` của user vào `notes_context`.
- Quota: curation + inject đều tính token per-user qua `cost_monitor` (FR-2).

---

## 11. Backfill (`note_index.backfill`)

- Chạy 1 lần lúc startup trong `main.py`, **sau** migrations.
- Logic:
  1. Liệt kê file trong notes folder + wiki folder qua adapter hiện có.
  2. Với mỗi `file_id` **chưa có** row SQLite → INSERT: owner = bootstrap admin id; scope: note/journal = `private`, wiki = `everyone`; `kind` suy từ tên (journal nếu khớp pattern `*_NhatKy.md`).
- **Idempotent:** file đã có row → skip. Chạy lại nhiều lần an toàn.
- Audit: `audit_log("note_index_backfill", details="inserted=N")`.

---

## 12. Risk & Impact

**Risk: `high`**

- **Interface change** — `save_note`/`save_page`/`add_to_daily_journal` đổi return type → mọi caller trong `core_handler.py` phải sửa đồng bộ. Mitigation: sửa hết trong 1 commit, test dispatch.
- **Retrieval đổi semantics** — mọi search/retrieve qua ACL filter; lọc sai = rò rỉ `private` hoặc ẩn nhầm note hợp lệ. Mitigation: test ACL matrix (role × scope × owner/non-owner) + test retrieval lọc đúng.
- **Dual-write consistency** — Drive OK / SQLite fail → rollback Drive delete; nếu delete cũng fail → orphan (ẩn, không leak). Chấp nhận ở quy mô gia đình.
- **Backfill trên production** — chạy 1 lần trên DB thật; phải idempotent + audit.
- **Wiki `_index.md`** — vẫn là 1 file chung; ACL enforce ở tầng SQLite, không ở file index. Index có thể chứa cả page private — lọc TRƯỚC khi đưa cho LLM (Section 7.2).

---

## 13. Dependencies

- **Python packages:** không thêm gì mới.
- **Env vars:** không thêm.
- **Migrations:** 009–011 chạy tự động lúc startup.

---

## 14. Suggested Commit Order

Branch `feature/FR3`, mỗi sub-deliverable một commit:

| # | Commit message (English) | Sub |
|---|--------------------------|-----|
| 1 | `feat(db): notes + wiki_pages + user_memory schema (009-011)` | 3.0 |
| 2 | `feat(acl): scope ACL helpers` | 3.2 |
| 3 | `feat(index): SqliteNoteIndex adapter + NoteIndex protocol` | 3.1 |
| 4 | `feat(notes): dual-write note/wiki metadata on create` | 3.3 |
| 5 | `feat(notes): ACL filter on all retrieval paths` | 3.4 |
| 6 | `feat(scope): chia se / bo chia se commands` | 3.5 |
| 7 | `feat(index): backfill existing Drive files` | 3.6 |
| 8 | `feat(memory): L1 memory store + curation + commands` | 3.7 |
| 9 | `docs: update ROADMAP Section 8 + decision log from FR-3` | 3.8 |

Mỗi commit build-pass (import OK, migrations apply, `pytest` xanh).

---

## 15. Test Plan

### 15.1 Unit tests (pytest)

| Module | Coverage target |
|--------|----------------|
| `acl` | `can_read` matrix: 4 role × 2 scope × owner/non-owner; `filter_visible` |
| `note_index` | CRUD; dual-write rollback; backfill idempotent; orphan = không visible |
| `memory_store` | get/set 2 kind; lazy create row; curated_at update |
| `scope` | `chia se`/`bo chia se` đổi scope; non-owner bị từ chối; retrieval lọc đúng sau khi đổi scope |

### 15.2 Integration / smoke (local + staging)

- Fresh DB → migrations 009–011 apply OK.
- User A tạo note → mặc định `private` → User B search không thấy; A thấy.
- A `chia se` note → B search thấy.
- Wiki: A tạo wiki → mặc định `everyone` → B thấy ngay.
- `cap nhat tri nho` → MEMORY.md/USER.md sinh ra; hỏi tự do → memory được inject.
- Backfill: DB cũ có file Drive → sau startup mọi file có row, scope đúng.

---

## 16. Definition of Done

- [ ] 9 commit landed trên `feature/FR3`
- [ ] `pytest` pass 100%
- [ ] Smoke test (15.2) pass
- [ ] `python main.py` start OK trên fresh DB và existing DB
- [ ] Existing single-user flow preserved (admin dùng được mọi lệnh cũ)
- [ ] Không note `private` nào rò rỉ qua retrieval của user khác
- [ ] `docs/ROADMAP.md` Section 8 updated
- [ ] Self-review: không Python comment tiếng Việt, commit message English

---

## 17. Decisions Captured (sẽ thêm vào ROADMAP Decision Log)

| # | Topic | Decision |
|---|-------|----------|
| 48 | Scope storage | Option A — SQLite metadata table (`notes`, `wiki_pages`) làm lớp ACL/index; Drive giữ nội dung |
| 49 | Scope values | Chỉ `private` + `everyone`; không `group` (gia đình nhỏ, `everyone` = cả nhà) |
| 50 | Default scope | note/journal = `private`; wiki = `everyone` |
| 51 | L1 Memory storage | SQLite (`user_memory` table), không file Drive |
| 52 | Admin private read | FR-3 ACL strict — admin KHÔNG đọc private người khác; stealth-read để FR-4 (kèm audit) |
| 53 | Per-person sharing | Hoãn; `acl.py` thiết kế để thêm `note_shares` sau không phá vỡ API |
| 54 | L1 curation trigger | FR-3 manual (`cap nhat tri nho`); cron tự động để FR sau |

---

## 18. Open Issues (resolve khi code, không tự quyết)

1. **Return type `save_*`** — tuple (ít churn) vs dict (tự mô tả). Đề xuất: tuple, đồng nhất style hiện có (`add_to_daily_journal` đã trả tuple).
2. **`kind` detection lúc backfill** — nhận diện journal qua pattern tên file `*_NhatKy.md`. Nếu pattern đổi → backfill gán nhầm `note`. Đề xuất: chấp nhận, journal/note khác nhau ít ở ACL (cùng `private`).
3. **Curation cost** — note nhiều → prompt dài. Đề xuất: cap số note gần đây đưa vào curation (vd 20 note mới nhất).

---

**End of FR-3 Plan**

> Đọc cùng `docs/ROADMAP.md` (Section 4 family model, Section 5 FR-3 entry) và `docs/FR-2-PLAN.md` (nền multi-user).
