"""web_anniversaries.py — Anniversary CRUD routes (FR-8)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Cookie, Request
from fastapi.responses import HTMLResponse, RedirectResponse

import web_context as ctx

router = APIRouter()


def _parse_int_or_400(s: str) -> int:
    try:
        return int(s)
    except ValueError:
        raise ValueError(f"Invalid integer: {s!r}")


def _form_to_anniv_kwargs(form: dict) -> dict:
    """Convert POST form dict to create_anniversary kwargs. Raises ValueError on bad input."""
    name = (form.get("name") or "").strip()
    if not name:
        raise ValueError("Tên kỷ niệm không được để trống.")
    date_type = (form.get("date_type") or "").strip()
    if date_type not in ("lunar", "solar"):
        raise ValueError(f"date_type must be lunar|solar, got {date_type!r}")
    try:
        day = int(form.get("day", "0"))
        month = int(form.get("month", "0"))
    except (TypeError, ValueError):
        raise ValueError("day/month must be integers")
    category = (form.get("category") or "khac").strip()
    offsets = (form.get("reminder_offsets") or "30,15,7,3,1,0").strip()
    note = (form.get("note") or "").strip() or None
    year_raw = (form.get("year") or "").strip()
    year = int(year_raw) if year_raw.isdigit() else None
    is_leap_month = 1 if date_type == "lunar" and form.get("is_leap_month") == "1" else 0
    return {
        "name": name, "date_type": date_type,
        "day": day, "month": month, "year": year,
        "is_leap_month": is_leap_month,
        "category": category, "reminder_offsets": offsets, "note": note,
    }


@router.get("/anniversaries", response_class=HTMLResponse)
async def anniversaries_list(
    request: Request,
    web_session: str | None = Cookie(default=None),
):
    user = ctx._resolve_user(web_session)
    if user is None:
        return RedirectResponse(url="/login", status_code=303)
    assert ctx._templates is not None
    rows = ctx._anniversary_store.list_for_user(user.id) if ctx._anniversary_store else []
    return ctx._templates.TemplateResponse(
        request, "anniversaries.html",
        {"user": user, "anniversaries": rows},
    )


@router.get("/anniversaries/new", response_class=HTMLResponse)
async def anniversary_new_form(
    request: Request,
    web_session: str | None = Cookie(default=None),
):
    user = ctx._resolve_user(web_session)
    if user is None:
        return RedirectResponse(url="/login", status_code=303)
    assert ctx._templates is not None
    return ctx._templates.TemplateResponse(
        request, "anniversary_form.html",
        {"user": user, "anniversary": None, "error": None},
    )


@router.post("/anniversaries")
async def anniversary_create(
    request: Request,
    web_session: str | None = Cookie(default=None),
):
    user = ctx._resolve_user(web_session)
    if user is None:
        return RedirectResponse(url="/login", status_code=303)
    if ctx._anniversary_store is None or ctx._anniversary_engine is None:
        return HTMLResponse("Anniversary feature not initialised.", status_code=503)

    form = dict(await request.form())
    try:
        kwargs = _form_to_anniv_kwargs(form)
        row = ctx._anniversary_store.create_anniversary(user_id=user.id, **kwargs)
    except ValueError as e:
        assert ctx._templates is not None
        return ctx._templates.TemplateResponse(
            request, "anniversary_form.html",
            {"user": user, "anniversary": None, "error": str(e)},
            status_code=400,
        )

    ctx._anniversary_engine.compute_year(datetime.now(timezone(timedelta(hours=7))).year)
    if ctx._audit is not None:
        ctx._audit.log(
            user.id, "anniversary_created", "anniversary", str(row["id"]),
            {"name": row["name"], "date_type": row["date_type"]},
        )
    return RedirectResponse(url=f"/anniversaries/{row['id']}", status_code=303)


@router.get("/anniversaries/{anniv_id}", response_class=HTMLResponse)
async def anniversary_view(
    anniv_id: int,
    request: Request,
    web_session: str | None = Cookie(default=None),
):
    user = ctx._resolve_user(web_session)
    if user is None:
        return RedirectResponse(url="/login", status_code=303)
    if ctx._anniversary_store is None:
        return HTMLResponse("Anniversary feature not initialised.", status_code=503)
    row = ctx._anniversary_store.get_anniversary(anniv_id)
    if row is None or row["user_id"] != user.id or row["deleted_at"] is not None:
        return HTMLResponse("404 Not Found", status_code=404)
    assert ctx._templates is not None
    return ctx._templates.TemplateResponse(
        request, "anniversary_view.html",
        {"user": user, "anniversary": row},
    )


@router.get("/anniversaries/{anniv_id}/edit", response_class=HTMLResponse)
async def anniversary_edit_form(
    anniv_id: int,
    request: Request,
    web_session: str | None = Cookie(default=None),
):
    user = ctx._resolve_user(web_session)
    if user is None:
        return RedirectResponse(url="/login", status_code=303)
    if ctx._anniversary_store is None:
        return HTMLResponse("Anniversary feature not initialised.", status_code=503)
    row = ctx._anniversary_store.get_anniversary(anniv_id)
    if row is None or row["user_id"] != user.id or row["deleted_at"] is not None:
        return HTMLResponse("404 Not Found", status_code=404)
    assert ctx._templates is not None
    return ctx._templates.TemplateResponse(
        request, "anniversary_form.html",
        {"user": user, "anniversary": row, "error": None},
    )


@router.post("/anniversaries/{anniv_id}")
async def anniversary_update(
    anniv_id: int,
    request: Request,
    web_session: str | None = Cookie(default=None),
):
    user = ctx._resolve_user(web_session)
    if user is None:
        return RedirectResponse(url="/login", status_code=303)
    if ctx._anniversary_store is None or ctx._anniversary_engine is None:
        return HTMLResponse("Anniversary feature not initialised.", status_code=503)

    row = ctx._anniversary_store.get_anniversary(anniv_id)
    if row is None or row["user_id"] != user.id or row["deleted_at"] is not None:
        return HTMLResponse("404 Not Found", status_code=404)

    form = dict(await request.form())
    try:
        kwargs = _form_to_anniv_kwargs(form)
    except ValueError as e:
        assert ctx._templates is not None
        return ctx._templates.TemplateResponse(
            request, "anniversary_form.html",
            {"user": user, "anniversary": row, "error": str(e)},
            status_code=400,
        )
    kwargs["enabled"] = 1 if form.get("enabled") else 0

    try:
        ctx._anniversary_store.update_anniversary(anniv_id, **kwargs)
    except ValueError as e:
        assert ctx._templates is not None
        return ctx._templates.TemplateResponse(
            request, "anniversary_form.html",
            {"user": user, "anniversary": row, "error": str(e)},
            status_code=400,
        )

    if (row["date_type"] != kwargs["date_type"]
            or row["month"] != kwargs["month"]
            or row["day"] != kwargs["day"]
            or row["reminder_offsets"] != kwargs["reminder_offsets"]):
        ctx._anniversary_engine.cancel_all_for_anniversary(anniv_id)
        ctx._anniversary_engine.compute_year(datetime.now(timezone(timedelta(hours=7))).year)

    if ctx._audit is not None:
        ctx._audit.log(
            user.id, "anniversary_updated", "anniversary", str(anniv_id),
            {"changed_fields": list(kwargs.keys())},
        )
    return RedirectResponse(url=f"/anniversaries/{anniv_id}", status_code=303)


@router.post("/anniversaries/{anniv_id}/delete")
async def anniversary_delete(
    anniv_id: int,
    web_session: str | None = Cookie(default=None),
):
    user = ctx._resolve_user(web_session)
    if user is None:
        return RedirectResponse(url="/login", status_code=303)
    if ctx._anniversary_store is None or ctx._anniversary_engine is None:
        return HTMLResponse("Anniversary feature not initialised.", status_code=503)

    row = ctx._anniversary_store.get_anniversary(anniv_id)
    if row is None or row["user_id"] != user.id or row["deleted_at"] is not None:
        return HTMLResponse("404 Not Found", status_code=404)

    ctx._anniversary_store.soft_delete_anniversary(anniv_id)
    ctx._anniversary_engine.cancel_all_for_anniversary(anniv_id)
    if ctx._audit is not None:
        ctx._audit.log(
            user.id, "anniversary_deleted", "anniversary", str(anniv_id),
            {"name": row["name"]},
        )
    return RedirectResponse(url="/anniversaries", status_code=303)
