"""web_channel.py — WebChannelAdapter: ChannelAdapter implementation for the web UI (FR-5/5.5).

Delivery mechanism: each active conversation has an asyncio.Queue keyed by
conversation_id (str). When the bot calls send(), the reply is pushed to that
queue. The SSE route (/chat/stream?conversation_id=X) drains the queue and
yields server-sent events to the browser.

FR-5.5 change (vs FR-5): queues are now keyed by conversation_id instead of
user_id. This allows multi-tab users to have separate conversations open
simultaneously, with each tab receiving only its own replies.

One queue per conversation_id: if the user opens the same conversation in a
second tab, the new SSE connection replaces the old one. Messages are dropped
if no SSE connection is active when send() is called (user closed the tab
before the bot finished).
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Dict

from interfaces import ChannelMessage

logger = logging.getLogger(__name__)


class WebChannelAdapter:
    """ChannelAdapter for the web UI channel.

    Implements the ChannelAdapter Protocol (parse_webhook, is_authorized,
    send, delete_message) plus web-specific helpers (connect, disconnect).

    SSE events are JSON-encoded with a 'type' field:
        {"type": "message", "text": "..."}
        {"type": "title_update", "title": "..."}
    """

    CHANNEL = "web"

    def __init__(self) -> None:
        # Keyed by str(conversation_id) — one queue per active SSE connection.
        self._queues: Dict[str, asyncio.Queue] = {}
        # Buffer for messages sent before the SSE connection is established.
        # Race condition: POST /chat/send creates the conv and spawns the handler
        # task immediately; the bot reply may arrive before the frontend reconnects
        # SSE with the new conversation_id. Messages are held here and flushed
        # into the queue as soon as connect() is called.
        self._pending: Dict[str, list] = {}
        self._conv_store = None  # injected after startup via set_conv_store()

    def set_conv_store(self, conv_store) -> None:
        """Wire the conversation store so send() can persist bot replies."""
        self._conv_store = conv_store

    # ── SSE queue management ───────────────────────────────────────────────────

    def connect(self, conv_id: str) -> asyncio.Queue:
        """Register an SSE connection for conv_id. Returns the queue to drain.

        Any messages buffered in _pending (sent before this SSE connection was
        established) are flushed into the queue immediately so the client
        receives them without delay.
        """
        q: asyncio.Queue = asyncio.Queue()
        # Drain any buffered messages before handing the queue to the SSE route.
        for event in self._pending.pop(conv_id, []):
            q.put_nowait(event)
        self._queues[conv_id] = q
        logger.debug("web SSE connected: conv_id=%s", conv_id)
        return q

    def disconnect(self, conv_id: str) -> None:
        """Remove the SSE queue for conv_id (called when the client disconnects)."""
        self._queues.pop(conv_id, None)
        logger.debug("web SSE disconnected: conv_id=%s", conv_id)

    def push_title_update(self, conv_id: str, title: str) -> bool:
        """Push a title_update event to the SSE queue for conv_id.

        Called after async LLM title generation completes. Returns True if an
        active SSE connection was found; False if the event was dropped.
        """
        q = self._queues.get(str(conv_id))
        if q is None:
            return False
        event = json.dumps({"type": "title_update", "title": title})
        q.put_nowait(event)
        return True

    # ── ChannelAdapter Protocol ────────────────────────────────────────────────

    def parse_webhook(self, payload: dict) -> ChannelMessage | None:
        # Web messages arrive via HTTP POST to /chat/<id>/send, not via webhook.
        return None

    def is_authorized(self, msg: ChannelMessage) -> bool:
        # Authorization is handled at the HTTP layer (session cookie check).
        # Any ChannelMessage reaching core_handler via the web route is already
        # authenticated.
        return True

    async def send(self, chat_id: str, text: str, use_markdown: bool = True) -> None:
        """Push a reply to the SSE queue for the given chat_id (= str(conversation_id)).

        Also persists the bot reply to the DB if conv_store is wired.
        If no SSE connection is active, the message is dropped and a warning logged.
        """
        # Persist bot reply before pushing to queue
        if self._conv_store is not None:
            try:
                self._conv_store.add_message(int(chat_id), "bot", text)
            except Exception:
                logger.exception("web send: failed to persist bot message for conv_id=%s", chat_id)

        q = self._queues.get(chat_id)
        event = json.dumps({"type": "message", "text": text})
        if q is None:
            # SSE not yet connected — buffer the event; connect() will flush it.
            self._pending.setdefault(chat_id, []).append(event)
            logger.debug(
                "web send: SSE not ready for conv_id=%s, buffering message", chat_id
            )
            return
        await q.put(event)

    async def delete_message(self, chat_id: str, message_id: int) -> bool:
        # Web UI does not support message deletion.
        return False
