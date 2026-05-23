# FR-6 — Backup / Restore Tooling — Detailed Implementation Plan

> **Status:** DRAFT — chờ review trước khi execute
> **Created:** 2026-05-23
> **Branch:** `feature/FR6` (branch off từ `main` theo Git workflow Section 3.5)
> **Approach:** 1 PR duy nhất `feature/FR6` → `main`

---

## 1. Goal

FR-6 thêm tooling backup/restore cho hệ thống — 3 năng lực chính:

1. **Export data của 1 user** — sinh ZIP gồm toàn bộ SQLite data + nội dung file Drive (notes, wiki) thuộc user đó. Cho phép cả admin export hộ user khác và user tự export data của mình (GDPR-style data portability).
2. **Import / restore từ backup** — admin upload ZIP đã export trước đó, hệ thống tạo lại user + data với ID mới (remap old → new), warn nếu trùng tên.
3. **Local mode migration** — CLI script standalone giúp clone toàn bộ DB + Drive content xuống local FS, chuẩn bị cho future "local-only" deployment mode (chạy hoàn toàn offline trên máy nhà).

FR-6 KHÔNG bao gồm: encrypted backup (lưu plaintext ZIP — gia đình tin nhau), schedule auto-backup (admin tự trigger; cron tự động để FR sau), partial restore (chỉ restore 1 phần data), conflict-resolution merge UI (import = tạo mới, không merge).

---

## 2. Context & Decisions

### 2.1 Decision references

| ROADMAP ref | Nội dung |
|---|---|
| Decision #11 | Storage cloud = SQLite + Litestream — DB đã được Litestream backup; FR-6 là backup logic-level (per-user), khác với Litestream (toàn DB) |
| Decision #43 | SQLite + Litestream → Cloudflare R2; FR-6 không thay thế Litestream, bổ sung |
| Section 5 — FR-6 | Scope ban đầu: export user data, import, local mode migration tool |

### 2.2 Quyết định nền tảng (chốt khi soạn plan này — bổ sung Decision Log khi merge)

| # | Quyết định |
|---|-----------|
| D1 | **Format ZIP, plaintext (không encrypt).** Gia đình tin nhau; encryption thêm complexity (key management) không xứng. Migration path sang encrypted backup nếu cần là additive (thêm flag `--encrypted` sau). |
| D2 | **Export tự đặt scope rõ ràng:** chỉ data của user đó. Audit log entries chỉ lấy rows `actor_user_id = user_id`. Parent links lấy cả 2 chiều (user là parent hoặc child). KHÔNG export channel_bindings của user khác, KHÔNG export wiki của user khác. |
| D3 | **Loại trừ khỏi export:** `web_sessions`, `elevation_sessions`, `sudo_attempts`, `pending_notifications`, `invite_codes`, `username_changes`/`birthdate_changes` đang pending — đều là dữ liệu phiên/quy trình, không có ý nghĩa sau restore. Lịch sử `username_changes`/`birthdate_changes` đã approved/rejected → có export (audit trail). |
| D4 | **Import tạo mới với ID mới** — không cố preserve old IDs. Build map `old_id → new_id` trong-process, dùng để remap mọi FK (notes.owner_user_id, web_conversations.user_id, etc.). Tránh conflict với rows hiện có. |
| D5 | **Conflict on user name** — nếu user import có `name` trùng với user active hiện có → reject (admin sửa tên ngoài rồi import lại). KHÔNG auto-rename. Lý do: admin cần biết explicit về conflict. |
| D6 | **Drive file upload trong import** — tạo file mới trên Drive (Drive cấp file_id mới); SQLite `notes.drive_file_id` dùng file_id mới. KHÔNG cố preserve drive_file_id cũ (vô nghĩa nếu Drive folder đã đổi). |
| D7 | **Audit log entries export chỉ ghi nhận, không restore** — import bỏ qua audit_log từ ZIP, ghi 1 audit event mới `data_imported` cho hành động import. Audit log là system record, không phải user data. |
| D8 | **Delivery channels:** Web download cho cả admin và self (`/admin/users/<id>/export`, `/settings/export`). Telegram cho admin (`xuat du lieu: <ten>` → upload Drive → reply link). Self qua Telegram để FR sau (UX phức tạp khi user nhận ZIP qua chat). |
| D9 | **Import qua Telegram = OUT OF SCOPE** v1. Telegram bot file upload + parsing ZIP phức tạp; admin xài web UI để import là đủ. |
| D10 | **`tools/local_migrate.py` standalone** — không nằm trong CoreDeps, không là route web. Admin chạy CLI ngoài process bot. Dùng cùng SQLite + Drive credentials (đọc từ env vars). Output: `./local_export/` directory với `bot.db` copy + `drive_files/` mirror. |
| D11 | **`BackupEngine` concrete class, không Protocol** — chỉ có 1 implementation (SQLite + Drive); Protocol overhead không cần thiết. Constructor inject `user_store`, `note_index`, `memory_store`, `web_conversation_store`, `audit`, `notes`/`wiki` (NoteStore/WikiStore để download/upload Drive content). |
| D12 | **Streaming export** — generate ZIP vào `BytesIO` rồi stream về client; tránh ghi file tạm trên disk (Render free tier ephemeral). Với user có data lớn (~100MB+) thì memory pressure — chấp nhận, gia đình ~10 user × wiki <50MB là OK. |
| D13 | **Rate-limit export** — 1 export per user per 5 phút (chống abuse + Drive API quota); track in-memory dict `last_export_at`. Reset khi process restart (OK vì không bảo mật chống admin malicious). |

