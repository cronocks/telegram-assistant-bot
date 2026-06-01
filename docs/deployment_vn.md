# Hướng Dẫn Deploy

Tài liệu này hướng dẫn deploy Telegram Claude Bot lên **Render.com** với **Cloudflare R2** làm backend backup SQLite, và setup môi trường phát triển local.

---

## 1. Yêu Cầu Trước Khi Bắt Đầu

Bạn cần tài khoản và credentials từ các dịch vụ sau:

| Dịch vụ | Mục đích | Free tier? |
|---------|---------|-----------|
| [Telegram](https://telegram.org) | Tạo bot qua BotFather | ✅ |
| [Anthropic](https://console.anthropic.com) | Claude API key | Trả theo dùng |
| [Google](https://console.cloud.google.com) | OAuth cho Google Drive | ✅ |
| [Cloudflare](https://cloudflare.com) | R2 object storage (backup SQLite) | ✅ (10 GB miễn phí) |
| [Render](https://render.com) | Docker hosting | ✅ (750 h/tháng) |

---

## 2. Tạo Telegram Bot

### Tạo bot mới
1. Mở Telegram → tìm **@BotFather**
2. Gửi `/newbot` và làm theo hướng dẫn (đặt tên và username)
3. BotFather gửi **bot token** — copy bằng cách bấm vào code block để copy toàn bộ

**Định dạng token:** `1234567890:ABCDefGhIJKlmNoPQRsTUVwxyZ-abc123`
(số + dấu hai chấm + chuỗi 35 ký tự)

### Tạo bot riêng cho staging
Lặp lại các bước trên để lấy token thứ hai cho staging. Hai service dùng chung một bot token sẽ xung đột — Telegram chỉ gửi message đến một webhook tại một thời điểm.

### Lấy chat ID của bạn
1. Bắt đầu chat với bot
2. Gọi: `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates`
3. Nhắn bất kỳ tin gì cho bot, sau đó gọi `getUpdates` lần nữa
4. Tìm `message.chat.id` trong response — đây là `TELEGRAM_CHAT_ID` của bạn

> **Lưu ý quan trọng về URL Telegram API:** Chữ `bot` là phần literal bắt buộc trong mọi URL API.
> Đúng: `https://api.telegram.org/bot8735442823:ABC.../getMe`
> Sai: `https://api.telegram.org/8735442823:ABC.../getMe` (thiếu chữ `bot`)

---

## 3. Cài Đặt Cloudflare R2

### Tạo bucket
1. Cloudflare dashboard → **R2 Object Storage** → **Create bucket**
2. Đặt tên (ví dụ: `telegram-bot-db`)
3. Giữ nguyên các cài đặt khác

### Lấy Account ID và Endpoint
1. Trang R2 overview → tìm **Account ID** ở thanh bên phải (chuỗi hex 32 ký tự)
2. Endpoint tương thích S3 của bạn: `https://<ACCOUNT_ID>.r2.cloudflarestorage.com`

### Tạo API token
1. R2 overview → **Manage R2 API Tokens** → **Create API token**
2. Đặt tên (ví dụ: `litestream-bot`)
3. Permissions: **Object Read & Write**
4. Scope: **Specific bucket** → chọn bucket vừa tạo
5. Bấm **Create API Token**
6. Copy **Access Key ID** và **Secret Access Key** — **chỉ hiển thị một lần duy nhất**

---

## 4. Cài Đặt Google OAuth

Bot dùng Google Drive để lưu ghi chú và wiki.

1. Tạo Google Cloud project và bật **Drive API**
2. Tạo OAuth 2.0 credentials (loại Desktop app), tải `credentials.json` về
3. Đặt `credentials.json` vào thư mục gốc của project
4. Chạy script setup:
   ```bash
   python oauth_setup.py
   ```
5. Làm theo hướng dẫn trên browser để cấp quyền Drive
6. Script in ra chuỗi base64 — copy làm giá trị `GOOGLE_OAUTH_TOKEN_B64`

> Token được lưu trong biến môi trường thay vì file để hoạt động trên ephemeral filesystem của Render.

---

## 5. Biến Môi Trường

Copy `.env.example` thành `.env` (local), `.env.staging`, hoặc `.env.production` rồi điền giá trị.

| Biến | Bắt buộc | Mặc định | Mô tả |
|------|----------|---------|-------|
| `TELEGRAM_TOKEN` | ✅ | — | Bot token từ BotFather |
| `TELEGRAM_CHAT_ID` | ✅ | — | Chat ID Telegram của bạn (bootstrap admin) |
| `ANTHROPIC_API_KEY` | ✅ | — | Anthropic API key |
| `MODEL` | ✅ | — | Model Claude (ví dụ: `claude-haiku-4-5-20251001`) |
| `GOOGLE_OAUTH_TOKEN_B64` | ✅ | — | Google OAuth token mã hóa base64 |
| `OWNER_EMAIL` | ✅ | — | Email Google để transfer quyền sở hữu Drive |
| `APP_ENV` | ✅ | `local` | `local` \| `staging` \| `production` |
| `SQLITE_PATH` | ✅ (non-local) | `./bot.db` | Đường dẫn file SQLite. Bắt buộc cho staging/production. |
| `LITESTREAM_BUCKET` | staging/prod | — | Tên R2 bucket |
| `LITESTREAM_DB_NAME` | staging/prod | — | Đường dẫn trong bucket: `staging/bot.db` hoặc `production/bot.db` |
| `LITESTREAM_ENDPOINT` | staging/prod | — | `https://<account-id>.r2.cloudflarestorage.com` |
| `LITESTREAM_ACCESS_KEY_ID` | staging/prod | — | Access Key ID của R2 API token |
| `LITESTREAM_SECRET_ACCESS_KEY` | staging/prod | — | Secret Access Key của R2 API token |
| `WEB_SECRET_KEY` | ✅ (non-local) | — | Secret key cho session cookie web. Tạo bằng: `python -c "import secrets; print(secrets.token_urlsafe(48))"`. Dùng key khác nhau cho mỗi môi trường. |
| `WEB_SESSION_TTL_DAYS` | ❌ | `7` | Thời gian tồn tại session cookie web (ngày) |
| `GDRIVE_FOLDER_ID` | ❌ | `""` | Ghim ghi chú vào folder Drive cụ thể theo ID |
| `CLAUDE_NOTES_FOLDER` | ❌ | `Claude-Notes` | Tên subfolder ghi chú |
| `WIKI_SUBFOLDER` | ❌ | `Wiki` | Tên subfolder wiki |
| `BUDGET_LIMIT` | ❌ | `10.0` | Giới hạn chi phí LLM tháng (USD) |
| `MAX_FILES_PER_HOUR` | ❌ | `20` | Giới hạn tốc độ ghi Drive |
| `ENABLE_OWNERSHIP_TRANSFER` | ❌ | `true` | Cho phép chuyển quyền Drive |

---

## 6. Deploy Lên Render

### 6.1 Tạo hai service (production + staging)

Lặp lại cho từng môi trường:

1. Render dashboard → **New** → **Web Service**
2. Kết nối repo GitHub
3. Chọn **Branch**: `main` (production) hoặc `dev` (staging)
4. Chọn **Runtime**: **Docker**
5. **Dockerfile Path**: `./Dockerfile`
6. **Plan**: Free
7. **Name**: ví dụ `telegram-bot-prod` / `telegram-bot-staging`

### 6.2 Cài đặt biến môi trường

Trong tab **Environment** của từng service:

**Cách A — Import từ file (nhanh hơn):**
- Bấm **Add from .env file**
- Dán nội dung file `.env.production` hoặc `.env.staging` vào

**Cách B — Điền thủ công:**
Điền từng biến theo bảng tham chiếu ở trên. Các biến khác nhau giữa 2 môi trường:

| Biến | Production | Staging |
|------|-----------|---------|
| `APP_ENV` | `production` | `staging` |
| `SQLITE_PATH` | `/data/bot.db` | `/data/bot.db` |
| `LITESTREAM_DB_NAME` | `production/bot.db` | `staging/bot.db` |
| `TELEGRAM_TOKEN` | token bot production | token bot staging (bot khác!) |

> `LITESTREAM_BUCKET`, `LITESTREAM_ENDPOINT`, `LITESTREAM_ACCESS_KEY_ID`, và `LITESTREAM_SECRET_ACCESS_KEY` **giống nhau** cho cả 2 service — dùng chung R2 bucket nhưng ghi vào path khác nhau.

### 6.3 Cài đặt webhook

Sau khi service live, đăng ký webhook với Telegram bằng cách mở URL này trong browser:

```
https://api.telegram.org/bot<YOUR_TOKEN>/setWebhook?url=https://<your-service>.onrender.com/webhook
```

Response mong đợi:
```json
{"ok": true, "result": true, "description": "Webhook was set"}
```

Kiểm tra webhook đã đúng chưa:
```
https://api.telegram.org/bot<YOUR_TOKEN>/getWebhookInfo
```

### 6.4 Lưu ý free tier

- Render free tier có **750 giờ instance/tháng** dùng chung cho tất cả service
- **Chỉ gắn UptimeRobot cho production** — giữ staging thức sẽ cạn kiệt giờ miễn phí
- Staging sẽ ngủ sau ~15 phút không hoạt động; tin nhắn đầu sau khi ngủ có thể chậm vài giây

---

## 7. Phát Triển Local

```bash
# Clone
git clone https://github.com/<your-username>/telegram-claude-bot.git
cd telegram-claude-bot

# Cài dependencies
pip install -r requirements.txt

# Copy và điền file env
cp .env.example .env
# Sửa .env — điền TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, ANTHROPIC_API_KEY, v.v.

# Chạy
python main.py
```

Với môi trường local, `APP_ENV=local` là mặc định. SQLite ghi vào `./bot.db` và không dùng Litestream.

Chạy tests:
```bash
pytest
```

---

## 8. Trình Tự Khởi Động

Khi Docker container khởi động (`docker-entrypoint.sh`):

1. **`litestream restore`** — tải snapshot SQLite mới nhất từ R2
   - Lần đầu boot (bucket trống): bỏ qua, bot tạo DB mới và chạy migrations
   - Các lần boot sau: restore trạng thái WAL mới nhất
2. **`litestream replicate`** — bắt đầu stream WAL lên R2 (~1 giây lag), sau đó khởi động `uvicorn`

Kiểm tra boot thành công trong Render Logs:
```
time=... msg="no matching backups found"   ← chỉ lần đầu boot
time=... msg="initialized db" path=/data/bot.db
[bot] DB ready — admin: <tên> (id=1)
[bot] Drive OK: {...}
INFO: Application startup complete.
==> Your service is live 🎉
```

---

## 9. Xử Lý Sự Cố

### Bot không phản hồi sau khi deploy

**Kiểm tra 1 — Service có đang chạy không?**
Render dashboard → Logs — tìm `Application startup complete` và `Your service is live`.

**Kiểm tra 2 — Webhook có trỏ đúng URL không?**
```
https://api.telegram.org/bot<TOKEN>/getWebhookInfo
```
Field `url` phải khớp với URL service Render của bạn + `/webhook`.
Nếu trỏ sai URL, chạy lại `setWebhook`.

**Kiểm tra 3 — Service có đang ngủ không?**
Free tier ngủ sau 15 phút. Nhắn tin và chờ ~10 giây để wake up.

### `getWebhookInfo` / `getMe` trả về 404 hoặc 401

| Lỗi | Nguyên nhân | Cách sửa |
|-----|------------|---------|
| `404 Not Found` | Thiếu tiền tố `bot` trong URL | Dùng `https://api.telegram.org/bot<TOKEN>/getMe` |
| `401 Unauthorized` | Token không hợp lệ hoặc đã bị revoke | Lấy token mới từ BotFather → `/mybots` → API Token |

> Luôn copy token bằng cách bấm vào code block của BotFather — bôi đen tay dễ bị thiếu ký tự hoặc dính xuống dòng.

### Litestream restore thất bại khi boot

Kiểm tra tất cả biến `LITESTREAM_*` đã set đúng trên Render. Lỗi hay gặp nhất là `LITESTREAM_ENDPOINT` thiếu tiền tố `https://` hoặc sai Account ID.

### Google Drive token hết hạn

Token trong `GOOGLE_OAUTH_TOKEN_B64` có refresh token tự làm mới. Nếu Drive bị lỗi, chạy lại `oauth_setup.py` trên máy local, lấy chuỗi base64 mới, và cập nhật biến env trên Render.

### `APP_ENV` chưa set cho staging/production

Nếu `SQLITE_PATH` bị thiếu trên môi trường non-local, app sẽ fail ngay với:
```
RuntimeError: APP_ENV=staging requires SQLITE_PATH to be set explicitly
```
Đặt `SQLITE_PATH=/data/bot.db` trong Render environment.
