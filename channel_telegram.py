"""channel_telegram.py — Telegram-backed implementation of ChannelAdapter.

Parses incoming Telegram webhook payloads into ChannelMessage and posts
outbound replies through the Bot API. The hard-coded chat_id whitelist
(`is_authorized`) will be replaced by a user registry in FR-2.
"""
import httpx

from interfaces import ChannelMessage


class TelegramAdapter:
    """ChannelAdapter impl for Telegram."""

    def __init__(self, token: str, allowed_chat_id: str | int):
        self._token = token
        self._allowed_chat_id = str(allowed_chat_id)

    # ─── Inbound ────────────────────────────────────────────────────────────

    def parse_webhook(self, payload: dict) -> ChannelMessage | None:
        """Convert a Telegram webhook payload into a ChannelMessage.

        Handles two update types:
        - message / edited_message  → normal text message
        - callback_query            → inline keyboard button tap; raw contains
                                      'callback_data' and 'callback_query_id'

        Returns None if the payload should be ignored.
        """
        message = payload.get("message") or payload.get("edited_message")
        if message:
            chat_id = str(message["chat"]["id"])
            text = message.get("text", "")
            if not text:
                return None
            return ChannelMessage(
                channel="telegram",
                chat_id=chat_id,
                text=text,
                raw=message,
            )

        cq = payload.get("callback_query")
        if cq:
            chat_id = str(cq["message"]["chat"]["id"])
            return ChannelMessage(
                channel="telegram",
                chat_id=chat_id,
                text="",
                raw={
                    "callback_data": cq.get("data", ""),
                    "callback_query_id": cq["id"],
                    "message_id": cq["message"]["message_id"],
                    "from_user": cq.get("from", {}),
                },
            )

        return None

    def is_authorized(self, msg: ChannelMessage) -> bool:
        """Single-user authorization: chat_id must match the configured allowlist.

        This is the current "security layer 7"; FR-2 replaces it with a user
        table + channel_bindings.
        """
        return msg.chat_id == self._allowed_chat_id

    # ─── Outbound ───────────────────────────────────────────────────────────

    async def send(
        self, chat_id: str, text: str, use_markdown: bool = True
    ) -> None:
        """Post a message to the given Telegram chat."""
        url = f"https://api.telegram.org/bot{self._token}/sendMessage"
        payload: dict = {"chat_id": chat_id, "text": text}
        if use_markdown:
            payload["parse_mode"] = "Markdown"
        async with httpx.AsyncClient() as client:
            try:
                resp = await client.post(url, json=payload, timeout=15)
                if resp.status_code != 200:
                    print(f"[telegram] Send failed {resp.status_code}: {resp.text[:300]}")
            except Exception as e:
                print(f"[telegram] Send error: {e}")

    async def send_with_inline_keyboard(
        self,
        chat_id: str,
        text: str,
        buttons: list[list[dict]],
        use_markdown: bool = False,
    ) -> None:
        """Send a message with a Telegram inline keyboard (reply_markup)."""
        url = f"https://api.telegram.org/bot{self._token}/sendMessage"
        payload: dict = {
            "chat_id": chat_id,
            "text": text,
            "reply_markup": {"inline_keyboard": buttons},
        }
        if use_markdown:
            payload["parse_mode"] = "Markdown"
        async with httpx.AsyncClient() as client:
            try:
                resp = await client.post(url, json=payload, timeout=15)
                if resp.status_code != 200:
                    print(
                        f"[telegram] send_with_inline_keyboard failed "
                        f"{resp.status_code}: {resp.text[:300]}"
                    )
            except Exception as e:
                print(f"[telegram] send_with_inline_keyboard error: {e}")

    async def answer_callback_query(
        self, callback_query_id: str, text: str = ""
    ) -> None:
        """Answer a callback_query to stop the loading spinner on the button."""
        url = f"https://api.telegram.org/bot{self._token}/answerCallbackQuery"
        payload: dict = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        async with httpx.AsyncClient() as client:
            try:
                resp = await client.post(url, json=payload, timeout=15)
                if resp.status_code != 200:
                    print(
                        f"[telegram] answerCallbackQuery failed "
                        f"{resp.status_code}: {resp.text[:300]}"
                    )
            except Exception as e:
                print(f"[telegram] answerCallbackQuery error: {e}")

    async def delete_message(self, chat_id: str, message_id: int) -> bool:
        """Delete a message via Telegram deleteMessage API. Returns True on success.

        Used for password hygiene — erasing `sudo:` and `dat mat khau:` messages
        after the bot has processed them. Failures are logged but never raised
        (the bot's primary flow must not abort if cleanup fails).
        """
        url = f"https://api.telegram.org/bot{self._token}/deleteMessage"
        payload = {"chat_id": chat_id, "message_id": message_id}
        async with httpx.AsyncClient() as client:
            try:
                resp = await client.post(url, json=payload, timeout=15)
                if resp.status_code != 200:
                    print(
                        f"[telegram] deleteMessage failed {resp.status_code}: "
                        f"{resp.text[:300]}"
                    )
                    return False
                return True
            except Exception as e:
                print(f"[telegram] deleteMessage error: {e}")
                return False
