"""web_tasks.py — Tasks page and task REST API."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Cookie, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

import web_context as ctx

router = APIRouter()


class _TaskCreate(BaseModel):
    title: str
    deadline: str | None = None


@router.get("/api/tasks")
async def api_list_tasks(
    date: str | None = Query(default=None),
    web_session: str | None = Cookie(default=None),
):
    """Return JSON list of pending tasks for the current user.

    Optional ?date=YYYY-MM-DD limits the deadline upper bound to end of that day.
    Defaults to end of today (VN_TZ).
    """
    user = ctx._resolve_user(web_session)
    if user is None:
        return JSONResponse({"error": "unauthenticated"}, status_code=401)
    if ctx._task_store is None:
        return JSONResponse([])

    vn_tz = timezone(timedelta(hours=7))
    if date:
        before_iso = f"{date}T23:59:59+07:00"
    else:
        today = datetime.now(vn_tz).strftime("%Y-%m-%d")
        before_iso = f"{today}T23:59:59+07:00"

    tasks = ctx._task_store.list_pending_due(before_iso, user_id=user.id)
    return JSONResponse(tasks)


@router.post("/api/tasks", status_code=201)
async def api_create_task(
    body: _TaskCreate,
    web_session: str | None = Cookie(default=None),
):
    user = ctx._resolve_user(web_session)
    if user is None:
        return JSONResponse({"error": "unauthenticated"}, status_code=401)
    if ctx._task_store is None:
        return JSONResponse({"error": "task store not available"}, status_code=503)

    task = ctx._task_store.create_task(
        user_id=user.id,
        title=body.title,
        deadline=body.deadline,
    )
    return JSONResponse(task, status_code=201)


@router.patch("/api/tasks/{task_id}/complete")
async def api_complete_task(
    task_id: int,
    web_session: str | None = Cookie(default=None),
):
    user = ctx._resolve_user(web_session)
    if user is None:
        return JSONResponse({"error": "unauthenticated"}, status_code=401)
    if ctx._task_store is None:
        return JSONResponse({"error": "task store not available"}, status_code=503)

    updated = ctx._task_store.complete_task(task_id)
    if updated is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(updated)


@router.delete("/api/tasks/{task_id}")
async def api_cancel_task(
    task_id: int,
    web_session: str | None = Cookie(default=None),
):
    user = ctx._resolve_user(web_session)
    if user is None:
        return JSONResponse({"error": "unauthenticated"}, status_code=401)
    if ctx._task_store is None:
        return JSONResponse({"error": "task store not available"}, status_code=503)

    updated = ctx._task_store.cancel_task(task_id)
    if updated is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(updated)


@router.get("/tasks", response_class=HTMLResponse)
async def tasks_page(
    request: Request,
    web_session: str | None = Cookie(default=None),
):
    user = ctx._resolve_user(web_session)
    if user is None:
        return RedirectResponse(url="/login", status_code=303)

    assert ctx._templates is not None
    vn_tz = timezone(timedelta(hours=7))
    now = datetime.now(vn_tz)
    today_str = now.strftime("%Y-%m-%d")
    today_end = f"{today_str}T23:59:59+07:00"

    pending = ctx._task_store.list_pending_due(today_end, user_id=user.id) if ctx._task_store else []
    completed = ctx._task_store.list_completed_on(user.id, today_str) if ctx._task_store else []

    return ctx._templates.TemplateResponse(
        request, "tasks.html",
        {"user": user, "pending": pending, "completed": completed, "today": today_str},
    )
