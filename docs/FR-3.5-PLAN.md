# FR-3.5 — Privilege Elevation (sudo) — Detailed Implementation Plan

> **Status:** DRAFT — chờ review lần 2 trước khi execute
> **Created:** 2026-05-19
> **Branch:** sẽ tạo branch riêng từ `main` (theo Git workflow Section 3.5)

---

## 1. Goal

Cho phép tài khoản Telegram chạy role `manager` **nâng quyền tạm thời** lên `admin` khi cần
thao tác quản trị, thay vì phải dùng admin làm tài khoản mặc định.

Lý do: ở production, tài khoản Telegram chính của user sẽ là role `manager` (dùng hằng ngày).
Quyền admin chỉ cần khi thật sự thực hiện việc quản trị (thêm user, đặt quota, ...).

---

## 2. Context & Decisions

### 2.1 Hiện trạng

- Mỗi `chat_id` bind cứng với đúng 1 user qua `channel_bindings`. Không có khái niệm session/login.
- Bootstrap admin bind với `config.TELEGRAM_CHAT_ID` lúc khởi tạo.
- FR-2 đã có hạ tầng Argon2id (`argon2-cffi`, migration 008) nhưng **chưa expose qua lệnh**.

### 2.2 Quyết định nền tảng (chốt 2026-05-19 — Decision Log #57–59)

| # | Quyết định |
|---|-----------|
| D1 | sudo = **nâng role**, KHÔNG đổi danh tính. Override `role`→`admin` tạm thời; `id`/`name` giữ nguyên. |
| D2 | TTL phiên elevation = **15 phút**, hết hạn kiểu lazy (không cần cron). |
| D3 | Lệnh `sudo` chỉ role `manager` dùng được; cổng xác thực chính là **mật khẩu admin**. |
| D4 | Mật khẩu admin chỉ quản lý được từ **tài khoản natively-admin** (admin qua channel binding, không qua elevation). Đây vừa là đặt lần đầu vừa là cơ chế recovery — KHÔNG làm tính năng quên mật khẩu riêng. |
| D5 | Audit ghi đúng người thật (base user id), không phải admin được giả lập. |

### 2.3 "Natively-admin" vs "elevated-admin"

- **Natively-admin:** user có `role='admin'` trong DB, bind trực tiếp với chat_id. Không có phiên elevation.
- **Elevated-admin:** user role thật `manager`, đang có phiên elevation còn hạn → role bị override thành `admin`.
- Phân biệt: nếu `get_active_session(channel, chat_id)` trả None mà `role=='admin'` → natively-admin.

---

## 3. Scope Breakdown

