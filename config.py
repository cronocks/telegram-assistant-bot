import os
from dotenv import load_dotenv

load_dotenv()

# ── Telegram ──────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN    = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID  = os.getenv("TELEGRAM_CHAT_ID")

# ── Anthropic ─────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
MODEL             = os.getenv("MODEL", "claude-haiku-4-5-20251001")

# ── Google Drive ──────────────────────────────────────────────────────────────
GDRIVE_FOLDER_ID    = os.getenv("GDRIVE_FOLDER_ID", "")
CLAUDE_NOTES_FOLDER = os.getenv("CLAUDE_NOTES_FOLDER", "Claude-Notes")

# ── OAuth & Ownership Transfer ────────────────────────────────────────────────
# OAuth token (base64) — được tạo bởi oauth_setup.py
# (Không cần khai báo ở đây, drive_client.py đọc trực tiếp từ env)

# Email tài khoản chính — để transfer ownership tới
OWNER_EMAIL = os.getenv("OWNER_EMAIL", "thangnm.it@gmail.com")

# Bật/tắt ownership transfer (default: bật)
ENABLE_OWNERSHIP_TRANSFER = os.getenv("ENABLE_OWNERSHIP_TRANSFER", "true").lower() == "true"

# ── Security ──────────────────────────────────────────────────────────────────
# Giới hạn số file tạo mỗi giờ (chống lạm dụng)
MAX_FILES_PER_HOUR = int(os.getenv("MAX_FILES_PER_HOUR", "20"))

# ── Budget ────────────────────────────────────────────────────────────────────
BUDGET_LIMIT = float(os.getenv("BUDGET_LIMIT", "10.0"))
ALERT_80     = BUDGET_LIMIT * 0.80
ALERT_90     = BUDGET_LIMIT * 0.90

# File lưu tracking chi phí local
COST_FILE = "cost_tracker.json"
