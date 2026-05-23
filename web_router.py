"""web_router.py — FastAPI router for the web UI channel (FR-5 / FR-5.5).

Routes:
  GET  /               → redirect to /chat (or /login if not authenticated)
  GET  /login          → login page
  POST /login          → authenticate, set session cookie
  POST /logout         → revoke session, clear cookie
  GET  /setup-password → force-reset page
  POST /setup-password → set new password + create full session

  -- Chat UI (FR-5.5: conversation-aware) --
  GET  /chat                         → new-chat shell (sidebar + empty messages)
  GET  /chat/<conv_id>               → open existing conversation
  POST /chat/send                    → lazy-create conversation + send first message
  POST /chat/<conv_id>/send          → send message into existing conversation
  GET  /chat/stream                  → SSE (new conv, before conv_id known)
  GET  /chat/stream?conversation_id= → SSE per conversation

  -- REST API (JSON) --
  GET   /api/conversations                    → list user's conversations
  GET   /api/conversations/<id>/messages      → list messages (ownership check)
  PATCH /api/conversations/<id>               → rename conversation
  GET   /api/conversations/search?q=          → LIKE search across user's messages

  -- Admin stealth-read (FR-5.5.6) --
  GET  /admin/users                           → list all users (admin only)
  GET  /admin/users/<id>/conversations        → conversations of a minor child (admin only)
  GET  /admin/conversations/<id>              → read-only view + stealth_read audit (admin only)

Security:
  - HttpOnly + SameSite=Lax cookies (+ Secure in staging/production)
  - Brute-force: reuses sudo_attempts table (channel="web"), 5 fails → 15-min lock
  - Audit: web_login / web_logout / web_login_failed / web_password_set
           web_conversation_created / web_conversation_renamed
           stealth_read_web_conversation
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Cookie, Depends, File, Form, Query, Request, Response, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sse_starlette.sse import EventSourceResponse

import config
from backup_engine import BackupEngine, ExportError, ImportFormatError, ParsedImport
from interfaces import (
    AuditLog, ElevationStore, User, UserStore,
    WebConversationStore, WebSessionStore,
)
from web_channel import WebChannelAdapter

logger = logging.getLogger(__name__)

router = APIRouter()

# Populated by main.py at startup via init_web_router().
_templates: Jinja2Templates | None = None
_web_channel: WebChannelAdapter | None = None
_session_store: WebSessionStore | None = None
_user_store: UserStore | None = None
_audit: AuditLog | None = None
_elevation_store: ElevationStore | None = None
_conv_store: WebConversationStore | None = None
_backup_engine: BackupEngine | None = None

# Import preview token store: token → {parsed: ParsedImport, expires_at: datetime}
_import_tokens: dict[str, dict] = {}
_IMPORT_TOKEN_TTL = timedelta(minutes=5)

# Download token store: token → {zip_bytes, filename, expires_at}
# Allows IDM/download managers to re-request the same file without hitting cooldown.
_download_tokens: dict[str, dict] = {}
_DOWNLOAD_TOKEN_TTL = timedelta(seconds=60)

_COOKIE_NAME = "web_session"
_SESSION_MAX_AGE = config.WEB_SESSION_TTL_DAYS * 86_400  # seconds


def init_web_router(
    templates: Jinja2Templates,
    web_channel: WebChannelAdapter,
    session_store: WebSessionStore,
    user_store: UserStore,
    audit: AuditLog,
    elevation_store: ElevationStore,
    conv_store: WebConversationStore,
    backup_engine: BackupEngine | None = None,
) -> None:
    """Wire dependencies into this router (called once from main.py lifespan)."""
    global _templates, _web_channel, _session_store, _user_store
    global _audit, _elevation_store, _conv_store, _backup_engine
    _templates = templates
    _web_channel = web_channel
    _session_store = session_store
    _user_store = user_store
    _audit = audit
    _elevation_store = elevation_store
    _conv_store = conv_store
    _backup_engine = backup_engine
    # Also inject conv_store into the channel adapter for bot-reply persistence
    web_channel.set_conv_store(conv_store)


# ── Import token helpers ───────────────────────────────────────────────────────

def _cleanup_expired_tokens() -> None:
    """Remove stale import tokens (called before issuing new ones)."""
    now = datetime.now(timezone.utc)
    expired = [t for t, v in _import_tokens.items() if v["expires_at"] < now]
    for t in expired:
        del _import_tokens[t]


def _store_import_token(parsed: ParsedImport) -> str:
    """Store a ParsedImport keyed by a fresh UUID token. Returns the token."""
    _cleanup_expired_tokens()
    token = str(uuid.uuid4())
    _import_tokens[token] = {
        "parsed": parsed,
        "expires_at": datetime.now(timezone.utc) + _IMPORT_TOKEN_TTL,
    }
    return token


def _consume_import_token(token: str) -> ParsedImport | None:
    """Retrieve and remove a ParsedImport by token. Returns None if expired/missing."""
    entry = _import_tokens.pop(token, None)
    if entry is None:
        return None
    if entry["expires_at"] < datetime.now(timezone.utc):
        return None
    return entry["parsed"]


# ── Download token helpers ────────────────────────────────────────────────────

def _store_download_token(zip_bytes: bytes, filename: str) -> str:
    """Store a ZIP payload keyed by a fresh UUID. Returns the token."""
    now = datetime.now(timezone.utc)
    expired = [t for t, v in _download_tokens.items() if v["expires_at"] < now]
    for t in expired:
        del _download_tokens[t]
    token = str(uuid.uuid4())
    _download_tokens[token] = {
        "zip_bytes": zip_bytes,
        "filename": filename,
        "expires_at": now + _DOWNLOAD_TOKEN_TTL,
    }
    return token


def _consume_download_token(token: str) -> dict | None:
    """Retrieve a download entry by token. Returns None if expired/missing.

    Token is NOT removed on read — it expires naturally after TTL (60s).
    This allows download managers (e.g. IDM) to re-request the same URL
    multiple times within the TTL window without getting 410 Gone.
    """
    entry = _download_tokens.get(token)
    if entry is None:
        return None
    if entry["expires_at"] < datetime.now(timezone.utc):
        _download_tokens.pop(token, None)
        return None
    return entry


# ── Cookie helpers ─────────────────────────────────────────────────────────────

def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=_COOKIE_NAME,
        value=token,
        max_age=_SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=(config.APP_ENV != "local"),
    )


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(key=_COOKIE_NAME, httponly=True, samesite="lax")


# ── Auth helpers ───────────────────────────────────────────────────────────────

def _resolve_user(token: str | None) -> User | None:
    if not token or _session_store is None or _user_store is None:
        return None
    user_id = _session_store.find_active(token)
    if user_id is None:
        return None
    user = _user_store.get_user_by_id(user_id)
    if user is None or not user.is_active:
        return None
    return user


def _get_conv_or_403(conv_id: int, user: User) -> dict | None:
    """Return conversation dict if it belongs to user, else None (caller returns 403)."""
    assert _conv_store is not None
    conv = _conv_store.get(conv_id)
    if conv is None or conv["user_id"] != user.id:
        return None
    return conv


# ── Root ───────────────────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
async def index(web_session: str | None = Cookie(default=None)):
    user = _resolve_user(web_session)
    if user:
        return RedirectResponse(url="/chat", status_code=303)
    return RedirectResponse(url="/login", status_code=303)


# ── Auth routes ────────────────────────────────────────────────────────────────

@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, web_session: str | None = Cookie(default=None)):
    if _resolve_user(web_session):
        return RedirectResponse(url="/chat", status_code=303)
    assert _templates is not None
    return _templates.TemplateResponse(request, "login.html", {"error": None})


@router.post("/login")
async def login(
    request: Request,
    response: Response,
    username: str = Form(...),
    password: str = Form(...),
):
    assert _templates is not None
    assert _user_store is not None
    assert _session_store is not None
    assert _audit is not None
    assert _elevation_store is not None

    def _render_error(msg: str):
        return _templates.TemplateResponse(
            request, "login.html", {"error": msg}, status_code=400
        )

    user = _user_store.find_by_username_or_name(username.strip())
    if user is None:
        return _render_error("Tên đăng nhập hoặc mật khẩu không đúng.")

    locked, locked_until = _elevation_store.is_locked("web", str(user.id))
    if locked:
        return _render_error(f"Tài khoản tạm khóa đến {locked_until}. Thử lại sau.")

    if _user_store.get_password_hash(user.id) is None:
        return _render_error("Mật khẩu web chưa được thiết lập. Liên hệ admin.")

    if not _user_store.check_password(user.id, password):
        result = _elevation_store.record_failure(
            "web", str(user.id),
            max_fails=config.SUDO_MAX_FAILS,
            lockout_minutes=config.SUDO_LOCKOUT_MINUTES,
        )
        _audit.log(user.id, "web_login_failed", "user", str(user.id))
        if result.get("locked"):
            return _render_error(
                f"Sai mật khẩu quá {config.SUDO_MAX_FAILS} lần. Tài khoản tạm khóa 15 phút."
            )
        return _render_error("Tên đăng nhập hoặc mật khẩu không đúng.")

    _elevation_store.reset_failures("web", str(user.id))
    must_change = _user_store.get_must_change_password(user.id)
    token = _session_store.create(user.id)
    _audit.log(user.id, "web_login", "user", str(user.id))

    redirect_url = "/setup-password" if must_change else "/chat"
    redirect = RedirectResponse(url=redirect_url, status_code=303)
    _set_session_cookie(redirect, token)
    return redirect


@router.post("/logout")
async def logout(web_session: str | None = Cookie(default=None)):
    assert _session_store is not None
    assert _audit is not None

    redirect = RedirectResponse(url="/login", status_code=303)
    _clear_session_cookie(redirect)

    if web_session:
        user_id = _session_store.find_active(web_session)
        _session_store.revoke(web_session)
        if user_id is not None:
            _audit.log(user_id, "web_logout", "user", str(user_id))

    return redirect


@router.get("/settings/password", response_class=HTMLResponse)
async def settings_password_page(request: Request, web_session: str | None = Cookie(default=None)):
    """Self-service password change page for logged-in users."""
    user = _resolve_user(web_session)
    if user is None:
        return RedirectResponse(url="/login", status_code=303)
    assert _user_store is not None
    if _user_store.get_must_change_password(user.id):
        return RedirectResponse(url="/setup-password", status_code=303)
    assert _templates is not None
    return _templates.TemplateResponse(
        request, "settings_password.html", {"user": user, "error": None, "success": False},
    )


@router.post("/settings/password", response_class=HTMLResponse)
async def settings_password_post(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    web_session: str | None = Cookie(default=None),
):
    """Handle self-service password change: verify current → set new → audit."""
    user = _resolve_user(web_session)
    if user is None:
        return RedirectResponse(url="/login", status_code=303)
    assert _user_store is not None
    assert _audit is not None
    assert _templates is not None

    def _render(error: str | None = None, success: bool = False):
        return _templates.TemplateResponse(
            request, "settings_password.html",
            {"user": user, "error": error, "success": success},
            status_code=400 if error else 200,
        )

    if len(new_password) < 8:
        return _render("Mật khẩu mới phải có ít nhất 8 ký tự.")
    if new_password != confirm_password:
        return _render("Mật khẩu xác nhận không khớp.")
    if not _user_store.check_password(user.id, current_password):
        return _render("Mật khẩu hiện tại không đúng.")

    _user_store.set_password(user.id, new_password)
    _user_store.set_must_change_password(user.id, False)
    _audit.log(user.id, "web_password_changed", "user", str(user.id))
    return _render(success=True)


@router.get("/setup-password", response_class=HTMLResponse)
async def setup_password_page(request: Request, web_session: str | None = Cookie(default=None)):
    user = _resolve_user(web_session)
    if user is None:
        return RedirectResponse(url="/login", status_code=303)
    assert _templates is not None
    return _templates.TemplateResponse(request, "setup_password.html", {"error": None})


@router.post("/setup-password")
async def setup_password(
    request: Request,
    response: Response,
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    web_session: str | None = Cookie(default=None),
):
    assert _templates is not None
    assert _user_store is not None
    assert _session_store is not None
    assert _audit is not None

    user = _resolve_user(web_session)
    if user is None:
        return RedirectResponse(url="/login", status_code=303)

    def _render_error(msg: str):
        return _templates.TemplateResponse(
            request, "setup_password.html", {"error": msg}, status_code=400
        )

    if len(new_password) < 8:
        return _render_error("Mật khẩu phải có ít nhất 8 ký tự.")
    if new_password != confirm_password:
        return _render_error("Mật khẩu xác nhận không khớp.")

    _user_store.set_password(user.id, new_password)
    _user_store.set_must_change_password(user.id, False)
    _audit.log(user.id, "web_password_set", "user", str(user.id))

    if web_session:
        _session_store.revoke(web_session)
    new_token = _session_store.create(user.id)
    redirect = RedirectResponse(url="/chat", status_code=303)
    _set_session_cookie(redirect, new_token)
    return redirect


# ── Chat UI routes ─────────────────────────────────────────────────────────────

@router.get("/chat", response_class=HTMLResponse)
async def chat_page(request: Request, web_session: str | None = Cookie(default=None)):
    user = _resolve_user(web_session)
    if user is None:
        return RedirectResponse(url="/login", status_code=303)

    assert _user_store is not None
    if _user_store.get_must_change_password(user.id):
        return RedirectResponse(url="/setup-password", status_code=303)

    assert _templates is not None
    assert _conv_store is not None
    conversations = _conv_store.list_for_user(user.id)
    return _templates.TemplateResponse(
        request, "chat.html",
        {"user": user, "conversations": conversations, "active_conv": None, "messages": []},
    )


# ── SSE stream — must be declared before /chat/{conv_id} to prevent ambiguous match ──

@router.get("/chat/stream")
async def chat_stream(
    request: Request,
    conversation_id: int | None = Query(default=None),
    web_session: str | None = Cookie(default=None),
):
    """SSE endpoint: streams JSON events to the browser via EventSource.

    conversation_id query param is required for FR-5.5 (queue per conv).
    If omitted (legacy / pre-conv-id state), a temporary key is used.
    """
    user = _resolve_user(web_session)
    if user is None:
        return RedirectResponse(url="/login", status_code=303)

    assert _web_channel is not None
    assert _conv_store is not None

    # Determine SSE key
    if conversation_id is not None:
        conv = _get_conv_or_403(conversation_id, user)
        if conv is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        queue_key = str(conversation_id)
    else:
        # Pending new conversation — use a temporary user-scoped key until
        # the frontend receives conversation_id from /chat/send and reconnects.
        queue_key = f"pending_{user.id}"

    q = _web_channel.connect(queue_key)

    async def event_generator():
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    raw = await asyncio.wait_for(q.get(), timeout=30.0)
                    yield {"data": raw}
                except asyncio.TimeoutError:
                    yield {"comment": "keepalive"}
        finally:
            _web_channel.disconnect(queue_key)

    return EventSourceResponse(event_generator())


@router.get("/chat/{conv_id}", response_class=HTMLResponse)
async def chat_conversation_page(
    conv_id: int,
    request: Request,
    web_session: str | None = Cookie(default=None),
):
    user = _resolve_user(web_session)
    if user is None:
        return RedirectResponse(url="/login", status_code=303)

    assert _user_store is not None
    if _user_store.get_must_change_password(user.id):
        return RedirectResponse(url="/setup-password", status_code=303)

    assert _conv_store is not None
    conv = _get_conv_or_403(conv_id, user)
    if conv is None:
        return RedirectResponse(url="/chat", status_code=303)

    conversations = _conv_store.list_for_user(user.id)
    messages = _conv_store.list_messages(conv_id)
    assert _templates is not None
    return _templates.TemplateResponse(
        request, "chat.html",
        {"user": user, "conversations": conversations, "active_conv": conv, "messages": messages},
    )


# ── Chat send routes ───────────────────────────────────────────────────────────

async def _generate_title_bg(conv_id: int, user_text: str, web_deps) -> None:
    """Background task: generate title after first exchange and push SSE update."""
    assert _conv_store is not None
    try:
        title, _ = web_deps.llm.generate_chat_title(
            user_text[:300],
            next(
                (m["text"] for m in reversed(_conv_store.list_messages(conv_id)) if m["role"] == "bot"),
                "",
            ),
        )
    except Exception:
        logger.exception("title gen failed for conv_id=%s, using fallback", conv_id)
        title = user_text[:40].strip() + ("…" if len(user_text) > 40 else "")

    if not title:
        return

    written = _conv_store.set_title_if_null(conv_id, title)
    if written and _web_channel is not None:
        _web_channel.push_title_update(str(conv_id), title)


async def _handle_and_maybe_title(msg, user, web_deps, conv_id: int, user_text: str) -> None:
    """Wrapper: run handle_message then trigger title gen on first exchange."""
    from core_handler import handle_message
    await handle_message(msg, user, web_deps)
    assert _conv_store is not None
    if _conv_store.count_messages(conv_id) == 2:  # user msg + first bot reply
        await _generate_title_bg(conv_id, user_text, web_deps)


@router.post("/chat/send")
async def send_message_new(
    request: Request,
    text: str = Form(...),
    web_session: str | None = Cookie(default=None),
):
    """Lazy-create a conversation then send the first message.

    Returns JSON {"conversation_id": N} so the frontend can update the URL
    and reconnect SSE with the new conversation_id.
    """
    from interfaces import ChannelMessage

    user = _resolve_user(web_session)
    if user is None:
        return JSONResponse({"error": "unauthenticated"}, status_code=401)

    assert _conv_store is not None
    assert _audit is not None

    clean_text = text.strip()
    if not clean_text:
        return JSONResponse({"error": "empty message"}, status_code=400)

    # Lazy-create conversation
    conv_id = _conv_store.create(user.id)
    _audit.log(user.id, "web_conversation_created", "web_conversation", str(conv_id))

    # Persist user message
    _conv_store.add_message(conv_id, "user", clean_text)

    # Route through core_handler; wrap to trigger title gen after first exchange
    msg = ChannelMessage(channel="web", chat_id=str(conv_id), text=clean_text)
    web_deps = getattr(request.app.state, "web_deps", None)
    if web_deps is None:
        logger.error("web_deps not wired — check main.py lifespan")
        return JSONResponse({"error": "server error"}, status_code=500)

    asyncio.create_task(_handle_and_maybe_title(msg, user, web_deps, conv_id, clean_text))
    return JSONResponse({"conversation_id": conv_id}, status_code=200)


@router.post("/chat/{conv_id}/send")
async def send_message(
    conv_id: int,
    request: Request,
    text: str = Form(...),
    web_session: str | None = Cookie(default=None),
):
    """Send a message into an existing conversation."""
    from interfaces import ChannelMessage
    from core_handler import handle_message

    user = _resolve_user(web_session)
    if user is None:
        return JSONResponse({"error": "unauthenticated"}, status_code=401)

    assert _conv_store is not None
    conv = _get_conv_or_403(conv_id, user)
    if conv is None:
        return JSONResponse({"error": "not found"}, status_code=404)

    clean_text = text.strip()
    if not clean_text:
        return JSONResponse({"error": "empty message"}, status_code=400)

    _conv_store.add_message(conv_id, "user", clean_text)

    msg = ChannelMessage(channel="web", chat_id=str(conv_id), text=clean_text)
    web_deps = getattr(request.app.state, "web_deps", None)
    if web_deps is None:
        logger.error("web_deps not wired — check main.py lifespan")
        return JSONResponse({"error": "server error"}, status_code=500)

    asyncio.create_task(handle_message(msg, user, web_deps))
    return HTMLResponse("", status_code=204)


# ── REST API ───────────────────────────────────────────────────────────────────

@router.get("/api/conversations")
async def api_list_conversations(web_session: str | None = Cookie(default=None)):
    """Return JSON list of current user's conversations (newest first)."""
    user = _resolve_user(web_session)
    if user is None:
        return JSONResponse({"error": "unauthenticated"}, status_code=401)

    assert _conv_store is not None
    convs = _conv_store.list_for_user(user.id)
    return JSONResponse(convs)


