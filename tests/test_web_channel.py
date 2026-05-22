"""tests/test_web_channel.py — Unit tests for WebChannelAdapter (FR-5)."""
import asyncio

import pytest

from web_channel import WebChannelAdapter


@pytest.fixture()
def adapter():
    return WebChannelAdapter()


class TestConnect:
    def test_connect_returns_queue(self, adapter):
        q = adapter.connect("1")
        assert isinstance(q, asyncio.Queue)

    def test_connect_overwrites_previous_queue(self, adapter):
        q1 = adapter.connect("1")
        q2 = adapter.connect("1")
        assert q1 is not q2
        assert adapter._queues["1"] is q2

    def test_disconnect_removes_queue(self, adapter):
        adapter.connect("1")
        adapter.disconnect("1")
        assert "1" not in adapter._queues

    def test_disconnect_unknown_user_is_safe(self, adapter):
        adapter.disconnect("999")  # must not raise


class TestSend:
    @pytest.mark.anyio
    async def test_send_pushes_to_queue(self, adapter):
        q = adapter.connect("1")
        await adapter.send("1", "hello")
        assert not q.empty()
        assert await q.get() == "hello"

    @pytest.mark.anyio
    async def test_send_no_connection_drops_silently(self, adapter):
        # No queue registered — must not raise
        await adapter.send("99", "dropped")

    @pytest.mark.anyio
    async def test_send_isolated_per_user(self, adapter):
        q1 = adapter.connect("1")
        q2 = adapter.connect("2")
        await adapter.send("1", "for-user-1")
        assert not q1.empty()
        assert q2.empty()

    @pytest.mark.anyio
    async def test_multiple_messages_ordered(self, adapter):
        q = adapter.connect("1")
        for msg in ["a", "b", "c"]:
            await adapter.send("1", msg)
        results = [await q.get() for _ in range(3)]
        assert results == ["a", "b", "c"]


class TestDeleteMessage:
    @pytest.mark.anyio
    async def test_delete_message_returns_false(self, adapter):
        result = await adapter.delete_message("1", 42)
        assert result is False


class TestIsAuthorized:
    def test_always_true(self, adapter):
        from interfaces import ChannelMessage
        msg = ChannelMessage(channel="web", chat_id="1", text="hi")
        assert adapter.is_authorized(msg) is True


class TestParseWebhook:
    def test_returns_none(self, adapter):
        assert adapter.parse_webhook({"any": "payload"}) is None
