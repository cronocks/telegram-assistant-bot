"""web_admin.py — Admin routes, export/import (FR-5.5.6, FR-6)."""
from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, Cookie, File, Form, Request, Response, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

from backup_engine import ExportError, ImportFormatError
from interfaces import User
import web_context as ctx

logger = logging.getLogger(__name__)

router = APIRouter()


def _require_admin(web_session: str | None) -> "User | HTMLResponse":
    """Return the authenticated admin User or a 403/redirect response."""
    user = ctx._resolve_user(web_session)
    if user is None:
        return RedirectResponse(url="/login", status_code=303)
    if not user.is_admin:
        return HTMLResponse("403 Forbidden", status_code=403)
    return user


def _zip_response(zip_bytes: bytes, filename: str) -> Response:
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


@router.get("/admin/users", response_class=HTMLResponse)
async def admin_users_list(
    request: Request,
    web_session: str | None = Cookie(default=None),
):
    from acl import _is_minor_child

    result = _require_admin(web_session)
    if not isinstance(result, User):
        return result
    admin = result

    assert ctx._user_store is not None
    assert ctx._templates is not None

    all_users = ctx._user_store.list_users()
    users_annotated = [
        {"user": u, "is_minor_child": _is_minor_child(u.id, ctx._user_store)}
        for u in all_users
    ]
    return ctx._templates.TemplateResponse(
        request, "admin_users.html",
        {"user": admin, "users": users_annotated},
    )


@router.get("/admin/users/{target_id}/conversations", response_class=HTMLResponse)
async def admin_user_conversations(
    target_id: int,
    request: Request,
    web_session: str | None = Cookie(default=None),
):
    from acl import _is_minor_child

    result = _require_admin(web_session)
    if not isinstance(result, User):
        return result
    admin = result

    assert ctx._user_store is not None
    assert ctx._conv_store is not None
    assert ctx._templates is not None

    target_user = ctx._user_store.get_user_by_id(target_id)
    if target_user is None:
        return HTMLResponse("404 Not found", status_code=404)

    if not _is_minor_child(target_id, ctx._user_store):
        return HTMLResponse("403 Forbidden — target is not an eligible minor child", status_code=403)

    conversations = ctx._conv_store.admin_list_for_user(target_id)
    return ctx._templates.TemplateResponse(
        request, "admin_conversations.html",
        {"user": admin, "target_user": target_user, "conversations": conversations},
    )


@router.get("/admin/conversations/{conv_id}", response_class=HTMLResponse)
async def admin_conversation_view(
    conv_id: int,
    request: Request,
    web_session: str | None = Cookie(default=None),
):
    from acl import _is_minor_child

    result = _require_admin(web_session)
    if not isinstance(result, User):
        return result
    admin = result

    assert ctx._conv_store is not None
    assert ctx._user_store is not None
    assert ctx._audit is not None
    assert ctx._templates is not None

    conv = ctx._conv_store.get(conv_id)
    if conv is None:
        return HTMLResponse("404 Not found", status_code=404)

    if not _is_minor_child(conv["user_id"], ctx._user_store):
        return HTMLResponse("403 Forbidden — conversation owner is not an eligible minor child", status_code=403)

    target_user = ctx._user_store.get_user_by_id(conv["user_id"])
    messages = ctx._conv_store.list_messages(conv_id)

    ctx._audit.log(
        admin.id, "stealth_read_web_conversation", "web_conversation", str(conv_id),
        {"target_user_id": conv["user_id"]},
    )

    return ctx._templates.TemplateResponse(
        request, "admin_conversation_view.html",
        {"user": admin, "target_user": target_user, "conv": conv, "messages": messages},
    )


@router.get("/settings/export")
async def self_export(web_session: str | None = Cookie(default=None)):
    """Self-export: generate ZIP, store under a one-time token, redirect to download URL."""
    user = ctx._resolve_user(web_session)
    if user is None:
        return RedirectResponse(url="/login", status_code=303)

    if ctx._backup_engine is None:
        return HTMLResponse("Backup chưa được cấu hình.", status_code=503)

    remaining = ctx._backup_engine.export_cooldown_remaining(user.id)
    if remaining > 0:
        return HTMLResponse(
            f"Rate limit: chờ {remaining} giây trước khi export lần tiếp theo.",
            status_code=429,
        )

    try:
        zip_bytes, _manifest = ctx._backup_engine.generate_export(user.id)
    except ExportError as exc:
        logger.warning("Self export failed for user %s: %s", user.id, exc)
        return HTMLResponse(f"Export thất bại: {exc}", status_code=500)

    filename = _export_filename(user.name)
    token = ctx._store_download_token(zip_bytes, filename)
    return RedirectResponse(url=f"/settings/export/download?token={token}", status_code=303)