@router.get("/api/conversations/search")
async def api_search_conversations(
    q: str = Query(default=""),
    web_session: str | None = Cookie(default=None),
):
    """LIKE search across user's messages. Returns [{conv_id, conv_title, ...}]."""
    user = _resolve_user(web_session)
    if user is None:
        return JSONResponse({"error": "unauthenticated"}, status_code=401)

    assert _conv_store is not None
    if not q.strip():
        return JSONResponse([])
    results = _conv_store.search(user.id, q.strip())
    return JSONResponse(results)


@router.get("/api/conversations/{conv_id}/messages")
async def api_get_messages(
    conv_id: int,
    web_session: str | None = Cookie(default=None),
):
    """Return JSON list of messages for a conversation (ownership check)."""
    user = _resolve_user(web_session)
    if user is None:
        return JSONResponse({"error": "unauthenticated"}, status_code=401)

    assert _conv_store is not None
    conv = _get_conv_or_403(conv_id, user)
    if conv is None:
        return JSONResponse({"error": "not found"}, status_code=404)

    messages = _conv_store.list_messages(conv_id)
    return JSONResponse(messages)


@router.patch("/api/conversations/{conv_id}")
async def api_rename_conversation(
    conv_id: int,
    request: Request,
    web_session: str | None = Cookie(default=None),
):
    """Rename a conversation. Body: {"title": "..."}"""
    user = _resolve_user(web_session)
    if user is None:
        return JSONResponse({"error": "unauthenticated"}, status_code=401)

    assert _conv_store is not None
    assert _audit is not None

    conv = _get_conv_or_403(conv_id, user)
    if conv is None:
        return JSONResponse({"error": "not found"}, status_code=404)

    try:
        body = await request.json()
        new_title = str(body.get("title", "")).strip()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)

    if not new_title:
        return JSONResponse({"error": "title cannot be empty"}, status_code=400)

    old_title = conv.get("title") or ""
    _conv_store.rename(conv_id, new_title)
    _audit.log(
        user.id,
        "web_conversation_renamed",
        "web_conversation",
        str(conv_id),
        {"old": old_title, "new": new_title},
    )
    return JSONResponse({"ok": True, "title": new_title})