### 2.3 Export taxonomy

Mọi event backup/restore ghi vào `audit_log` (FR-4):

| `action` | `target_type` | `target_id` | `payload` | Khi nào |
|---|---|---|---|---|
| `data_export` | `user` | user_id | `{"size_bytes": N, "notes": M, "wiki_pages": K, "messages": L, "delivery": "web"\|"telegram_drive"}` | Mỗi lần export thành công |
| `data_import` | `user` | new_user_id | `{"source_name": "...", "id_map": {old→new}, "items_imported": {...}}` | Mỗi lần import thành công |
| `data_export_failed` | `user` | user_id | `{"error": "..."}` | Export fail (lưu để debug) |
| `data_import_failed` | — | — | `{"error": "...", "stage": "validate"\|"apply"}` | Import fail (rollback) |

---

## 3. Scope Breakdown

| Sub | Tên | File chính |
|-----|-----|-----------|
| 6.1 | BackupEngine (export) | `backup_engine.py` |
| 6.2 | ImportEngine (validate + apply) | `backup_engine.py` (cùng module) |
| 6.3 | Web routes export + import | `web_router.py` (edit), `templates/import.html` (new) |
| 6.4 | Telegram command admin export | `core_handler.py` (edit) |
| 6.5 | Local mode migration CLI | `tools/local_migrate.py` (new) |
| 6.6 | Wiring + audit + rate-limit | `deps.py`, `main.py` (edit) |
| 6.7 | Tests | `tests/test_backup_engine.py`, `tests/test_backup_routes.py`, `tests/test_local_migrate.py` |

---

## 4. File Changes Summary

### 4.1 New files

| # | File | Purpose |
|---|------|---------|
| 1 | `backup_engine.py` | `BackupEngine` class — `generate_export(user_id)`, `parse_import(zip_bytes)`, `apply_import(parsed)` |
| 2 | `tools/__init__.py` | Package init |
| 3 | `tools/local_migrate.py` | CLI script: dump SQLite + sync Drive files → local dir |
| 4 | `templates/import.html` | Web UI: admin upload ZIP + confirm import |
| 5 | `tests/test_backup_engine.py` | Unit tests cho export + import logic |
| 6 | `tests/test_backup_routes.py` | HTTP tests cho web export/import endpoints |
| 7 | `tests/test_local_migrate.py` | Tests cho CLI migrate script |

### 4.2 Edited files