@router.get("/settings/export/download")
async def self_export_download(token: str):
    """Serve the pre-generated ZIP identified by a one-time download token (TTL 60s)."""
    entry = ctx._consume_download_token(token)
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
    result = _require_admin(web_session)
    if not isinstance(result, User):
        return result
    admin = result

    if ctx._backup_engine is None:
        return HTMLResponse("Backup chưa được cấu hình.", status_code=503)

    assert ctx._user_store is not None
    target = ctx._user_store.get_user_by_id(target_id)
    if target is None:
        return HTMLResponse("404 User không tồn tại.", status_code=404)

    remaining = ctx._backup_engine.export_cooldown_remaining(target_id)
    if remaining > 0:
        return HTMLResponse(
            f"Rate limit: chờ {remaining} giây (cooldown chung với mọi export của user này).",
            status_code=429,
        )

    try:
        zip_bytes, manifest = ctx._backup_engine.generate_export(target_id)
    except ExportError as exc:
        logger.warning("Admin export failed for user %s by admin %s: %s", target_id, admin.id, exc)
        return HTMLResponse(f"Export thất bại: {exc}", status_code=500)

    assert ctx._audit is not None
    ctx._audit.log(
        actor_user_id=admin.id,
        action="data_export",
        target_type="user",
        target_id=target_id,
        payload={"size_bytes": len(zip_bytes), "delivery": "web_admin"},
    )

    return _zip_response(zip_bytes, _export_filename(target.name))


@router.get("/admin/import", response_class=HTMLResponse)
async def admin_import_page(
    request: Request,
    web_session: str | None = Cookie(default=None),
):
    result = _require_admin(web_session)
    if not isinstance(result, User):
        return result

    assert ctx._templates is not None
    return ctx._templates.TemplateResponse(
        request, "import.html",
        {"user": result, "state": "upload", "preview": None, "result": None, "error": None},
    )


@router.post("/admin/import/preview", response_class=HTMLResponse)
async def admin_import_preview(
    request: Request,
    zip_file: UploadFile = File(...),
    web_session: str | None = Cookie(default=None),
):
    result = _require_admin(web_session)
    if not isinstance(result, User):
        return result

    if ctx._backup_engine is None:
        return HTMLResponse("Backup chưa được cấu hình.", status_code=503)

    assert ctx._templates is not None

    zip_bytes = await zip_file.read()
    if len(zip_bytes) > 100 * 1024 * 1024:
        return ctx._templates.TemplateResponse(
            request, "import.html",
            {"user": result, "state": "upload", "preview": None, "result": None,
             "error": "File quá lớn (tối đa 100 MB)."},
            status_code=413,
        )

    try:
        parsed = ctx._backup_engine.parse_import(zip_bytes)
    except ImportFormatError as exc:
        return ctx._templates.TemplateResponse(
            request, "import.html",
            {"user": result, "state": "upload", "preview": None, "result": None,
             "error": f"ZIP không hợp lệ: {exc}"},
            status_code=400,
        )

    token = ctx._store_import_token(parsed)
    preview = {"manifest": parsed.manifest, "warnings": parsed.warnings, "token": token}
    return ctx._templates.TemplateResponse(
        request, "import.html",
        {"user": result, "state": "preview", "preview": preview, "result": None, "error": None},
    )


@router.post("/admin/import/apply", response_class=HTMLResponse)
async def admin_import_apply(
    request: Request,
    token: str = Form(...),
    web_session: str | None = Cookie(default=None),
):
    result = _require_admin(web_session)
    if not isinstance(result, User):
        return result

    if ctx._backup_engine is None:
        return HTMLResponse("Backup chưa được cấu hình.", status_code=503)

    assert ctx._templates is not None

    parsed = ctx._consume_import_token(token)
    if parsed is None:
        return ctx._templates.TemplateResponse(
            request, "import.html",
            {"user": result, "state": "upload", "preview": None, "result": None,
             "error": "Token hết hạn hoặc không hợp lệ. Vui lòng upload lại file ZIP."},
            status_code=400,
        )

    try:
        import_result = ctx._backup_engine.apply_import(parsed, admin_user_id=result.id)
    except Exception as exc:
        logger.exception("Import apply failed: %s", exc)
        return ctx._templates.TemplateResponse(
            request, "import.html",
            {"user": result, "state": "upload", "preview": None, "result": None,
             "error": f"Import thất bại: {exc}"},
            status_code=500,
        )

    return ctx._templates.TemplateResponse(
        request, "import.html",
        {"user": result, "state": "result", "preview": None, "result": import_result, "error": None},
    )
