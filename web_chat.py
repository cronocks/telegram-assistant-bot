"""web_chat.py — Chat UI routes, SSE stream, and conversation REST API."""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Cookie, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sse_starlette.sse import EventSourceResponse

import web_context as ctx

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/chat", response_class=HTMLResponse)
async def chat_page(request: Request, web_session: str | None = Cookie(default=None)):
    user = ctx._resolve_user(web_session)
    if user is None:
        return RedirectResponse(url="/login", status_code=303)

    assert ctx._user_store is not None
    if ctx._user_store.get_must_change_password(user.id):
        return RedirectResponse(url="/setup-password", status_code=303)

    assert ctx._templates is not None
    assert ctx._conv_store is not None
    conversations = ctx._conv_store.list_for_user(user.id)
    return ctx._templates.TemplateResponse(
        request, "chat.html",
        {"user": user, "conversations": conversations, "active_conv": None, "messages": []},
    )


# SSE stream — must be declared before /chat/{conv_id} to prevent ambiguous match.

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
    user = ctx._resolve_user(web_session)
    if user is None:
        return RedirectResponse(url="/login", status_code=303)

    assert ctx._web_channel is not None
    assert ctx._conv_store is not None

    if conversation_id is not None:
        conv = ctx._get_conv_or_403(conversation_id, user)
        if conv is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        queue_key = str(conversation_id)
    else:
        # Pending new conversation — use a temporary user-scoped key until
        # the frontend receives conversation_id from /chat/send and reconnects.
        queue_key = f"pending_{user.id}"

    q = ctx._web_channel.connect(queue_key)

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
            ctx._web_channel.disconnect(queue_key)

    return EventSourceResponse(event_generator())


@router.get("/chat/{conv_id}", response_class=HTMLResponse)
async def chat_conversation_page(
    conv_id: int,
    request: Request,
    web_session: str | None = Cookie(default=None),
):
    user = ctx._resolve_user(web_session)
    if user is None:
        return RedirectResponse(url="/login", status_code=303)

    assert ctx._user_store is not None
    if ctx._user_store.get_must_change_password(user.id):
        return RedirectResponse(url="/setup-password", status_code=303)

    assert ctx._conv_store is not None
    conv = ctx._get_conv_or_403(conv_id, user)
    if conv is None:
        return RedirectResponse(url="/chat", status_code=303)

    conversations = ctx._conv_store.list_for_user(user.id)
    messages = ctx._conv_store.list_messages(conv_id)
    assert ctx._templates is not None
    return ctx._templates.TemplateResponse(
        request, "chat.html",
        {"user": user, "conversations": conversations, "active_conv": conv, "messages": messages},
    )


async def _generate_title_bg(conv_id: int, user_text: str, web_deps) -> None:
    """Background task: generate title after first exchange and push SSE update."""
    assert ctx._conv_store is not None
    try:
        title, _ = web_deps.llm.generate_chat_title(
            user_text[:300],
            next(
                (m["text"] for m in reversed(ctx._conv_store.list_messages(conv_id)) if m["role"] == "bot"),
                "",
            ),
        )
    except Exception:
        logger.exception("title gen failed for conv_id=%s, using fallback", conv_id)
        title = user_text[:40].strip() + ("…" if len(user_text) > 40 else "")

    if not title:
        return

    written = ctx._conv_store.set_title_if_null(conv_id, title)
    if written and ctx._web_channel is not None:
        ctx._web_channel.push_title_update(str(conv_id), title)


async def _handle_and_maybe_title(msg, user, web_deps, conv_id: int, user_text: str) -> None:
    """Wrapper: run handle_message then trigger title gen on first exchange."""
    from core_handler import handle_message
    await handle_message(msg, user, web_deps)
    assert ctx._conv_store is not None
    if ctx._conv_store.count_messages(conv_id) == 2:  # user msg + first bot reply
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

    user = ctx._resolve_user(web_session)
    if user is None:
        return JSONResponse({"error": "unauthenticated"}, status_code=401)

    assert ctx._conv_store is not None
    assert ctx._audit is not None

    clean_text = text.strip()
    if not clean_text:
        return JSONResponse({"error": "empty message"}, status_code=400)

    conv_id = ctx._conv_store.create(user.id)
    ctx._audit.log(user.id, "web_conversation_created", "web_conversation", str(conv_id))
    ctx._conv_store.add_message(conv_id, "user", clean_text)

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

    user = ctx._resolve_user(web_session)
    if user is None:
        return JSONResponse({"error": "unauthenticated"}, status_code=401)

    assert ctx._conv_store is not None
    conv = ctx._get_conv_or_403(conv_id, user)
    if conv is None:
        return JSONResponse({"error": "not found"}, status_code=404)

    clean_text = text.strip()
    if not clean_text:
        return JSONResponse({"error": "empty message"}, status_code=400)

    ctx._conv_store.add_message(conv_id, "user", clean_text)

    msg = ChannelMessage(channel="web", chat_id=str(conv_id), text=clean_text)
    web_deps = getattr(request.app.state, "web_deps", None)
    if web_deps is None:
        logger.error("web_deps not wired — check main.py lifespan")
        return JSONResponse({"error": "server error"}, status_code=500)

    asyncio.create_task(handle_message(msg, user, web_deps))
    return HTMLResponse("", status_code=204)


# ── Conversation REST API ─────────────────────────────────────────────────────

@router.get("/api/conversations")
async def api_list_conversations(web_session: str | None = Cookie(default=None)):
    user = ctx._resolve_user(web_session)
    if user is None:
        return JSONResponse({"error": "unauthenticated"}, status_code=401)
    assert ctx._conv_store is not None
    convs = ctx._conv_store.list_for_user(user.id)
    return JSONResponse(convs)


@router.get("/api/conversations/search")
async def api_search_conversations(
    q: str = Query(default=""),
    web_session: str | None = Cookie(default=None),
):
    user = ctx._resolve_user(web_session)
    if user is None:
        return JSONResponse({"error": "unauthenticated"}, status_code=401)
    assert ctx._conv_store is not None
    if not q.strip():
        return JSONResponse([])
    results = ctx._conv_store.search(user.id, q.strip())
    return JSONResponse(results)


@router.get("/api/conversations/{conv_id}/messages")
async def api_get_messages(
    conv_id: int,
    web_session: str | None = Cookie(default=None),
):
    user = ctx._resolve_user(web_session)
    if user is None:
        return JSONResponse({"error": "unauthenticated"}, status_code=401)
    assert ctx._conv_store is not None
    conv = ctx._get_conv_or_403(conv_id, user)
    if conv is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    messages = ctx._conv_store.list_messages(conv_id)
    return JSONResponse(messages)


@router.patch("/api/conversations/{conv_id}")
async def api_rename_conversation(
    conv_id: int,
    request: Request,
    web_session: str | None = Cookie(default=None),
):
    """Rename a conversation. Body: {"title": "..."}"""
    user = ctx._resolve_user(web_session)
    if user is None:
        return JSONResponse({"error": "unauthenticated"}, status_code=401)
    assert ctx._conv_store is not None
    assert ctx._audit is not None

    conv = ctx._get_conv_or_403(conv_id, user)
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
    ctx._conv_store.rename(conv_id, new_title)
    ctx._audit.log(
        user.id, "web_conversation_renamed", "web_conversation", str(conv_id),
        {"old": old_title, "new": new_title},
    )
    return JSONResponse({"ok": True, "title": new_title})
