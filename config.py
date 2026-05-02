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
OWNER_EMAIL = os.getenv("OWNER_EMAIL", "thangnm.it@gmail.com")
ENABLE_OWNERSHIP_TRANSFER = os.getenv("ENABLE_OWNERSHIP_TRANSFER", "true").lower() == "true"

# ── Security ──────────────────────────────────────────────────────────────────
MAX_FILES_PER_HOUR = int(os.getenv("MAX_FILES_PER_HOUR", "20"))

# ── Budget ────────────────────────────────────────────────────────────────────
BUDGET_LIMIT = float(os.getenv("BUDGET_LIMIT", "10.0"))
ALERT_80     = BUDGET_LIMIT * 0.80
ALERT_90     = BUDGET_LIMIT * 0.90

# File lưu tracking chi phí local
COST_FILE = "cost_tracker.json"

# ── Timezone (mới) ────────────────────────────────────────────────────────────
# UTC+7 — Hà Nội. Dùng offset cố định để không phụ thuộc tzdata trên container.
TIMEZONE_OFFSET_HOURS = 7

# ── State management cho pending choice (mới) ────────────────────────────────
PENDING_CHOICE_TIMEOUT_SEC = 60   # state pending hết hạn sau N giây

# ── List notes (mới) ─────────────────────────────────────────────────────────
LIST_RECENT_LIMIT  = 10           # liệt kê 10 file gần nhất
FUZZY_SCAN_LIMIT   = 200          # scan tối đa N file để fuzzy match
FUZZY_SHOW_LIMIT   = 10           # hiện tối đa N kết quả khi nhiều match

# ── Wiki ──────────────────────────────────────────────────────────────────────
WIKI_SUBFOLDER          = os.getenv("WIKI_SUBFOLDER", "Wiki")
MAX_WIKI_UPDATES        = 3       # tối đa N topics mỗi lần ingest
MAX_WIKI_PAGES_CONTEXT  = 2       # tối đa N wiki pages đưa vào context QA
MAX_WIKI_CONTEXT_CHARS  = 400     # cắt mỗi wiki page ở N chars khi làm context
