"""web_family.py — Family tree CRUD routes (FR-11)."""
from __future__ import annotations

from fastapi import APIRouter, Cookie, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

import web_context as ctx

router = APIRouter()

_FAMILY_ADMIN_ROLES = {"admin", "manager"}


def _form_to_member_kwargs(form: dict) -> tuple[str, dict]:
    """Parse POST form into (full_name, kwargs) for create_member / update_member.

    Raises ValueError on validation failure (propagated to caller for re-render).
    """
    full_name = (form.get("full_name") or "").strip()
    if not full_name:
        raise ValueError("Họ tên không được để trống.")
    kwargs: dict = {}
    for key in ("alias_name", "gender", "branch", "bio", "birth_date_type", "death_date_type"):
        val = (form.get(key) or "").strip() or None
        if val:
            kwargs[key] = val
    for key in (
        "generation",
        "birth_year", "birth_month", "birth_day",
        "death_year", "death_month", "death_day",
    ):
        raw = (form.get(key) or "").strip()
        if raw.isdigit():
            kwargs[key] = int(raw)
    for key in ("birth_approx", "death_approx"):
        kwargs[key] = 1 if form.get(key) == "1" else 0
    return full_name, kwargs


@router.get("/family/members", response_class=HTMLResponse)
async def family_member_list(
    request: Request,
    q: str | None = Query(default=None),
    web_session: str | None = Cookie(default=None),
):
    user = ctx._resolve_user(web_session)
    if user is None:
        return RedirectResponse(url="/login", status_code=303)
    assert ctx._templates is not None
    if q:
        members = ctx._family_store.search_by_name(q) if ctx._family_store else []
    else:
        members = ctx._family_store.list_members() if ctx._family_store else []
    return ctx._templates.TemplateResponse(
        request, "family_members.html",
        {"user": user, "members": members, "q": q or ""},
    )


@router.get("/family", response_class=HTMLResponse)
async def family_tree_view(
    request: Request,
    web_session: str | None = Cookie(default=None),
):
    user = ctx._resolve_user(web_session)
    if user is None:
        return RedirectResponse(url="/login", status_code=303)
    assert ctx._templates is not None
    from family_tree import build_tree_structure
    tree_data = (
        build_tree_structure(ctx._family_store._conn)
        if ctx._family_store else {"has_data": False, "rows": []}
    )
    return ctx._templates.TemplateResponse(
        request, "family_tree.html",
        {"user": user, "tree_data": tree_data},
    )


# Specific sub-path declared before dynamic /{member_id} to prevent ambiguous match.

@router.get("/family/members/new", response_class=HTMLResponse)
async def family_member_new_form(
    request: Request,
    web_session: str | None = Cookie(default=None),
):
    user = ctx._resolve_user(web_session)
    if user is None:
        return RedirectResponse(url="/login", status_code=303)
    if user.role not in _FAMILY_ADMIN_ROLES:
        return HTMLResponse("403 Forbidden", status_code=403)
    assert ctx._templates is not None
    return ctx._templates.TemplateResponse(
        request, "family_member_form.html",
        {"user": user, "member": None, "error": None},
    )


@router.post("/family/members")
async def family_member_create(
    request: Request,
    web_session: str | None = Cookie(default=None),
):
    user = ctx._resolve_user(web_session)
    if user is None:
        return RedirectResponse(url="/login", status_code=303)
    if user.role not in _FAMILY_ADMIN_ROLES:
        return HTMLResponse("403 Forbidden", status_code=403)
    if ctx._family_store is None:
        return HTMLResponse("Family feature not initialised.", status_code=503)

    form = dict(await request.form())
    try:
        full_name, kwargs = _form_to_member_kwargs(form)
        row = ctx._family_store.create_member(created_by=user.id, full_name=full_name, **kwargs)
    except ValueError as e:
        assert ctx._templates is not None
        return ctx._templates.TemplateResponse(
            request, "family_member_form.html",
            {"user": user, "member": None, "error": str(e)},
            status_code=400,
        )

    if ctx._audit is not None:
        ctx._audit.log(
            user.id, "family_member_created", "family_member", str(row["id"]),
            {"full_name": row["full_name"]},
        )
    return RedirectResponse(url=f"/family/members/{row['id']}", status_code=303)


@router.get("/family/members/{member_id}", response_class=HTMLResponse)
async def family_member_view(
    member_id: int,
    request: Request,
    web_session: str | None = Cookie(default=None),
):
    user = ctx._resolve_user(web_session)
    if user is None:
        return RedirectResponse(url="/login", status_code=303)
    if ctx._family_store is None:
        return HTMLResponse("Family feature not initialised.", status_code=503)
    row = ctx._family_store.get_member(member_id)
    if row is None or row.get("deleted_at") is not None:
        return HTMLResponse("404 Not Found", status_code=404)
    burial = ctx._burial_store.get_current_for_member(member_id) if ctx._burial_store else None
    maps_url = None
    if burial and burial.get("lat") is not None and burial.get("lng") is not None:
        maps_url = f"https://maps.google.com/?q={burial['lat']},{burial['lng']}"
    assert ctx._templates is not None
    return ctx._templates.TemplateResponse(
        request, "family_member_view.html",
        {"user": user, "member": row, "burial": burial, "maps_url": maps_url},
    )


@router.get("/family/members/{member_id}/edit", response_class=HTMLResponse)
async def family_member_edit_form(
    member_id: int,
    request: Request,
    web_session: str | None = Cookie(default=None),
):
    user = ctx._resolve_user(web_session)
    if user is None:
        return RedirectResponse(url="/login", status_code=303)
    if user.role not in _FAMILY_ADMIN_ROLES:
        return HTMLResponse("403 Forbidden", status_code=403)
    if ctx._family_store is None:
        return HTMLResponse("Family feature not initialised.", status_code=503)
    row = ctx._family_store.get_member(member_id)
    if row is None or row.get("deleted_at") is not None:
        return HTMLResponse("404 Not Found", status_code=404)
    assert ctx._templates is not None
    return ctx._templates.TemplateResponse(
        request, "family_member_form.html",
        {"user": user, "member": row, "error": None},
    )


@router.post("/family/members/{member_id}")
async def family_member_update(
    member_id: int,
    request: Request,
    web_session: str | None = Cookie(default=None),
):
    user = ctx._resolve_user(web_session)
    if user is None:
        return RedirectResponse(url="/login", status_code=303)
    if user.role not in _FAMILY_ADMIN_ROLES:
        return HTMLResponse("403 Forbidden", status_code=403)
    if ctx._family_store is None:
        return HTMLResponse("Family feature not initialised.", status_code=503)

    row = ctx._family_store.get_member(member_id)
    if row is None or row.get("deleted_at") is not None:
        return HTMLResponse("404 Not Found", status_code=404)

    form = dict(await request.form())
    try:
        full_name, kwargs = _form_to_member_kwargs(form)
        kwargs["full_name"] = full_name
        ctx._family_store.update_member(member_id, **kwargs)
    except ValueError as e:
        assert ctx._templates is not None
        return ctx._templates.TemplateResponse(
            request, "family_member_form.html",
            {"user": user, "member": row, "error": str(e)},
            status_code=400,
        )

    if ctx._audit is not None:
        ctx._audit.log(
            user.id, "family_member_updated", "family_member", str(member_id),
            {"changed_fields": list(kwargs.keys())},
        )
    return RedirectResponse(url=f"/family/members/{member_id}", status_code=303)