# ── Admin stealth-read routes (FR-5.5.6) ──────────────────────────────────────

def _require_admin(web_session: str | None) -> "User | HTMLResponse":
    """Return the authenticated admin User or a 403/redirect response."""
    user = _resolve_user(web_session)
    if user is None:
        return RedirectResponse(url="/login", status_code=303)
    if not user.is_admin:
        return HTMLResponse("403 Forbidden", status_code=403)
    return user


@router.get("/admin/users", response_class=HTMLResponse)
async def admin_users_list(
    request: Request,
    web_session: str | None = Cookie(default=None),
):
    """Admin: list all users, annotated with minor-child eligibility."""
    from acl import _is_minor_child

    result = _require_admin(web_session)
    if not isinstance(result, User):
        return result
    admin = result

    assert _user_store is not None
    assert _templates is not None

    all_users = _user_store.list_users()
    users_annotated = [
        {"user": u, "is_minor_child": _is_minor_child(u.id, _user_store)}
        for u in all_users
    ]
    return _templates.TemplateResponse(
        request, "admin_users.html",
        {"user": admin, "users": users_annotated},
    )


@router.get("/admin/users/{target_id}/conversations", response_class=HTMLResponse)
async def admin_user_conversations(
    target_id: int,
    request: Request,
    web_session: str | None = Cookie(default=None),
):
    """Admin: list conversations of a minor child (ACL-gated)."""
    from acl import _is_minor_child

    result = _require_admin(web_session)
    if not isinstance(result, User):
        return result
    admin = result

    assert _user_store is not None
    assert _conv_store is not None
    assert _templates is not None

    target_user = _user_store.get_user_by_id(target_id)
    if target_user is None:
        return HTMLResponse("404 Not found", status_code=404)

    if not _is_minor_child(target_id, _user_store):
        return HTMLResponse("403 Forbidden — target is not an eligible minor child", status_code=403)

    conversations = _conv_store.admin_list_for_user(target_id)
    return _templates.TemplateResponse(
        request, "admin_conversations.html",
        {"user": admin, "target_user": target_user, "conversations": conversations},
    )


