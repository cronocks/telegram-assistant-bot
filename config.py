import os
from dotenv import load_dotenv

load_dotenv()

# ── Telegram ──────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN    = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID  = os.getenv("TELEGRAM_CHAT_ID")

# ── Anthropic ─────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
MODEL             = os.getenv("MODEL")

# ── Google Drive ──────────────────────────────────────────────────────────────
GDRIVE_FOLDER_ID    = os.getenv("GDRIVE_FOLDER_ID", "")
CLAUDE_NOTES_FOLDER = os.getenv("CLAUDE_NOTES_FOLDER", "Claude-Notes")

# ── OAuth & Ownership Transfer ────────────────────────────────────────────────
OWNER_EMAIL = os.getenv("OWNER_EMAIL")
ENABLE_OWNERSHIP_TRANSFER = os.getenv("ENABLE_OWNERSHIP_TRANSFER", "true").lower() == "true"

# ── Security ──────────────────────────────────────────────────────────────────
MAX_FILES_PER_HOUR = int(os.getenv("MAX_FILES_PER_HOUR", "20"))

# ── Budget ────────────────────────────────────────────────────────────────────
BUDGET_LIMIT = float(os.getenv("BUDGET_LIMIT", "10.0"))
ALERT_80     = BUDGET_LIMIT * 0.80
ALERT_90     = BUDGET_LIMIT * 0.90

# Local file for cost tracking
COST_FILE = "cost_tracker.json"

# ── Timezone ──────────────────────────────────────────────────────────────────
# UTC+7 — Hanoi. Fixed offset to avoid tzdata dependency on container.
TIMEZONE_OFFSET_HOURS = 7

# ── Pending choice state ──────────────────────────────────────────────────────
PENDING_CHOICE_TIMEOUT_SEC = 60   # seconds before pending state expires

# ── List notes ────────────────────────────────────────────────────────────────
LIST_RECENT_LIMIT  = 10           # max files shown in recent list
FUZZY_SCAN_LIMIT   = 200          # max files scanned for fuzzy match
FUZZY_SHOW_LIMIT   = 10           # max results shown when multiple matches

# ── Wiki ──────────────────────────────────────────────────────────────────────
WIKI_SUBFOLDER          = os.getenv("WIKI_SUBFOLDER", "Wiki")
MAX_WIKI_UPDATES        = int(os.getenv("MAX_WIKI_UPDATES", "3"))
MAX_WIKI_PAGES_CONTEXT  = int(os.getenv("MAX_WIKI_PAGES_CONTEXT", "2"))
MAX_WIKI_CONTEXT_CHARS  = int(os.getenv("MAX_WIKI_CONTEXT_CHARS", "400"))

# ── Environment & SQLite ──────────────────────────────────────────────────────
# APP_ENV selects the deployment environment: 'local' | 'staging' | 'production'.
APP_ENV = os.getenv("APP_ENV", "local")

if APP_ENV == "local":
    SQLITE_PATH = os.getenv("SQLITE_PATH", "./bot.db")
else:
    # staging/production must set SQLITE_PATH explicitly so a misconfigured
    # deploy fails fast instead of silently writing to the local dev database.
    SQLITE_PATH = os.getenv("SQLITE_PATH")
    if not SQLITE_PATH:
        raise RuntimeError(
            f"APP_ENV={APP_ENV} requires SQLITE_PATH to be set explicitly"
        )
