"""web_channel.py — WebChannelAdapter: ChannelAdapter implementation for the web UI (FR-5).

Delivery mechanism: each logged-in user has an asyncio.Queue. When the bot calls
send(), the reply is pushed to that queue. The SSE route (/chat/stream) drains
the queue and yields server-sent events to the browser.

One queue per user_id: if the user opens a second tab, the new connection
replaces the old one (old SSE stream stops receiving). This is acceptable for a
family-scale system.

If no SSE connection is active when send() is called (user closed the tab before
the bot finished), the message is silently dropped. The synchronous chat flow
means an active SSE connection is almost always present during a request.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Dict

from interfaces import ChannelMessage

logger = logging.getLogger(__name__)


class WebChannelAdapter:
    """ChannelAdapter for the web UI channel.

    Implements the ChannelAdapter Protocol (parse_webhook, is_authorized,
    send, delete_message) plus web-specific helpers (connect, disconnect).
    """

    CHANNEL = "web"

    def __init__(self) -> None:
        self._queues: Dict[str, asyncio.Queue] = {}

    # ── SSE queue management ───────────────────────────────────────────────────

    def connect(self, user_id: str) -> asyncio.Queue:
        """Register an SSE connection for user_id. Returns the queue to drain."""
        q: asyncio.Queue = asyncio.Queue()
        self._queues[user_id] = q
        logger.debug("web SSE connected: user_id=%s", user_id)
        return q

    def disconnect(self, user_id: str) -> None:
        """Remove the SSE queue for user_id (called when the client disconnects)."""
        self._queues.pop(user_id, None)
        logger.debug("web SSE disconnected: user_id=%s", user_id)

    # ── ChannelAdapter Protocol ────────────────────────────────────────────────

    def parse_webhook(self, payload: dict) -> ChannelMessage | None:
        # Web messages are submitted via HTTP POST to /chat/send, not via webhook.
        # This method is not used by the web channel.
        return None

    def is_authorized(self, msg: ChannelMessage) -> bool:
        # Authorization is handled at the HTTP layer (session cookie check).
        # Any ChannelMessage that reaches core_handler via the web route is
        # already authenticated.
        return True

    async def send(self, chat_id: str, text: str, use_markdown: bool = True) -> None:
        """Push a reply to the SSE queue for the given chat_id (= str(user_id)).

        If no SSE connection is active for this user, the message is dropped
        and a warning is logged.
        """
        q = self._queues.get(chat_id)
        if q is None:
            logger.warning("web send: no active SSE connection for chat_id=%s, dropping", chat_id)
            return
        await q.put(text)

    async def delete_message(self, chat_id: str, message_id: int) -> bool:
        # Web UI does not support message deletion (no Telegram deleteMessage equivalent).
        return False