@router.get("/admin/conversations/{conv_id}", response_class=HTMLResponse)
async def admin_conversation_view(
    conv_id: int,
    request: Request,
    web_session: str | None = Cookie(default=None),
):
    """Admin: read-only view of a conversation. Emits stealth_read audit log."""
    from acl import _is_minor_child

    result = _require_admin(web_session)
    if not isinstance(result, User):
        return result
    admin = result

    assert _conv_store is not None
    assert _user_store is not None
    assert _audit is not None
    assert _templates is not None

    conv = _conv_store.get(conv_id)
    if conv is None:
        return HTMLResponse("404 Not found", status_code=404)

    if not _is_minor_child(conv["user_id"], _user_store):
        return HTMLResponse("403 Forbidden — conversation owner is not an eligible minor child", status_code=403)

    target_user = _user_store.get_user_by_id(conv["user_id"])
    messages = _conv_store.list_messages(conv_id)

    _audit.log(
        admin.id,
        "stealth_read_web_conversation",
        "web_conversation",
        str(conv_id),
        {"target_user_id": conv["user_id"]},
    )

    return _templates.TemplateResponse(
        request, "admin_conversation_view.html",
        {"user": admin, "target_user": target_user, "conv": conv, "messages": messages},
    )


# ── Export routes (FR-6) ───────────────────────────────────────────────────────