| # | File | Change |
|---|------|--------|
| 8 | `deps.py` | Thêm `backup_engine: BackupEngine` vào `CoreDeps` |
| 9 | `main.py` | Wire `BackupEngine(user_store, note_index, memory_store, web_conversation_store, audit, notes, wiki)` vào `CoreDeps` (cả telegram + web) |
| 10 | `web_router.py` | 3 routes mới: GET `/admin/users/<id>/export`, GET `/settings/export`, POST `/admin/import` (multipart upload + preview + apply); 1 GET `/admin/import` để render form |
| 11 | `core_handler.py` | Lệnh `xuat du lieu` (self) + `xuat du lieu: <ten>` (admin); dispatch + help group `quan tri` mở rộng |
| 12 | `templates/chat.html` (sidebar nav) | Thêm link admin "Quản lý dữ liệu" (gated role=admin) → `/admin/import` |
| 13 | `interfaces.py` | (Có thể) thêm method `download_file_content(file_id) -> bytes` vào `NoteStore`/`WikiStore` nếu chưa có; xem code khi implement |
| 14 | `drive_client.py` / `web_wiki_client.py` | Implement download nếu thiếu |

---

## 5. Database Schema

**KHÔNG có migration mới.** FR-6 chỉ đọc/ghi data hiện có; không thêm bảng.

Audit events mới (Section 2.3) dùng `audit_log` đã có; không cần schema change.

---

## 6. Export Format Specification

### 6.1 ZIP layout

```
export_<user_name>_<YYYYMMDD-HHMMSS>.zip
├── manifest.json
├── data.json
├── notes/
│   ├── <drive_file_id_1>.md
│   ├── <drive_file_id_2>.md
│   └── ...
└── wiki/
    ├── <slug_1>.md
    ├── <slug_2>.md
    └── ...
```

### 6.2 `manifest.json`

```json
{
  "format_version": 1,
  "exported_at": "2026-05-23T15:30:00+07:00",
  "exporter": "telegram-bot-fr6",
  "source_user": {
    "id": 5,
    "name": "An",
    "username": "an_nguyen",
    "role": "member"
  },
  "stats": {
    "notes": 42,
    "wiki_pages": 8,
    "memory_kinds": 2,
    "web_conversations": 15,
    "web_messages": 234,
    "audit_entries": 580,
    "size_bytes_uncompressed": 1245678
  }
}
```

### 6.3 `data.json` shape

```json
{
  "user": {
    "id": 5,
    "name": "An",
    "username": "an_nguyen",
    "role": "member",
    "birthdate": "2010-03-15",
    "password_hash": "$argon2id$...",
    "must_change_password": 0,
    "created_at": "...",
    "deleted_at": null
  },
  "channel_bindings": [
    {"channel": "telegram", "chat_id": "12345", "bound_at": "..."}
  ],
  "quota": {
    "monthly_token_limit": 100000,
    "used_tokens": 23456,
    "month": "2026-05",
    "updated_at": "..."
  },
  "parent_links_as_child": [
    {"parent_name": "Bố An", "digest_frequency": "daily", "digest_time": "21:00", ...}
  ],
  "parent_links_as_parent": [
    {"child_name": "Em An", ...}
  ],
  "username_changes": [
    {"old_username": null, "new_username": "an_nguyen", "approved_at": "...", "approved_by_name": "Bot Owner"}
  ],
  "birthdate_changes": [...],
  "notes": [
    {
      "drive_file_id": "1abcDEF...",
      "scope": "private",
      "kind": "note",
      "title": "My first note",
      "created_at": "...",
      "updated_at": "...",
      "deleted_at": null,
      "content_path": "notes/1abcDEF...md"
    }
  ],
  "wiki_pages": [
    {
      "drive_file_id": "1xyzABC...",
      "scope": "everyone",
      "topic": "Recipe Index",
      "slug": "recipe-index",
      "created_at": "...",
      "updated_at": "...",
      "deleted_at": null,
      "content_path": "wiki/recipe-index.md"
    }
  ],
  "user_memory": [
    {"kind": "memory", "content": "...", "updated_at": "...", "curated_at": "..."},
    {"kind": "user", "content": "...", "updated_at": "...", "curated_at": "..."}
  ],
  "web_conversations": [
    {
      "id": 12,
      "title": "Hỏi cách trồng cây",
      "created_at": "...",
      "updated_at": "...",
      "messages": [
        {"role": "user", "text": "...", "created_at": "..."},
        {"role": "bot", "text": "...", "created_at": "..."}
      ]
    }
  ],
  "audit_entries": [
    {"action": "scope_change", "target_type": "note", "target_id": "1abc...", "payload": {...}, "created_at": "..."}
  ]
}
```

