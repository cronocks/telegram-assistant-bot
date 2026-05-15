"""main.py — FastAPI entry point.

Thin wiring layer: instantiates adapters (LLM, NoteStore, WikiStore, Channel),
bundles them into CoreDeps, and routes Telegram webhook payloads through the
channel-agnostic core handler.

All business logic lives in core_handler.py. Adapter selection (e.g. swapping
Drive → local FS or Anthropic → Ollama) happens here without touching the core.
"""
import traceback
from contextlib import asynccontextmanager

import uvicorn
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, Request

from channel_telegram import TelegramAdapter
from claude_client import AnthropicLLM
from config import TELEGRAM_CHAT_ID, TELEGRAM_TOKEN
from core_handler import CoreDeps, handle_message
from cost_monitor import check_and_alert
from db.migrations import run_migrations
from drive_client import DriveNoteStore
from user_store import SqliteUserStore
from wiki_client import DriveWikiStore

scheduler = AsyncIOScheduler()


# ═══════════════════════════════════════════════════════════════════════════════
# Adapter wiring — single point where concrete implementations are chosen
# ═══════════════════════════════════════════════════════════════════════════════

llm = AnthropicLLM()
notes = DriveNoteStore()
wiki = DriveWikiStore(llm=llm)
channel = TelegramAdapter(token=TELEGRAM_TOKEN, allowed_chat_id=TELEGRAM_CHAT_ID)

deps = CoreDeps(llm=llm, notes=notes, wiki=wiki, channel=channel)


# ═══════════════════════════════════════════════════════════════════════════════
# Lifespan & app
# ═══════════════════════════════════════════════════════════════════════════════

@asynccontextmanager
async def lifespan(app: FastAPI):
    # DB: run migrations then bootstrap first admin user
    try:
        run_migrations()
        user_store = SqliteUserStore()
        admin = user_store.bootstrap_admin()
        if admin:
            print(f"[bot] DB ready — admin: {admin.name} (id={admin.id})")
        else:
            print("[bot] DB ready — no bootstrap (TELEGRAM_CHAT_ID not set or users exist)")
    except Exception as e:
        print(f"[bot] DB ERROR at startup: {e}")
        traceback.print_exc()

    print("[bot] Testing Google Drive connection at startup...")
    try:
        result = notes.test_connection()
        print(f"[bot] Drive OK: {result}")
    except Exception as e:
        print(f"[bot] Drive ERROR at startup: {e}")
        traceback.print_exc()

    scheduler.add_job(check_and_alert, "interval", hours=6, id="cost_alert")
    scheduler.start()
    print("[bot] Scheduler started — cost check every 6h")
    yield
    scheduler.shutdown()


app = FastAPI(lifespan=lifespan)


# ═══════════════════════════════════════════════════════════════════════════════
# Webhook & health
# ═══════════════════════════════════════════════════════════════════════════════

@app.post("/webhook")
async def webhook(request: Request):
    try:
        data = await request.json()
        msg = channel.parse_webhook(data)
        if not msg:
            return {"ok": True}

        # Single-user authorization (chat_id lock). FR-2 replaces this with a
        # user registry across all channels.
        if not channel.is_authorized(msg):
            print(f"[security] Rejected message from unauthorized chat_id={msg.chat_id}")
            return {"ok": True}

        await handle_message(msg, deps)
    except Exception as e:
        traceback.print_exc()
    return {"ok": True}


@app.api_route("/", methods=["GET", "HEAD"])
async def root():
    return {"status": "running", "version": "v5-features"}


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