def _zip_response(zip_bytes: bytes, filename: str) -> Response:
    """Return a file-download Response for a ZIP archive."""
    safe_name = filename.replace('"', "_")
    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}"'},
    )


def _export_filename(user_name: str) -> str:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in user_name)
    return f"export_{safe}_{ts}.zip"


@router.get("/settings/export")
async def self_export(web_session: str | None = Cookie(default=None)):
    """Self-export: generate ZIP, store under a one-time token, redirect to download URL.

    Two-step approach prevents download managers (e.g. IDM) from hitting the
    rate-limit on their mandatory second request to the same URL.
    """
    user = _resolve_user(web_session)
    if user is None:
        return RedirectResponse(url="/login", status_code=303)

    if _backup_engine is None:
        return HTMLResponse("Backup chưa được cấu hình.", status_code=503)

    remaining = _backup_engine.export_cooldown_remaining(user.id)
    if remaining > 0:
        return HTMLResponse(
            f"Rate limit: chờ {remaining} giây trước khi export lần tiếp theo.",
            status_code=429,
        )

    try:
        zip_bytes, _manifest = _backup_engine.generate_export(user.id)
    except ExportError as exc:
        logger.warning("Self export failed for user %s: %s", user.id, exc)
        return HTMLResponse(f"Export thất bại: {exc}", status_code=500)

    filename = _export_filename(user.name)
    token = _store_download_token(zip_bytes, filename)
    return RedirectResponse(url=f"/settings/export/download?token={token}", status_code=303)


