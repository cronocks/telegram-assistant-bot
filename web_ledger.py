"""web_ledger.py — Ledger (expense/income) routes (FR-9)."""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Cookie, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

import web_context as ctx

router = APIRouter()


def _current_month_str() -> str:
    from timeutils import VIETNAM_TZ
    return datetime.now(VIETNAM_TZ).strftime("%Y-%m")


def _now_local_str() -> str:
    from timeutils import VIETNAM_TZ
    return datetime.now(VIETNAM_TZ).strftime("%Y-%m-%dT%H:%M")


@router.get("/ledger", response_class=HTMLResponse)
async def ledger_list(
    request: Request,
    web_session: str | None = Cookie(default=None),
    month: str | None = None,
):
    user = ctx._resolve_user(web_session)
    if user is None:
        return RedirectResponse(url="/login", status_code=303)
    assert ctx._templates is not None
    if ctx._ledger_store is None:
        return HTMLResponse("Ledger feature not initialised.", status_code=503)

    current_month = month or _current_month_str()
    entries = ctx._ledger_store.list_for_user(user.id, month=current_month, limit=100)

    cat_map: dict[int, str] = {}
    if ctx._category_store is not None:
        for cat in ctx._category_store.list_for_user(user.id):
            cat_map[cat["id"]] = cat["name"]
    for e in entries:
        e["category_name"] = cat_map.get(e["category_id"]) if e["category_id"] else None

    summary = {"income": 0, "expense": 0, "savings": 0, "budget_pct": None}
    budget = None
    if ctx._ledger_reports is not None:
        summary = ctx._ledger_reports.monthly_summary(user.id, current_month)
    if ctx._budget_store is not None:
        budget = ctx._budget_store.get_budget(user.id, current_month)

    return ctx._templates.TemplateResponse(
        request, "ledger.html",
        {"user": user, "entries": entries, "month": current_month,
         "summary": summary, "budget": budget},
    )


@router.get("/ledger/new", response_class=HTMLResponse)
async def ledger_new_form(
    request: Request,
    web_session: str | None = Cookie(default=None),
):
    user = ctx._resolve_user(web_session)
    if user is None:
        return RedirectResponse(url="/login", status_code=303)
    assert ctx._templates is not None
    categories = ctx._category_store.list_for_user(user.id) if ctx._category_store else []
    return ctx._templates.TemplateResponse(
        request, "ledger_entry_form.html",
        {"user": user, "entry": None, "categories": categories,
         "now": _now_local_str(), "error": None},
    )


@router.post("/ledger", response_class=HTMLResponse)
async def ledger_create(
    request: Request,
    web_session: str | None = Cookie(default=None),
    kind: str = Form(...),
    amount: str = Form(""),
    note: str = Form(""),
    occurred_at: str = Form(""),
    category_id: str = Form(""),
):
    user = ctx._resolve_user(web_session)
    if user is None:
        return RedirectResponse(url="/login", status_code=303)
    assert ctx._templates is not None
    if ctx._ledger_store is None:
        return HTMLResponse("Ledger feature not initialised.", status_code=503)

    categories = ctx._category_store.list_for_user(user.id) if ctx._category_store else []

    def _render_error(msg: str):
        return ctx._templates.TemplateResponse(
            request, "ledger_entry_form.html",
            {"user": user, "entry": None, "categories": categories,
             "now": _now_local_str(), "error": msg},
            status_code=400,
        )

    try:
        amount_int = int(amount)
        if amount_int <= 0:
            raise ValueError
    except (ValueError, TypeError):
        return _render_error("Số tiền không hợp lệ.")

    if kind not in ("income", "expense"):
        return _render_error("Loại giao dịch không hợp lệ.")

    occurred_iso = occurred_at.replace("T", " ") + ":00" if occurred_at else _now_local_str().replace("T", " ") + ":00"
    cat_id = int(category_id) if category_id else None

    ctx._ledger_store.add_entry(
        user.id, kind, amount_int, occurred_iso,
        category_id=cat_id,
        note=note.strip() or None,
        source="web",
    )
    return RedirectResponse(url="/ledger", status_code=303)