**Lưu ý:**
- `password_hash` xuất nguyên — khi import vào hệ thống khác, password cũ vẫn dùng được (admin có thể `dat web pass` reset nếu muốn).
- `parent_links_as_*` xuất tên parent/child thay vì id để import biết link với user nào (tìm theo tên trong target system).
- `web_conversations[].messages` lồng vào (denormalized) — đơn giản hóa import.
- `audit_entries` chỉ dùng để xem (information only); import bỏ qua.

---

## 7. BackupEngine API

```python
class BackupEngine:
    def __init__(
        self,
        user_store: UserStore,
        note_index: NoteIndex,
        memory_store: MemoryStore,
        web_conversation_store: WebConversationStore,
        audit: AuditLog,
        notes: NoteStore,
        wiki: WikiStore,
    ): ...

    # Export
    def generate_export(self, user_id: int) -> tuple[bytes, dict]:
        """Generate ZIP for one user.
        Returns (zip_bytes, manifest_dict).
        Raises ExportError if user not found.
        """

    # Import
    def parse_import(self, zip_bytes: bytes) -> ParsedImport:
        """Validate ZIP structure + parse manifest/data.
        Returns ParsedImport with stats + warnings (e.g. name conflict).
        Raises ImportFormatError if invalid.
        """

    def apply_import(self, parsed: ParsedImport, *, admin_user_id: int) -> ImportResult:
        """Apply parsed import:
          1. Create new user (with new id)
          2. Insert channel_bindings (only those not conflicting — drop on conflict)
          3. Insert quota
          4. Upload notes content to Drive → INSERT notes index rows
          5. Upload wiki content to Drive → INSERT wiki_pages index rows
          6. INSERT user_memory
          7. INSERT web_conversations + web_messages
          8. INSERT parent_links (resolve parent/child by name; warn if not found)
          9. Emit audit `data_import` with id_map payload
        Returns ImportResult with new_user_id + counts + warnings.
        On any step fail: rollback Drive uploads + DB transaction, emit `data_import_failed`.
        """


@dataclass
class ParsedImport:
    manifest: dict
    data: dict
    notes_content: dict[str, bytes]   # old_drive_file_id → content
    wiki_content: dict[str, bytes]    # slug → content
    warnings: list[str]               # e.g. "name conflict: 'An' already exists"


@dataclass
class ImportResult:
    new_user_id: int
    counts: dict[str, int]
    id_map: dict[str, dict[int, int]]   # e.g. {"notes": {old_id: new_id}, ...}
    warnings: list[str]
```

### 7.1 Export flow chi tiết

```
generate_export(user_id):
  1. user = user_store.get_user_by_id(user_id)  → raise if None
  2. Build data dict (query SQLite cho từng table)
  3. Iterate notes:
     - row = SELECT * FROM notes WHERE owner_user_id = ?
     - content = notes.read_file_by_id(row.drive_file_id)  → bytes
     - Add to zip: notes/<drive_file_id>.md
  4. Same cho wiki_pages
  5. Build manifest with stats
  6. Write manifest.json + data.json vào ZIP
  7. Emit audit data_export
  8. Return (zip_bytes, manifest)
```

### 7.2 Import flow chi tiết

```
apply_import(parsed, admin_user_id):
  Step 1 — Validate (post-parse):
    - Check name conflict: user_store.find_by_name(parsed.data.user.name)
      → if active user exists: WARN, hỏi admin có override? (UI quyết định)
    - Check ZIP format_version == 1 → else fail
    - Validate all content files referenced trong data.notes/wiki có trong ZIP

  Step 2 — Apply (transactional):
    Begin SQLite transaction
    Try:
      a. INSERT users (new id assigned) → store new_user_id
      b. INSERT channel_bindings (skip conflicting (channel, chat_id))
      c. INSERT user_quotas
      d. For each note in parsed.data.notes:
           - upload to Drive: new_file_id = notes.upload_content(filename, content)
           - INSERT notes (drive_file_id=new_file_id, owner_user_id=new_user_id, ...)
           - Append (old_file_id, new_file_id) to id_map.drive_files
      e. Same cho wiki_pages
      f. INSERT user_memory rows
      g. INSERT web_conversations (new conv id) + web_messages
      h. Resolve parent_links by name → INSERT parent_links
      i. audit.log(actor=admin, action='data_import', target_user=new_user_id, payload={...})
    Commit
    On exception:
      - Rollback SQLite
      - Best-effort: delete uploaded Drive files (track new_file_ids)
      - Emit data_import_failed
      - Re-raise
```