| Sub | Tên | Mục đích |
|-----|-----|---------|
| 3.0 | Schema migration | `elevation_sessions` + `sudo_attempts` tables |
| 3.1 | Elevation store | CRUD phiên elevation + rate-limit thất bại |
| 3.2 | Password commands | `dat mat khau` — đặt/đổi mật khẩu admin (natively-admin only) |
| 3.3 | sudo commands | `sudo` / `thoat sudo` |
| 3.4 | Role resolution | `main.py` override role khi có phiên elevation còn hạn |
| 3.5 | Message hygiene | Bot tự xóa message chứa mật khẩu (`delete_message` trên ChannelAdapter) |
| 3.6 | Audit | Log mọi lần elevate / drop / fail |
| 3.7 | Documentation | ROADMAP cập nhật + Decision Log (đã làm: #57–60) |

---

## 4. File Changes Summary

### 4.1 New files

| # | File | Purpose |
|---|------|---------|
| 1 | `db/migrations/013_elevation.sql` | `elevation_sessions` + `sudo_attempts` tables |
| 2 | `elevation_store.py` | `SqliteElevationStore` — phiên elevation + rate-limit |
| 3 | `tests/test_elevation_store.py` | Elevation store tests |
| 4 | `tests/test_sudo.py` | Command flow tests (elevate/drop/expiry/rate-limit) |

### 4.2 Edited files

| # | File | Change |
|---|------|--------|
| 5 | `config.py` | `SUDO_TTL_MINUTES=15`, `SUDO_MAX_FAILS=5`, `SUDO_LOCKOUT_MINUTES=15` |
| 6 | `interfaces.py` | Protocol `ElevationStore`; `delete_message` trên `ChannelAdapter`; password method trên `UserStore` nếu chưa có |
| 7 | `user_store.py` | `set_password` / `verify_password` (nếu FR-2 chưa có) |
| 8 | `channel_telegram.py` | Implement `delete_message` (Telegram `deleteMessage` API) |
| 9 | `main.py` | Sau `find_by_channel`: check elevation → `dataclasses.replace(user, role="admin")`; wire `ElevationStore` vào `CoreDeps` |
| 10 | `core_handler.py` | Lệnh `dat mat khau`, `sudo`, `thoat sudo`; command table; dispatch; help; `toi la ai` hiện trạng thái elevation |

---

## 5. Database Schema

### 5.1 `013_elevation.sql`

```sql
CREATE TABLE elevation_sessions (
    channel       TEXT NOT NULL,
    chat_id       TEXT NOT NULL,
    base_user_id  INTEGER NOT NULL REFERENCES users(id),
    started_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at    DATETIME NOT NULL,
    PRIMARY KEY (channel, chat_id)
);

CREATE TABLE sudo_attempts (
    channel       TEXT NOT NULL,
    chat_id       TEXT NOT NULL,
    failed_count  INTEGER NOT NULL DEFAULT 0,
    locked_until  DATETIME,
    last_attempt_at DATETIME,
    PRIMARY KEY (channel, chat_id)
);
```

- Phiên elevation: 1 dòng / (channel, chat_id). Re-elevate → refresh `expires_at`.
- `sudo_attempts`: đếm số lần nhập sai; `failed_count >= SUDO_MAX_FAILS` → set `locked_until`.
- Reset `failed_count` về 0 khi sudo thành công.

---

## 6. Elevation Flow

### 6.1 Nâng quyền — `sudo: <mật khẩu>`

```
1. Kiểm tra role base user == 'manager'  (khác → từ chối)
2. Kiểm tra sudo_attempts.locked_until    (đang khóa → từ chối, báo còn bao lâu)
3. verify_password(<mật khẩu>) với password_hash của (các) user role 'admin'
   - Sai  → failed_count += 1; nếu đạt ngưỡng → set locked_until; audit("sudo_fail")
   - Đúng → reset failed_count; UPSERT elevation_sessions (expires_at = now + 15m);
            audit("sudo_elevate"); báo user
4. Bot xóa message chứa `sudo: <mật khẩu>` (delete_message)
```

### 6.2 Resolution trong `main.py`

```
base_user = find_by_channel(channel, chat_id)
session   = elevation_store.get_active_session(channel, chat_id)  # đã lọc hết hạn
if session is not None:
    user = dataclasses.replace(base_user, role="admin")
else:
    user = base_user
```

### 6.3 Hạ quyền — `thoat sudo`

Xóa dòng `elevation_sessions`. Audit `sudo_drop`.

### 6.4 Hết hạn

Lazy: `get_active_session` chỉ trả dòng có `expires_at > now`. Dòng quá hạn coi như không tồn tại
(có thể dọn định kỳ sau, không bắt buộc trong FR-3.5).

---

## 7. Commands

| Command ID | VN prefix | Mô tả | Ai dùng được |
|------------|-----------|-------|--------------|
| `DAT_MAT_KHAU` | `dat mat khau: ` | Đặt/đổi mật khẩu admin | Chỉ tài khoản **natively-admin** |
| `SUDO` | `sudo: ` | Nâng quyền lên admin 15 phút | Role `manager` |
| `THOAT_SUDO` | `thoat sudo` | Hạ quyền ngay | Bất kỳ ai đang elevated |

- `toi la ai` (FR-3 đã có) bổ sung dòng trạng thái: đang elevated hay không, còn bao lâu.
- Mật khẩu hiển thị rõ trong chat → bot xóa message ngay sau xử lý (cả khi đúng lẫn sai).

---

## 8. Security

| Biện pháp | Chi tiết |
|-----------|---------|
| Rate-limit | `SUDO_MAX_FAILS=5` lần sai → khóa `SUDO_LOCKOUT_MINUTES=15` phút |
| Xóa message mật khẩu | `delete_message` qua Telegram `deleteMessage` API; áp dụng cho cả `sudo` và `dat mat khau` |
| Hashing | Argon2id (argon2-cffi) — hạ tầng FR-2 |
| Audit | `sudo_elevate`, `sudo_drop`, `sudo_fail`, `sudo_locked`, `password_set` — log stdout (FR-4 sẽ có audit table chính thức) |
| TTL | Auto hết hạn 15 phút — không phụ thuộc user nhớ thoát |
| Gating | `sudo` chỉ role `manager`; `dat mat khau` chỉ natively-admin |

---

## 9. Risk & Impact

**Risk: `medium`**

- **Role resolution đổi ở `main.py`** — mọi message đi qua bước check elevation. Lỗi logic = hoặc rò quyền admin, hoặc chặn nhầm. Mitigation: test kỹ matrix elevated/expired/none.
- **Mật khẩu plaintext qua Telegram** — mitigated bằng delete_message; vẫn có rủi ro nếu xóa fail (log warning).
- **Rate-limit bypass** — nếu attacker đổi chat_id... nhưng chat_id do Telegram cấp, không tự đặt được. Chấp nhận.
- **`delete_message` thêm vào ChannelAdapter** — interface change, adapter khác (Discord/Web tương lai) phải implement; để no-op mặc định nếu chưa hỗ trợ.

---

## 10. Dependencies

- **Python packages:** không thêm (argon2-cffi đã có từ FR-2).
- **Env vars:** không thêm.
- **Migrations:** 013 chạy tự động lúc startup.
- **FR phụ thuộc:** FR-2 (Argon2id), FR-3.

---

## 11. Test Plan

### 11.1 Unit tests

| Module | Coverage |
|--------|----------|
| `elevation_store` | create/get/drop session; hết hạn không trả; rate-limit đếm + khóa + reset |
| `sudo` flow | elevate đúng mật khẩu; sai mật khẩu tăng count; khóa sau N lần; `thoat sudo`; non-manager bị từ chối |

### 11.2 Integration / staging smoke

- Manager `sudo` đúng mật khẩu → chạy được `them user` trong 15 phút.
- Sau 15 phút → thao tác admin bị từ chối, báo hết hạn.
- `thoat sudo` → mất quyền ngay.
- Nhập sai 5 lần → bị khóa 15 phút.
- `dat mat khau` từ tài khoản manager (elevated) → bị từ chối; từ tài khoản natively-admin → OK.
- Message `sudo: <mk>` bị bot xóa khỏi chat.

---

## 12. Definition of Done

- [ ] Migration 013 apply OK trên fresh + existing DB
- [ ] `pytest` pass 100%
- [ ] Smoke test (11.2) pass trên staging
- [ ] Mật khẩu không bao giờ lưu plaintext; message mật khẩu bị xóa
- [ ] Audit log đầy đủ các sự kiện sudo
- [ ] ROADMAP Section 5 + Decision Log cập nhật (#57–60 đã có)

---

## 13. Open Issues (resolve khi code)

1. **FR-2 password methods** — xác nhận `user_store` đã có `set_password`/`verify_password` chưa; nếu chưa thì thêm.
2. **Nhiều admin** — verify mật khẩu với tất cả user role `admin`, khớp dòng nào thì elevate. Gia đình hiện tại 1 admin nên không phức tạp.
3. **Dọn phiên hết hạn** — lazy expiry là đủ; có cần job dọn rác `elevation_sessions` không, hay để FR sau.

---

**End of FR-3.5 Plan**

> Đọc cùng `docs/ROADMAP.md` Section 5 (FR-3.5 entry) và Decision Log #57–60.