@router.get("/settings/export/download")
async def self_export_download(token: str):
    """Serve the pre-generated ZIP identified by a one-time download token (TTL 60s)."""
    entry = _consume_download_token(token)
    if entry is None:
        return HTMLResponse(
            "Link tải đã hết hạn hoặc không hợp lệ. Vui lòng thử lại.",
            status_code=410,
        )
    return _zip_response(entry["zip_bytes"], entry["filename"])


@router.get("/admin/users/{target_id}/export")
async def admin_export_user(
    target_id: int,
    web_session: str | None = Cookie(default=None),
):
    """Admin: export data for any user by ID."""
    result = _require_admin(web_session)
    if not isinstance(result, User):
        return result
    admin = result

    if _backup_engine is None:
        return HTMLResponse("Backup chưa được cấu hình.", status_code=503)

    assert _user_store is not None
    target = _user_store.get_user_by_id(target_id)
    if target is None:
        return HTMLResponse("404 User không tồn tại.", status_code=404)

    remaining = _backup_engine.export_cooldown_remaining(target_id)
    if remaining > 0:
        return HTMLResponse(
            f"Rate limit: chờ {remaining} giây (cooldown chung với mọi export của user này).",
            status_code=429,
        )

    try:
        zip_bytes, manifest = _backup_engine.generate_export(target_id)
    except ExportError as exc:
        logger.warning("Admin export failed for user %s by admin %s: %s", target_id, admin.id, exc)
        return HTMLResponse(f"Export thất bại: {exc}", status_code=500)

    # Override audit delivery field to reflect admin-triggered export.
    assert _audit is not None
    _audit.log(
        actor_user_id=admin.id,
        action="data_export",
        target_type="user",
        target_id=target_id,
        payload={"size_bytes": len(zip_bytes), "delivery": "web_admin"},
    )

    return _zip_response(zip_bytes, _export_filename(target.name))