### 7.3 Edge cases handled

| Case | Behavior |
|---|---|
| Import user name trùng active user | Warn → require admin explicit confirm; nếu confirm: import vẫn tạo user mới (DB UNIQUE constraint trên name cho phép vì old user mới deleted hoặc constraint không enforce) — cần kiểm code, có thể fail. Cách an toàn: reject + ask admin rename source. |
| Import note có drive_file_id đã tồn tại | Không xảy ra — Drive sinh ID mới khi upload. |
| Import channel_binding trùng | Skip (giữ binding hiện tại); warn. |
| Parent link not found (parent/child name không tồn tại) | Skip + warn. |
| ZIP corrupted | Raise `ImportFormatError`. |
| ZIP > 100MB | Reject (rate-limit upload size); admin import qua local script nếu cần. |
| Drive upload fail giữa chừng | Rollback: delete tất cả file Drive đã upload (best-effort); rollback SQLite transaction. |

---

## 8. Web Routes

| Method | Path | Auth | Mô tả |
|--------|------|------|-------|
| GET    | `/settings/export` | user | Tự export data của mình; streams ZIP download |
| GET    | `/admin/users/<id>/export` | admin | Admin export hộ user khác; streams ZIP download |
| GET    | `/admin/import` | admin | Render form upload (`templates/import.html`) |
| POST   | `/admin/import/preview` | admin | Multipart upload ZIP → parse (no apply) → render preview với stats + warnings |
| POST   | `/admin/import/apply` | admin | Apply previously-uploaded ZIP (token-based: response của `/preview` chứa upload token; FE submit token + confirm) |

**Lưu ý implementation:**
- `/preview` lưu ZIP bytes tạm trong-memory với token UUID; TTL 5 phút; `/apply` consume token.
- Stream upload nếu ZIP lớn — `python-multipart` đã có (FR-5 dep).
- Rate-limit: per-user export cooldown 5 phút; track in `dict[user_id, datetime]` (in-memory).

---

## 9. Telegram Commands

| Command ID | VN prefix | Mô tả | Ai dùng |
|---|---|---|---|
| `XUAT_DU_LIEU_SELF` | `xuat du lieu` (không có `:`) | Self-export → upload Drive → reply link | mọi user |
| `XUAT_DU_LIEU_ADMIN` | `xuat du lieu: <tên>` | Admin export hộ user khác | admin |

**Flow:**
1. User gửi lệnh → handler check rate-limit
2. Spawn `asyncio.create_task` (export có thể mất vài giây với data nhiều)
3. `BackupEngine.generate_export()` → zip_bytes
4. Upload zip lên Drive folder `Claude-Notes/Backups/`; file name `export_<user>_<timestamp>.zip`
5. `notes.share_with_user(file_id, OWNER_EMAIL)` (best-effort)
6. Reply: "Đã tạo backup: <Drive link>"
7. Audit `data_export` với `delivery: telegram_drive`

