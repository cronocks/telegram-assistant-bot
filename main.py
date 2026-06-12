"""main.py — FastAPI entry point.

Thin wiring layer: instantiates adapters (LLM, NoteStore, WikiStore, Channel),
bundles them into CoreDeps, and routes Telegram webhook payloads through the
channel-agnostic core handler.

All business logic lives in core_handler.py. Adapter selection (e.g. swapping
Drive → local FS or Anthropic → Ollama) happens here without touching the core.
"""
import dataclasses
import traceback
from contextlib import asynccontextmanager

import uvicorn
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, Request
from fastapi.templating import Jinja2Templates

from audit import SqliteAuditLog
from security_headers import SecurityHeadersMiddleware
from rate_limit import RateLimitMiddleware
from csrf import CSRFMiddleware
from channel_telegram import TelegramAdapter
from web_channel import WebChannelAdapter
from web_session_store import SqliteWebSessionStore
from web_router import router as web_router, init_web_router
import scheduled_jobs
from claude_client import AnthropicLLM
import config
from config import TELEGRAM_CHAT_ID, TELEGRAM_TOKEN
from deps import CoreDeps
from core_handler import handle_message
from cost_monitor import check_and_alert
from db.migrations import run_migrations
from drive_client import DriveNoteStore
from elevation_store import SqliteElevationStore
from memory_store import SqliteMemoryStore
from note_index import SqliteNoteIndex
from notification_store import SqliteNotificationStore
from notification_service import NotificationService
from user_store import SqliteUserStore
from wiki_client import DriveWikiStore
from backup_engine import BackupEngine
from task_store import SqliteTaskStore
from reminder_store import SqliteReminderStore
from reminder_engine import ReminderEngine
from task_parser import TaskParser
from anniversary_store import SqliteAnniversaryStore
from anniversary_engine import AnniversaryEngine
from family_store import SqliteFamilyStore
from burial_store import SqliteBurialStore
from category_store import SqliteCategoryStore
from credit_card_store import SqliteCreditCardStore
from ledger_store import SqliteLedgerStore
from budget_store import SqliteBudgetStore
from ledger_parser import LedgerParser
from ledger_reports import LedgerReports
from db.connection import get_connection

_REGISTER_PREFIXES = ("dang ky:", "đăng ký:")

scheduler = AsyncIOScheduler()


# ═══════════════════════════════════════════════════════════════════════════════
# Adapter wiring — single point where concrete implementations are chosen
# ═══════════════════════════════════════════════════════════════════════════════

llm = AnthropicLLM()
notes = DriveNoteStore()
wiki = DriveWikiStore(llm=llm)
channel = TelegramAdapter(token=TELEGRAM_TOKEN, allowed_chat_id=TELEGRAM_CHAT_ID)
web_channel = WebChannelAdapter()
user_store = SqliteUserStore()
note_index = SqliteNoteIndex()
memory_store = SqliteMemoryStore()
elevation_store = SqliteElevationStore()
audit = SqliteAuditLog()
web_session_store = SqliteWebSessionStore()
from web_conversation_store import SqliteWebConversationStore
web_conv_store = SqliteWebConversationStore()
backup_engine = BackupEngine(
    user_store=user_store,
    note_index=note_index,
    memory_store=memory_store,
    web_conversation_store=web_conv_store,
    audit=audit,
    notes=notes,
    wiki=wiki,
)
notif_store = SqliteNotificationStore()
notif_service = NotificationService(
    store=notif_store,
    audit=audit,
    user_store=user_store,
    channels={"telegram": channel},
)
task_store = SqliteTaskStore()
reminder_store = SqliteReminderStore()
reminder_engine = ReminderEngine(
    task_store=task_store,
    reminder_store=reminder_store,
    user_store=user_store,
    notification_service=notif_service,
    audit=audit,
)
task_parser = TaskParser()
anniversary_store = SqliteAnniversaryStore()
family_store = SqliteFamilyStore()
burial_store = SqliteBurialStore()
anniversary_engine = AnniversaryEngine(
    anniv_store=anniversary_store,
    user_store=user_store,
    notification_service=notif_service,
    audit=audit,
    conn=get_connection(),
    burial_store=burial_store,
)
category_store = SqliteCategoryStore()
ledger_store = SqliteLedgerStore()
budget_store = SqliteBudgetStore()
credit_card_store = SqliteCreditCardStore()
ledger_parser = LedgerParser()
ledger_reports = LedgerReports(ledger_store, budget_store)

deps = CoreDeps(
    llm=llm,
    notes=notes,
    wiki=wiki,
    channel=channel,
    user_store=user_store,
    note_index=note_index,
    memory_store=memory_store,
    elevation_store=elevation_store,
    audit=audit,
    notification_service=notif_service,
    web_session_store=web_session_store,
    backup_engine=backup_engine,
    task_store=task_store,
    reminder_store=reminder_store,
    reminder_engine=reminder_engine,
    task_parser=task_parser,
    anniversary_store=anniversary_store,
    anniversary_engine=anniversary_engine,
    category_store=category_store,
    ledger_store=ledger_store,
    budget_store=budget_store,
    ledger_parser=ledger_parser,
    ledger_reports=ledger_reports,
    credit_card_store=credit_card_store,
    family_store=family_store,
    burial_store=burial_store,
)