# ── Import routes (FR-6) ───────────────────────────────────────────────────────

@router.get("/admin/import", response_class=HTMLResponse)
async def admin_import_page(
    request: Request,
    web_session: str | None = Cookie(default=None),
):
    """Admin: render import form."""
    result = _require_admin(web_session)
    if not isinstance(result, User):
        return result

    assert _templates is not None
    return _templates.TemplateResponse(
        request, "import.html",
        {"user": result, "state": "upload", "preview": None, "result": None, "error": None},
    )


@router.post("/admin/import/preview", response_class=HTMLResponse)
async def admin_import_preview(
    request: Request,
    zip_file: UploadFile = File(...),
    web_session: str | None = Cookie(default=None),
):
    """Admin: upload a ZIP, parse it, and render a preview with stats + warnings."""
    result = _require_admin(web_session)
    if not isinstance(result, User):
        return result

    if _backup_engine is None:
        return HTMLResponse("Backup chưa được cấu hình.", status_code=503)

    assert _templates is not None

    zip_bytes = await zip_file.read()
    if len(zip_bytes) > 100 * 1024 * 1024:
        return _templates.TemplateResponse(
            request, "import.html",
            {"user": result, "state": "upload", "preview": None, "result": None,
             "error": "File quá lớn (tối đa 100 MB)."},
            status_code=413,
        )

    try:
        parsed = _backup_engine.parse_import(zip_bytes)
    except ImportFormatError as exc:
        return _templates.TemplateResponse(
            request, "import.html",
            {"user": result, "state": "upload", "preview": None, "result": None,
             "error": f"ZIP không hợp lệ: {exc}"},
            status_code=400,
        )

    token = _store_import_token(parsed)
    preview = {
        "manifest": parsed.manifest,
        "warnings": parsed.warnings,
        "token": token,
    }
    return _templates.TemplateResponse(
        request, "import.html",
        {"user": result, "state": "preview", "preview": preview, "result": None, "error": None},
    )


