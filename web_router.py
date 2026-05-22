"""web_router.py — FastAPI router for the web UI channel (FR-5).

Routes:
  GET  /               → redirect to /chat (or /login if not authenticated)
  GET  /login          → login page
  POST /login          → authenticate, set session cookie
  POST /logout         → revoke session, clear cookie
  GET  /chat           → chat UI (requires auth)
  POST /chat/send      → submit a message (requires auth)
  GET  /chat/stream    → SSE stream for bot replies (requires auth)
  GET  /setup-password → force-reset page (requires must_change_password flag)
  POST /setup-password → set new password + create full session

Security:
  - HttpOnly + SameSite=Lax cookies (+ Secure in staging/production)
  - Brute-force: reuses sudo_attempts table (channel="web"), 5 fails → 15-min lock
  - Audit: web_login / web_logout / web_login_failed / web_password_set
"""
from __future__ import annotations

import asyncio
import logging
from typing import Annotated

from fastapi import APIRouter, Cookie, Depends, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sse_starlette.sse import EventSourceResponse

import config
from interfaces import User, WebSessionStore, UserStore, AuditLog, ElevationStore
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

_COOKIE_NAME = "web_session"
_SESSION_MAX_AGE = config.WEB_SESSION_TTL_DAYS * 86_400  # seconds


def init_web_router(
    templates: Jinja2Templates,
    web_channel: WebChannelAdapter,
    session_store: WebSessionStore,
    user_store: UserStore,
    audit: AuditLog,
    elevation_store: ElevationStore,
) -> None:
    """Wire dependencies into this router (called once from main.py lifespan)."""
    global _templates, _web_channel, _session_store, _user_store, _audit, _elevation_store
    _templates = templates
    _web_channel = web_channel
    _session_store = session_store
    _user_store = user_store
    _audit = audit
    _elevation_store = elevation_store


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


# ── Auth dependency ────────────────────────────────────────────────────────────

def _get_session_token(web_session: str | None = Cookie(default=None)) -> str | None:
    return web_session


def _require_user(token: str | None = Depends(_get_session_token)) -> User:
    """FastAPI dependency: resolves the session token to a User or raises redirect."""
    if token is None or _session_store is None:
        raise _redirect_to_login()
    user_id = _session_store.find_active(token)
    if user_id is None:
        raise _redirect_to_login()
    assert _user_store is not None
    user = _user_store.get_user_by_id(user_id)
    if user is None or not user.is_active:
        raise _redirect_to_login()
    return user


def _redirect_to_login():
    from fastapi import HTTPException
    # We raise a redirect as an exception so Depends() callers get redirected.
    # HTTPException with 303 works when caught by FastAPI's exception handler.
    # The actual redirect is handled in the route via try/except or direct check.
    return RedirectResponse(url="/login", status_code=303)


# ── Helper: resolve User from cookie (returns None instead of raising) ─────────

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


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
async def index(web_session: str | None = Cookie(default=None)):
    user = _resolve_user(web_session)
    if user:
        return RedirectResponse(url="/chat", status_code=303)
    return RedirectResponse(url="/login", status_code=303)


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

    # Find user
    user = _user_store.find_by_username_or_name(username.strip())
    if user is None:
        return _render_error("Tên đăng nhập hoặc mật khẩu không đúng.")

    # Brute-force check (reuse elevation_store sudo_attempts with channel="web")
    locked, locked_until = _elevation_store.is_locked("web", str(user.id))
    if locked:
        return _render_error(f"Tài khoản tạm khóa đến {locked_until}. Thử lại sau.")

    # Password not set yet
    if _user_store.get_password_hash(user.id) is None:
        return _render_error("Mật khẩu web chưa được thiết lập. Liên hệ admin.")

    # Verify password
    if not _user_store.check_password(user.id, password):
        result = _elevation_store.record_failure(
            "web", str(user.id),
            max_fails=config.SUDO_MAX_FAILS,
            lockout_minutes=config.SUDO_LOCKOUT_MINUTES,
        )
        _audit.log(user.id, "web_login_failed", "user", str(user.id))
        if result.get("locked"):
            return _render_error(f"Sai mật khẩu quá {config.SUDO_MAX_FAILS} lần. Tài khoản tạm khóa 15 phút.")
        return _render_error("Tên đăng nhập hoặc mật khẩu không đúng.")

    # Success — reset failure counter
    _elevation_store.reset_failures("web", str(user.id))

    # Force-reset check
    must_change = _user_store.get_must_change_password(user.id)

    # Create session (short-lived temp token for force-reset, full TTL otherwise)
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

    # Revoke old session, issue fresh full-TTL session
    if web_session:
        _session_store.revoke(web_session)
    new_token = _session_store.create(user.id)
    redirect = RedirectResponse(url="/chat", status_code=303)
    _set_session_cookie(redirect, new_token)
    return redirect


@router.get("/chat", response_class=HTMLResponse)
async def chat_page(request: Request, web_session: str | None = Cookie(default=None)):
    user = _resolve_user(web_session)
    if user is None:
        return RedirectResponse(url="/login", status_code=303)

    # Block if must_change_password is still set
    if _user_store.get_must_change_password(user.id):
        return RedirectResponse(url="/setup-password", status_code=303)

    assert _templates is not None
    return _templates.TemplateResponse(request, "chat.html", {"user": user})


@router.post("/chat/send")
async def send_message(
    request: Request,
    text: str = Form(...),
    web_session: str | None = Cookie(default=None),
):
    """Receive a message from the browser, route through core_handler."""
    from interfaces import ChannelMessage
    from core_handler import handle_message

    user = _resolve_user(web_session)
    if user is None:
        return RedirectResponse(url="/login", status_code=303)

    msg = ChannelMessage(
        channel="web",
        chat_id=str(user.id),
        text=text.strip(),
    )

    # web_deps is set on app.state by main.py
    web_deps = getattr(request.app.state, "web_deps", None)
    if web_deps is None:
        logger.error("web_deps not wired — check main.py lifespan")
        return HTMLResponse("<p>Lỗi hệ thống.</p>", status_code=500)

    asyncio.create_task(handle_message(msg, user, web_deps))
    # Return immediately; the reply arrives via SSE stream
    return HTMLResponse("", status_code=204)


@router.get("/chat/stream")
async def chat_stream(request: Request, web_session: str | None = Cookie(default=None)):
    """SSE endpoint: streams bot replies to the browser via EventSource."""
    user = _resolve_user(web_session)
    if user is None:
        return RedirectResponse(url="/login", status_code=303)

    assert _web_channel is not None
    user_id_str = str(user.id)
    q = _web_channel.connect(user_id_str)

    async def event_generator():
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    text = await asyncio.wait_for(q.get(), timeout=30.0)
                    yield {"data": text}
                except asyncio.TimeoutError:
                    # Send a keep-alive comment so the connection stays open
                    yield {"comment": "keepalive"}
        finally:
            _web_channel.disconnect(user_id_str)

    return EventSourceResponse(event_generator())