# CoreDeps for web channel — same adapters, different channel adapter.
web_deps = CoreDeps(
    llm=llm,
    notes=notes,
    wiki=wiki,
    channel=web_channel,
    user_store=user_store,
    note_index=note_index,
    memory_store=memory_store,
    elevation_store=elevation_store,
    audit=audit,
    notification_service=notif_service,
    web_session_store=web_session_store,
    backup_engine=backup_engine,
    task_store=task_store,
    reminder_store=reminder_store,
    reminder_engine=reminder_engine,
    task_parser=task_parser,
    anniversary_store=anniversary_store,
    anniversary_engine=anniversary_engine,
    category_store=category_store,
    ledger_store=ledger_store,
    budget_store=budget_store,
    ledger_parser=ledger_parser,
    ledger_reports=ledger_reports,
    credit_card_store=credit_card_store,
    family_store=family_store,
    burial_store=burial_store,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Lifespan & app
# ═══════════════════════════════════════════════════════════════════════════════

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Route Drive security audit events into the SQLite audit log (FR — security
    # hardening), so they surface via `xem audit` instead of only stdout.
    import security as _security
    _security.set_audit_sink(audit)

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

    # Backfill: index existing Drive files that have no SQLite row yet.
    try:
        _user_store = SqliteUserStore()
        admins = [u for u in _user_store.list_users() if u.role == "admin"]
        if admins:
            note_files = notes.list_recent_files(limit=None)
            wiki_files = wiki.list_pages()
            inserted = note_index.backfill(note_files, wiki_files, admins[0].id)
            print(f"[bot] Note index backfill complete — {inserted} new rows inserted")
        else:
            print("[bot] Note index backfill skipped — no admin user found")
    except Exception as e:
        print(f"[bot] Note index backfill ERROR (non-fatal): {e}")
        traceback.print_exc()

    app.state.web_deps = web_deps

    scheduler.add_job(check_and_alert, "interval", hours=6, id="cost_alert")
    scheduled_jobs.register_jobs(scheduler, deps)
    scheduler.start()
    print(
        "[bot] Scheduler started — cost check every 6h; "
        "FR-4 recycle purge (180d) + auto-purge-18 at 03:00 UTC+7"
    )
    yield
    scheduler.shutdown()


templates = Jinja2Templates(directory="templates")

app = FastAPI(lifespan=lifespan)
app.add_middleware(SecurityHeadersMiddleware, hsts=(config.APP_ENV != "local"))
app.add_middleware(
    CSRFMiddleware,
    exempt_paths={"/webhook"},               # Telegram bot — no browser cookies
    secure=(config.APP_ENV != "local"),
)
app.add_middleware(
    RateLimitMiddleware,
    # /login gets a tight cap: 10 attempts / 60 s per IP (brute-force deterrent
    # at the transport layer — distinct from the per-user lockout in elevation_store).
    path_limits={"/login": (10, 60)},
    default_limit=(120, 60),  # 120 unsafe requests / 60 s per IP for all other routes
)
app.include_router(web_router)

init_web_router(
    templates=templates,
    web_channel=web_channel,
    session_store=web_session_store,
    user_store=user_store,
    audit=audit,
    elevation_store=elevation_store,
    conv_store=web_conv_store,
    backup_engine=backup_engine,
    task_store=task_store,
    anniversary_store=anniversary_store,
    anniversary_engine=anniversary_engine,
    ledger_store=ledger_store,
    category_store=category_store,
    budget_store=budget_store,
    ledger_reports=ledger_reports,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Webhook & health
# ═══════════════════════════════════════════════════════════════════════════════

async def _handle_registration(msg_chat_id: str, code: str) -> None:
    """Handle dang ky command before user auth check."""
    user = user_store.consume_invite_code(code, "telegram", msg_chat_id)
    if user is None:
        await channel.send(
            msg_chat_id,
            "Mã mời không hợp lệ, đã dùng, hoặc đã hết hạn. Liên hệ admin để lấy mã mới.",
            use_markdown=False,
        )
        return
    await channel.send(
        msg_chat_id,
        f"Chào mừng *{user.name}*! Tài khoản đã được kích hoạt (role: {user.role}).\n"
        f"Gõ /start để xem danh sách lệnh.",
    )


@app.post("/webhook")
async def webhook(request: Request):
    try:
        data = await request.json()
        msg = channel.parse_webhook(data)
        if not msg:
            return {"ok": True}

        # Pre-auth: allow registration command for unbound users.
        low = msg.text.strip().lower()
        for prefix in _REGISTER_PREFIXES:
            if low.startswith(prefix):
                code = msg.text.strip()[len(prefix):].strip()
                await _handle_registration(msg.chat_id, code)
                return {"ok": True}

        # All other commands require a registered, active user.
        user = user_store.find_by_channel(msg.channel, msg.chat_id)
        if user is None:
            await channel.send(
                msg.chat_id,
                "Bạn chưa được đăng ký. Liên hệ admin để được mời, "
                "sau đó dùng lệnh: dang ky: <mã mời>",
                use_markdown=False,
            )
            return {"ok": True}
        if not user.is_active:
            await channel.send(
                msg.chat_id, "Tài khoản của bạn đã bị vô hiệu hóa.", use_markdown=False,
            )
            return {"ok": True}

        # Apply active sudo elevation, if any, by overriding role to admin.
        # Base identity (id, name) stays the same; audit and ownership remain
        # attached to the real user.
        session = elevation_store.get_active_session(msg.channel, msg.chat_id)
        if session is not None and user.role != "admin":
            user = dataclasses.replace(user, role="admin")

        await handle_message(msg, user, deps)
    except Exception as e:
        traceback.print_exc()
    return {"ok": True}


@app.api_route("/", methods=["GET", "HEAD"])
async def root():
    return {"status": "running", "version": "v5-features"}


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