@router.post("/admin/import/apply", response_class=HTMLResponse)
async def admin_import_apply(
    request: Request,
    token: str = Form(...),
    web_session: str | None = Cookie(default=None),
):
    """Admin: apply a previously-previewed import using the one-time token."""
    result = _require_admin(web_session)
    if not isinstance(result, User):
        return result

    if _backup_engine is None:
        return HTMLResponse("Backup chưa được cấu hình.", status_code=503)

    assert _templates is not None

    parsed = _consume_import_token(token)
    if parsed is None:
        return _templates.TemplateResponse(
            request, "import.html",
            {"user": result, "state": "upload", "preview": None, "result": None,
             "error": "Token hết hạn hoặc không hợp lệ. Vui lòng upload lại file ZIP."},
            status_code=400,
        )

    try:
        import_result = _backup_engine.apply_import(parsed, admin_user_id=result.id)
    except Exception as exc:
        logger.exception("Import apply failed: %s", exc)
        return _templates.TemplateResponse(
            request, "import.html",
            {"user": result, "state": "upload", "preview": None, "result": None,
             "error": f"Import thất bại: {exc}"},
            status_code=500,
        )

    return _templates.TemplateResponse(
        request, "import.html",
        {"user": result, "state": "result", "preview": None, "result": import_result, "error": None},
    )