# Specific non-dynamic routes must come BEFORE dynamic /{entry_id} routes
# to prevent FastAPI from matching e.g. "categories" as entry_id (int → 422).

@router.get("/ledger/categories", response_class=HTMLResponse)
async def ledger_categories(
    request: Request,
    web_session: str | None = Cookie(default=None),
):
    user = ctx._resolve_user(web_session)
    if user is None:
        return RedirectResponse(url="/login", status_code=303)
    assert ctx._templates is not None
    categories = ctx._category_store.list_for_user(user.id) if ctx._category_store else []
    return ctx._templates.TemplateResponse(
        request, "ledger_categories.html",
        {"user": user, "categories": categories},
    )


@router.post("/ledger/categories")
async def ledger_create_category(
    web_session: str | None = Cookie(default=None),
    name: str = Form(""),
    kind: str = Form("expense"),
):
    user = ctx._resolve_user(web_session)
    if user is None:
        return RedirectResponse(url="/login", status_code=303)
    if ctx._category_store is None:
        return HTMLResponse("Ledger feature not initialised.", status_code=503)

    name = name.strip()
    if name and kind in ("income", "expense"):
        ctx._category_store.create_category(name, kind, user_id=user.id)
    return RedirectResponse(url="/ledger/categories", status_code=303)


@router.post("/ledger/categories/{cat_id}/delete")
async def ledger_delete_category(
    cat_id: int,
    web_session: str | None = Cookie(default=None),
):
    user = ctx._resolve_user(web_session)
    if user is None:
        return RedirectResponse(url="/login", status_code=303)
    if ctx._category_store is None:
        return HTMLResponse("Ledger feature not initialised.", status_code=503)

    cat = ctx._category_store.get_category(cat_id)
    if cat is None:
        return HTMLResponse("403 Forbidden", status_code=403)
    is_owner = cat["user_id"] == user.id
    is_shared = cat["user_id"] is None
    can_manage = user.role in ("admin", "manager")
    if not (is_owner or (is_shared and can_manage)):
        return HTMLResponse("403 Forbidden", status_code=403)

    ctx._category_store.soft_delete_category(cat_id)
    return RedirectResponse(url="/ledger/categories", status_code=303)


@router.get("/ledger/report", response_class=HTMLResponse)
async def ledger_report(
    request: Request,
    web_session: str | None = Cookie(default=None),
    month: str | None = None,
):
    user = ctx._resolve_user(web_session)
    if user is None:
        return RedirectResponse(url="/login", status_code=303)
    assert ctx._templates is not None

    current_month = month or _current_month_str()
    summary = {"income": 0, "expense": 0, "savings": 0,
               "expense_budget": None, "savings_target": None,
               "budget_pct": None, "by_category": []}
    if ctx._ledger_reports is not None:
        summary = ctx._ledger_reports.monthly_summary(user.id, current_month)
        cat_map: dict[int, str] = {}
        if ctx._category_store is not None:
            for cat in ctx._category_store.list_for_user(user.id):
                cat_map[cat["id"]] = cat["name"]
        for row in summary.get("by_category", []):
            row["category_name"] = cat_map.get(row["category_id"]) if row["category_id"] else None

    return ctx._templates.TemplateResponse(
        request, "ledger_report.html",
        {"user": user, "month": current_month, "summary": summary},
    )


@router.get("/ledger/budget", response_class=HTMLResponse)
async def ledger_budget_page(
    request: Request,
    web_session: str | None = Cookie(default=None),
):
    user = ctx._resolve_user(web_session)
    if user is None:
        return RedirectResponse(url="/login", status_code=303)
    assert ctx._templates is not None

    current_month = _current_month_str()
    budget = ctx._budget_store.get_budget(user.id, current_month) if ctx._budget_store else None
    return ctx._templates.TemplateResponse(
        request, "ledger_budget.html",
        {"user": user, "budget": budget, "current_month": current_month},
    )


