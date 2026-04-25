import os
from dotenv import load_dotenv

load_dotenv()

# Telegram
TELEGRAM_TOKEN    = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID  = os.getenv("TELEGRAM_CHAT_ID")

# Anthropic
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
MODEL             = os.getenv("MODEL", "claude-haiku-4-5-20251001")

# Google Drive
GDRIVE_FOLDER_ID    = os.getenv("GDRIVE_FOLDER_ID", "13jODlrZkSiSxR4aCr_zSlKL1qdfmvNjt")
CLAUDE_NOTES_FOLDER = os.getenv("CLAUDE_NOTES_FOLDER", "Claude-Notes")
CREDENTIALS_FILE    = os.getenv("CREDENTIALS_FILE", "credentials.json")

# Budget
BUDGET_LIMIT = float(os.getenv("BUDGET_LIMIT", "10.0"))
ALERT_80     = BUDGET_LIMIT * 0.80
ALERT_90     = BUDGET_LIMIT * 0.90

# File lưu tracking chi phí local
COST_FILE = "cost_tracker.json"