**KHÔNG có Telegram import command** (Decision #D9).

---

## 10. Local Mode Migration CLI

### 10.1 `tools/local_migrate.py`

```bash
python tools/local_migrate.py --output ./local_export
```

**Behavior:**
1. Đọc `.env` (cùng cách `main.py` load config)
2. Connect SQLite (qua `db/connection.py`)
3. Copy `bot.db` → `<output>/bot.db` (close + open in read-only mode để tránh corruption)
4. Initialize Drive client (qua `drive_client.py` helpers)
5. List tất cả file trong `Claude-Notes/` recursively
6. Cho mỗi file: download → ghi vào `<output>/drive_files/<relative_path>`
7. Sinh `<output>/manifest.json` với stats + timestamp
8. Print summary

**Use case tương lai:** chuẩn bị cho local-only mode (FR future); FR-6 chỉ làm tool, không thay đổi runtime.

**KHÔNG cần audit log** (script chạy ngoài bot process; không có user context).

### 10.2 `tools/local_migrate.py` arguments

| Flag | Default | Mô tả |
|---|---|---|
| `--output <dir>` | `./local_export` | Output directory |
| `--include-deleted` | False | Include soft-deleted notes/wiki |
| `--users <id1,id2>` | (all) | Limit to specific users |
| `--dry-run` | False | Print kế hoạch, không thực sự download |

---

## 11. Security

| Biện pháp | Chi tiết |
|---|---|
| Export auth | Self-export check `user.id == session.user.id`; admin-export check `user.role == 'admin'` (cả natively + elevated) |
| Import auth | Admin only (gated bằng `has_role(user, 'admin')`) |
| Rate-limit | 1 export per user per 5 phút (in-memory); upload ZIP size limit 100MB |
| Password hash exported | OK — Argon2id là one-way; export hash không leak password gốc. Admin có thể `dat web pass` reset sau import nếu lo. |
| Audit trail | Mọi export/import emit `data_export`/`data_import` (Section 2.3 taxonomy) |
| Drive scope | Export đọc file Drive thuộc owner; Drive folder permission đã được FR-3 setup; không cần permission change |
| Import safety | Apply trong SQLite transaction; rollback Drive uploads on failure (best-effort); warn admin trên name conflict trước khi apply |
| ZIP path traversal | Reject filenames có `..` hoặc absolute path khi parse ZIP |
| Memory pressure | Export streaming vào `BytesIO`; chấp nhận với scope gia đình (<200MB) |

---

## 12. Risk & Impact

**Risk: `high`**

| Risk | Mitigation |
|---|---|
| Drive API quota exhausted khi export/import nhiều | Rate-limit per-user 5 phút; warn trong audit; admin có thể chờ retry |
| Import rollback fail mid-way (DB rollback OK nhưng Drive files đã upload) | Track `uploaded_file_ids` list; trong `except` block: `for fid in uploaded_file_ids: notes.delete_file(fid)` (best-effort); log warnings cho file không xóa được |
| Conflict user name trên import | Reject sớm với clear error; admin sửa source `data.json` rồi re-upload, hoặc đổi tên user hiện tại trước |
| Parent links resolve sai (cùng tên 2 user khác) | Khi resolve, lookup user active; nếu >1 match → warn + skip link đó |
| Export file lộ password hash | Document trong UI: "File backup chứa Argon2id password hash. Giữ kín như password gốc." |
| Local migrate fail giữa chừng | Idempotent: re-run sẽ skip file đã tồn tại trong output dir (compare size + mtime) |
| Memory blow up với user có wiki/notes lớn | Streaming response, ZIP vào BytesIO; cap upload 100MB; nếu user vượt → khuyên dùng local migrate tool |
| Audit log export rò rỉ data người khác | Filter `WHERE actor_user_id = ?` ngay từ query, không cần app-layer filter |

**Impact lên codebase:**
- `CoreDeps` thêm 1 field — touch `deps.py`, `main.py`, và `web_router.py` chỗ build `web_deps`
- `web_router.py` thêm ~150 LOC cho 5 routes
- `core_handler.py` thêm ~80 LOC cho 2 commands
- `backup_engine.py` ~400-500 LOC (logic chính)
- Không phá API hiện có; FR-6 100% additive

---

## 13. Dependencies

- **Python packages:** không thêm. `zipfile` stdlib, `json` stdlib, `io.BytesIO` stdlib. `python-multipart` đã có (FR-5).
- **Env vars:** không thêm.
- **Migrations:** không có schema change.
- **FR phụ thuộc:**
  - FR-2 (UserStore)
  - FR-3 (NoteIndex + Drive content)
  - FR-3.5 (sudo precedent cho audit pattern)
  - FR-4 (AuditLog + recycle bin precedent cho soft-delete handling)
  - FR-5 (web routes + admin session)
  - FR-5.5 (WebConversationStore)

---

## 14. Test Plan

### 14.1 Unit tests

| Module | Coverage |
|---|---|
| `backup_engine.generate_export` (~10 cases) | Export empty user (no notes, no wiki); export user với mixed scope notes; export bao gồm soft-deleted notes (giả định include); manifest stats khớp; ZIP structure đúng; audit emitted; raises on non-existent user |
| `backup_engine.parse_import` (~6 cases) | Valid ZIP parse OK; missing manifest.json → error; format_version > 1 → error; missing content file referenced trong data → warning; corrupt ZIP → error; path traversal in filenames → error |
| `backup_engine.apply_import` (~10 cases) | Happy path tạo user + notes + wiki; channel_binding conflict skip + warn; name conflict reject; parent link resolve OK; parent link not found warn; rollback Drive upload on DB fail; rollback DB on Drive fail; id_map đúng; audit emitted; web_conversations + messages preserved |
| Edge cases (~4 cases) | User no birthdate; user no password (NULL hash); web conversation no title; note with deleted_at set |

### 14.2 HTTP / route tests

| Route | Coverage |
|---|---|
| GET `/settings/export` | Authenticated user → 200 + Content-Type: application/zip + valid ZIP body; unauthenticated → 302 login |
| GET `/admin/users/<id>/export` | Admin → 200 + ZIP; non-admin → 403; non-existent user → 404 |
| GET `/admin/import` | Admin → 200 render form; non-admin → 403 |
| POST `/admin/import/preview` | Admin upload valid ZIP → 200 render preview + stats + token; invalid ZIP → 400 error; >100MB → 413 |
| POST `/admin/import/apply` | Admin valid token → 200 success + new_user_id; expired token → 400; non-admin → 403 |
| Rate-limit | 2nd export within 5 min → 429 |

### 14.3 Telegram tests

| Command | Coverage |
|---|---|
| `xuat du lieu` (self) | Returns Drive link; audit `data_export` with `delivery=telegram_drive` |
| `xuat du lieu: <ten>` from admin | Targeted user found → OK; not found → error msg; non-admin → "không đủ quyền" |
| Rate-limit | Self spam command → "Đợi 5 phút" |

### 14.4 Local migrate tests

| Coverage |
|---|
| Fresh empty output dir → bot.db copied + drive_files mirrored |
| Re-run on populated dir → idempotent, skip existing files |
| `--dry-run` → no files written, print plan |
| `--users 1,2` → only users 1, 2 data |
| Missing env vars → fail-fast with error |

**Target:** ≥ 35 test cases, all pass, 0 warnings.

### 14.5 Integration / staging smoke

- Tạo user test "ImportMe" trên local; ghi 5 notes + 2 wiki + 3 web conversations; gửi `xuat du lieu` qua Telegram; nhận Drive link; download → kiểm tra ZIP structure
- Trên staging mới (fresh DB): upload ZIP qua `/admin/import/preview` → preview hiện đúng stats → apply → user "ImportMe" hiện trong `xem danh sach user`; `liet ke` thấy 5 notes; `xem wiki` thấy 2 pages
- Chạy `tools/local_migrate.py --output /tmp/test_migrate` → verify `bot.db` size > 0 + `drive_files/` có content; re-run → no duplicate

---

## 15. Definition of Done

- [ ] `backup_engine.py` implement đủ `generate_export`, `parse_import`, `apply_import`
- [ ] Web routes hoạt động: self + admin export (download ZIP); admin import (preview + apply flow)
- [ ] Telegram commands: `xuat du lieu` self + admin variants; upload Drive + reply link
- [ ] `tools/local_migrate.py` chạy được standalone với args; output dir đúng layout
- [ ] `pytest` pass 100% (test count >= 350 — hiện ~314 + 35 mới)
- [ ] Smoke test (14.5) pass trên staging
- [ ] Audit events `data_export` / `data_import` xuất hiện trong `xem audit`
- [ ] Rate-limit hoạt động (test bằng 2 requests liên tiếp)
- [ ] Import rollback test: inject lỗi giữa apply → verify không có data nửa vời trong DB + Drive
- [ ] ROADMAP cập nhật: FR-6 status DONE; Decision Log thêm D1–D13
- [ ] `docs/architecture_vn.md` + `docs/architecture_en.md` cập nhật: header → FR-6; thêm `backup_engine.py` + `tools/local_migrate.py` vào File Layout; thêm web routes export/import vào Section 7; thêm audit events `data_*` vào taxonomy

---

## 16. Open Issues (resolve khi code)

1. **Name conflict policy** — Decision D5 nói reject; nhưng UX có thể tệ. Cân nhắc thêm option "import as <new_name>" với suffix `_imported` tự động. Quyết định khi implement (đề xuất giữ D5 đơn giản; thêm option nếu user cần).
2. **NoteStore/WikiStore API** — `read_file_by_id` đã có; nhưng có `upload_content(filename, bytes)` chưa? Cần check `drive_client.py` lúc implement; nếu chưa: thêm method.
3. **Wiki `_index.md` regeneration** — sau import wiki pages, có cần regenerate `_index.md` không? Có. Add to apply_import.
4. **Channel binding conflict** — Decision D3 nói "không export channel_bindings của user khác"; mỗi user chỉ export binding của mình. Nhưng import vào hệ thống có thể có chat_id binding conflict (user khác đã bind chat_id đó). Decision D3 hint skip + warn — đủ.
5. **Parent link many-to-many** — `parent_links_as_child` + `parent_links_as_parent` có thể overlap (vd user vừa là parent vừa là child trong cùng family). Cần dedup khi import.
6. **Local migrate Drive folder structure** — Drive có nested folder; CLI cần preserve relative path. Verify Drive API list trả `parents` đầy đủ.
7. **Audit log entries volume** — user lâu năm có thể có 10k+ audit entries; ZIP size ảnh hưởng. Cân nhắc cap last N=1000 entries hoặc filter theo `created_at > now - 1 year`. Quyết định khi implement.

---

## 17. Estimated Effort

| Sub | Effort |
|-----|--------|
| 6.1 BackupEngine (export) | 0.4 ngày |
| 6.2 ImportEngine | 0.4 ngày |
| 6.3 Web routes + import preview UI | 0.3 ngày |
| 6.4 Telegram commands | 0.15 ngày |
| 6.5 Local migrate CLI | 0.25 ngày |
| 6.6 Wiring + audit + rate-limit | 0.1 ngày |
| 6.7 Tests | 0.5 ngày |
| Doc + smoke test | 0.2 ngày |
| **Tổng** | **~2.3 ngày** |

---

## 18. Decisions Captured (sẽ thêm vào ROADMAP Decision Log khi merge)

| # | Topic | Decision |
|---|-------|----------|
| 76 | Backup encryption | KHÔNG encrypt (plaintext ZIP); gia đình tin nhau; encryption thêm complexity không xứng; migration path sang encrypted backup là additive (flag `--encrypted` sau) |
| 77 | Export scope | Chỉ data của 1 user; loại trừ phiên (web_sessions, elevation_sessions, sudo_attempts, pending_notifications, invite_codes, pending change requests) |
| 78 | Import ID strategy | Tạo mới với ID mới + remap qua id_map; KHÔNG cố preserve old IDs |
| 79 | Import conflict policy | Name conflict → reject (admin sửa source rồi re-upload); KHÔNG auto-rename |
| 80 | Drive file_id strategy | Import tạo file Drive mới (Drive cấp file_id mới); SQLite dùng id mới; KHÔNG cố preserve drive_file_id cũ |
| 81 | Audit log không restore | Export `audit_entries` chỉ để xem; import bỏ qua; ghi audit mới `data_import` |
| 82 | Telegram import out of scope v1 | Admin import qua web UI; Telegram bot file upload + parsing ZIP phức tạp không cần thiết |
| 83 | Local migrate = CLI standalone | `tools/local_migrate.py` chạy ngoài bot process; admin tay; chuẩn bị cho future local-only mode |
| 84 | BackupEngine concrete class | KHÔNG Protocol; chỉ 1 implementation (SQLite + Drive); Protocol overhead không cần |
| 85 | Streaming export, không file tạm | Generate ZIP vào BytesIO; Render free tier ephemeral filesystem; chấp nhận memory pressure ở scale gia đình |
| 86 | Rate-limit export | 1 export per user per 5 phút; in-memory tracking; chống abuse + Drive API quota |

---

**End of FR-6 Plan**

> Đọc cùng `docs/ROADMAP.md` Section 5 (FR-6 entry), Section 6 (Decision Log), và `docs/architecture_vn.md` Section 5 (Data Model) để hiểu data đang sống ở đâu.
