"""web_auth.py — Auth routes: login, logout, setup-password, settings/password."""
from __future__ import annotations

from fastapi import APIRouter, Cookie, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

import config
import web_context as ctx

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def index(web_session: str | None = Cookie(default=None)):
    user = ctx._resolve_user(web_session)
    if user:
        return RedirectResponse(url="/chat", status_code=303)
    return RedirectResponse(url="/login", status_code=303)


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, web_session: str | None = Cookie(default=None)):
    if ctx._resolve_user(web_session):
        return RedirectResponse(url="/chat", status_code=303)
    assert ctx._templates is not None
    return ctx._templates.TemplateResponse(request, "login.html", {"error": None})


@router.post("/login")
async def login(
    request: Request,
    response: Response,
    username: str = Form(...),
    password: str = Form(...),
):
    assert ctx._templates is not None
    assert ctx._user_store is not None
    assert ctx._session_store is not None
    assert ctx._audit is not None
    assert ctx._elevation_store is not None

    def _render_error(msg: str):
        return ctx._templates.TemplateResponse(
            request, "login.html", {"error": msg}, status_code=400
        )

    user = ctx._user_store.find_by_username_or_name(username.strip())
    if user is None:
        return _render_error("Tên đăng nhập hoặc mật khẩu không đúng.")

    locked, locked_until = ctx._elevation_store.is_locked("web", str(user.id))
    if locked:
        return _render_error(f"Tài khoản tạm khóa đến {locked_until}. Thử lại sau.")

    if ctx._user_store.get_password_hash(user.id) is None:
        return _render_error("Mật khẩu web chưa được thiết lập. Liên hệ admin.")

    if not ctx._user_store.check_password(user.id, password):
        result = ctx._elevation_store.record_failure(
            "web", str(user.id),
            max_fails=config.SUDO_MAX_FAILS,
            lockout_minutes=config.SUDO_LOCKOUT_MINUTES,
        )
        ctx._audit.log(user.id, "web_login_failed", "user", str(user.id))
        if result.get("locked"):
            return _render_error(
                f"Sai mật khẩu quá {config.SUDO_MAX_FAILS} lần. Tài khoản tạm khóa 15 phút."
            )
        return _render_error("Tên đăng nhập hoặc mật khẩu không đúng.")

    ctx._elevation_store.reset_failures("web", str(user.id))
    must_change = ctx._user_store.get_must_change_password(user.id)
    token = ctx._session_store.create(user.id)
    ctx._audit.log(user.id, "web_login", "user", str(user.id))

    redirect_url = "/setup-password" if must_change else "/chat"
    redirect = RedirectResponse(url=redirect_url, status_code=303)
    ctx._set_session_cookie(redirect, token)
    return redirect


@router.post("/logout")
async def logout(web_session: str | None = Cookie(default=None)):
    assert ctx._session_store is not None
    assert ctx._audit is not None

    redirect = RedirectResponse(url="/login", status_code=303)
    ctx._clear_session_cookie(redirect)

    if web_session:
        user_id = ctx._session_store.find_active(web_session)
        ctx._session_store.revoke(web_session)
        if user_id is not None:
            ctx._audit.log(user_id, "web_logout", "user", str(user_id))

    return redirect


@router.get("/settings/password", response_class=HTMLResponse)
async def settings_password_page(request: Request, web_session: str | None = Cookie(default=None)):
    user = ctx._resolve_user(web_session)
    if user is None:
        return RedirectResponse(url="/login", status_code=303)
    assert ctx._user_store is not None
    if ctx._user_store.get_must_change_password(user.id):
        return RedirectResponse(url="/setup-password", status_code=303)
    assert ctx._templates is not None
    return ctx._templates.TemplateResponse(
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
    user = ctx._resolve_user(web_session)
    if user is None:
        return RedirectResponse(url="/login", status_code=303)
    assert ctx._user_store is not None
    assert ctx._audit is not None
    assert ctx._templates is not None

    def _render(error: str | None = None, success: bool = False):
        return ctx._templates.TemplateResponse(
            request, "settings_password.html",
            {"user": user, "error": error, "success": success},
            status_code=400 if error else 200,
        )

    if len(new_password) < 8:
        return _render("Mật khẩu mới phải có ít nhất 8 ký tự.")
    if new_password != confirm_password:
        return _render("Mật khẩu xác nhận không khớp.")
    if not ctx._user_store.check_password(user.id, current_password):
        return _render("Mật khẩu hiện tại không đúng.")

    ctx._user_store.set_password(user.id, new_password)
    ctx._user_store.set_must_change_password(user.id, False)
    ctx._audit.log(user.id, "web_password_changed", "user", str(user.id))
    return _render(success=True)


@router.get("/setup-password", response_class=HTMLResponse)
async def setup_password_page(request: Request, web_session: str | None = Cookie(default=None)):
    user = ctx._resolve_user(web_session)
    if user is None:
        return RedirectResponse(url="/login", status_code=303)
    assert ctx._templates is not None
    return ctx._templates.TemplateResponse(request, "setup_password.html", {"error": None})


@router.post("/setup-password")
async def setup_password(
    request: Request,
    response: Response,
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    web_session: str | None = Cookie(default=None),
):
    assert ctx._templates is not None
    assert ctx._user_store is not None
    assert ctx._session_store is not None
    assert ctx._audit is not None

    user = ctx._resolve_user(web_session)
    if user is None:
        return RedirectResponse(url="/login", status_code=303)

    def _render_error(msg: str):
        return ctx._templates.TemplateResponse(
            request, "setup_password.html", {"error": msg}, status_code=400
        )

    if len(new_password) < 8:
        return _render_error("Mật khẩu phải có ít nhất 8 ký tự.")
    if new_password != confirm_password:
        return _render_error("Mật khẩu xác nhận không khớp.")

    ctx._user_store.set_password(user.id, new_password)
    ctx._user_store.set_must_change_password(user.id, False)
    ctx._audit.log(user.id, "web_password_set", "user", str(user.id))

    if web_session:
        ctx._session_store.revoke(web_session)
    new_token = ctx._session_store.create(user.id)
    redirect = RedirectResponse(url="/chat", status_code=303)
    ctx._set_session_cookie(redirect, new_token)
    return redirect
