"""notification_service.py — Bridge between the persistent queue and channel adapters.

`NotificationService.enqueue(...)` is the API producers (FR-7 reminders, FR-4
audit failures, etc.) call. It is fast: one SQLite insert + one audit row.

`NotificationService.flush_pending()` is called by the scheduled job every
30 seconds. It pulls ready rows from the queue, sends each via the registered
channel adapter, and updates state. Audit emission per state transition
(notification_enqueued / notification_retry / notification_delivered /
notification_failed) follows FR-4-PLAN.md section 6.5.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from interfaces import AuditLog, ChannelAdapter, UserStore
    from notification_store import SqliteNotificationStore

logger = logging.getLogger(__name__)


class NotificationService:
    """Bridge between the persistent notification queue and channel adapters."""

    def __init__(
        self,
        store: "SqliteNotificationStore",
        audit: "AuditLog",
        user_store: "UserStore",
        channels: dict[str, "ChannelAdapter"],
        max_attempts: int = 5,
    ) -> None:
        self._store = store
        self._audit = audit
        self._user_store = user_store
        self._channels = dict(channels)
        self._max_attempts = max_attempts

    # ── Producer API ──────────────────────────────────────────────────────────

    def enqueue(self, user_id: int, channel: str, payload: dict[str, Any]) -> int:
        """Insert a pending notification + audit row. Returns the notification id.

        Non-blocking: does not attempt delivery. The scheduled flush job picks
        the row up on its next tick (≤30 seconds for the default schedule).
        """
        notif_id = self._store.enqueue(user_id, channel, payload)
        self._audit.log(
            actor_user_id=None,
            action="notification_enqueued",
            target_type="notification",
            target_id=notif_id,
            payload={"user_id": user_id, "channel": channel, "kind": payload.get("kind")},
        )
        return notif_id

    # ── Scheduler API ─────────────────────────────────────────────────────────

    async def flush_pending(self, now: datetime | None = None) -> dict:
        """Process pending notifications ready for delivery.

        Returns a summary dict: {delivered, retried, failed, processed}.
        Safe to call repeatedly; idempotent on already-delivered/failed rows.
        """
        now = now or datetime.now(timezone.utc)
        summary = {"delivered": 0, "retried": 0, "failed": 0, "processed": 0}

        for notif in self._store.get_pending_ready(now=now, limit=100):
            summary["processed"] += 1
            try:
                await self._attempt_delivery(notif, now=now)
                summary["delivered"] += 1
            except _RetryableDeliveryError as exc:
                final = self._handle_failure(notif, str(exc), now=now)
                if final:
                    summary["failed"] += 1
                else:
                    summary["retried"] += 1
        return summary

    # ── Internal ──────────────────────────────────────────────────────────────

    async def _attempt_delivery(self, notif: dict, now: datetime) -> None:
        """Try to deliver one notification.

        Raises `_RetryableDeliveryError` on any failure (no channel adapter,
        no chat_id binding, send exception). On success, marks delivered and
        emits the audit row.
        """
        channel_name = notif["channel"]
        adapter = self._channels.get(channel_name)
        if adapter is None:
            raise _RetryableDeliveryError(f"channel_not_supported: {channel_name}")

        chat_id = self._user_store.get_chat_id_for_user(notif["user_id"], channel_name)
        if chat_id is None:
            raise _RetryableDeliveryError("no_binding")

        try:
            payload = json.loads(notif["payload"])
        except (TypeError, json.JSONDecodeError) as e:
            # Corrupt payload — treat as transient; will burn retries until final.
            raise _RetryableDeliveryError(f"invalid_payload: {e}") from None

        text = payload.get("text")
        if not text:
            raise _RetryableDeliveryError("empty_text")

        try:
            await adapter.send(chat_id, text, use_markdown=False)
        except Exception as e:  # noqa: BLE001 — adapter errors are intentionally broad
            raise _RetryableDeliveryError(str(e)) from e

        # Success: mark delivered + audit.
        self._store.mark_delivered(notif["id"], now=now)
        self._audit.log(
            actor_user_id=None,
            action="notification_delivered",
            target_type="notification",
            target_id=notif["id"],
            payload={"total_attempts": notif["attempts"] + 1},
        )

    def _handle_failure(self, notif: dict, error: str, now: datetime) -> bool:
        """Record a failed attempt + emit retry/final-fail audit. Returns True if final."""
        result = self._store.record_failed_attempt(
            notif["id"], error, max_attempts=self._max_attempts, now=now,
        )
        if result["final"]:
            self._audit.log(
                actor_user_id=None,
                action="notification_failed",
                target_type="notification",
                target_id=notif["id"],
                payload={
                    "last_error": error[:500],
                    "total_attempts": result["attempts"],
                },
            )
            return True
        self._audit.log(
            actor_user_id=None,
            action="notification_retry",
            target_type="notification",
            target_id=notif["id"],
            payload={
                "attempt": result["attempts"],
                "error": error[:500],
                "next_retry_at": result["next_retry_at"],
            },
        )
        return False


class _RetryableDeliveryError(Exception):
    """Internal signal: this notification could not be delivered this round."""