@router.post("/ledger/budget")
async def ledger_budget_set(
    web_session: str | None = Cookie(default=None),
    month: str = Form(""),
    expense_budget: str = Form(""),
    savings_target: str = Form(""),
):
    user = ctx._resolve_user(web_session)
    if user is None:
        return RedirectResponse(url="/login", status_code=303)
    if ctx._budget_store is None:
        return HTMLResponse("Ledger feature not initialised.", status_code=503)

    month = month.strip() or _current_month_str()
    eb = int(expense_budget) if expense_budget.strip() else None
    st = int(savings_target) if savings_target.strip() else None
    ctx._budget_store.upsert_budget(user.id, month, expense_budget=eb, savings_target=st)
    return RedirectResponse(url="/ledger/budget", status_code=303)


# Dynamic entry routes (must come after all specific /ledger/* paths above)

@router.get("/ledger/{entry_id}/edit", response_class=HTMLResponse)
async def ledger_edit_form(
    entry_id: int,
    request: Request,
    web_session: str | None = Cookie(default=None),
):
    user = ctx._resolve_user(web_session)
    if user is None:
        return RedirectResponse(url="/login", status_code=303)
    assert ctx._templates is not None
    if ctx._ledger_store is None:
        return HTMLResponse("Ledger feature not initialised.", status_code=503)

    entry = ctx._ledger_store.get_entry(entry_id)
    if entry is None or entry["user_id"] != user.id or entry["voided_at"] is not None:
        return HTMLResponse("404 Not Found", status_code=404)

    categories = ctx._category_store.list_for_user(user.id) if ctx._category_store else []
    return ctx._templates.TemplateResponse(
        request, "ledger_entry_form.html",
        {"user": user, "entry": entry, "categories": categories,
         "now": _now_local_str(), "error": None},
    )


@router.post("/ledger/{entry_id}", response_class=HTMLResponse)
async def ledger_update(
    entry_id: int,
    request: Request,
    web_session: str | None = Cookie(default=None),
    kind: str = Form(...),
    amount: str = Form(""),
    note: str = Form(""),
    occurred_at: str = Form(""),
    category_id: str = Form(""),
):
    user = ctx._resolve_user(web_session)
    if user is None:
        return RedirectResponse(url="/login", status_code=303)
    assert ctx._templates is not None
    if ctx._ledger_store is None:
        return HTMLResponse("Ledger feature not initialised.", status_code=503)

    entry = ctx._ledger_store.get_entry(entry_id)
    if entry is None or entry["user_id"] != user.id or entry["voided_at"] is not None:
        return HTMLResponse("404 Not Found", status_code=404)

    categories = ctx._category_store.list_for_user(user.id) if ctx._category_store else []

    def _render_error(msg: str):
        return ctx._templates.TemplateResponse(
            request, "ledger_entry_form.html",
            {"user": user, "entry": entry, "categories": categories,
             "now": _now_local_str(), "error": msg},
            status_code=400,
        )

    try:
        amount_int = int(amount)
        if amount_int <= 0:
            raise ValueError
    except (ValueError, TypeError):
        return _render_error("Số tiền không hợp lệ.")

    if kind not in ("income", "expense"):
        return _render_error("Loại giao dịch không hợp lệ.")

    occurred_iso = occurred_at.replace("T", " ") + ":00" if occurred_at else None
    cat_id = int(category_id) if category_id else None

    ctx._ledger_store.update_entry(
        entry_id,
        kind=kind,
        amount=amount_int,
        note=note.strip() or None,
        occurred_at=occurred_iso,
        category_id=cat_id,
    )
    return RedirectResponse(url="/ledger", status_code=303)


@router.post("/ledger/{entry_id}/void")
async def ledger_void(
    entry_id: int,
    web_session: str | None = Cookie(default=None),
):
    user = ctx._resolve_user(web_session)
    if user is None:
        return RedirectResponse(url="/login", status_code=303)
    if ctx._ledger_store is None:
        return HTMLResponse("Ledger feature not initialised.", status_code=503)

    entry = ctx._ledger_store.get_entry(entry_id)
    if entry is None or entry["user_id"] != user.id:
        return HTMLResponse("403 Forbidden", status_code=403)

    ctx._ledger_store.void_entry(entry_id)
    return RedirectResponse(url="/ledger", status_code=303)
