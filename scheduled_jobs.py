"""scheduled_jobs.py — APScheduler job definitions for FR-4.

Two daily jobs at 03:00 UTC+7:
  - purge_recycle_bin_180d:    hard-delete soft-deleted users/notes/wiki older
                               than 180 days; best-effort Drive cleanup.
  - purge_children_turning_18: for users whose 18th birthday was yesterday,
                               purge all their soft-deleted notes/wiki.

Both jobs use `actor_user_id=None` in audit rows to mark system events.
Drive deletion failures are recorded in audit payload but never block SQLite
purge — SQLite is the canonical store.

The job functions are sync (no awaits) because the Drive adapters are sync.
APScheduler's AsyncIOScheduler can run sync callables directly; we register
them as regular functions, not coroutines.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING

from apscheduler.triggers.cron import CronTrigger

if TYPE_CHECKING:
    from apscheduler.schedulers.base import BaseScheduler
    from deps import CoreDeps

logger = logging.getLogger(__name__)

# Bot timezone (matches timeutils.VIETNAM_TZ; redeclared here to avoid an
# import-time dependency on config in this module's signature).
VN_TZ = timezone(timedelta(hours=7))

# Retention before automatic purge of recycle-bin items.
RECYCLE_RETENTION_DAYS = 180

JOB_ID_180D = "fr4_purge_180d"
JOB_ID_TURN_18 = "fr4_purge_children_18"
JOB_ID_NOTIF_FLUSH = "fr4_flush_notifications"


# ═══════════════════════════════════════════════════════════════════════════════
# Job 1 — 180-day retention purge
# ═══════════════════════════════════════════════════════════════════════════════


def purge_recycle_bin_180d(deps: "CoreDeps", now: datetime | None = None) -> dict:
    """Hard-delete users/notes/wiki soft-deleted more than 180 days ago.

    Returns a summary dict for logging/tests:
      {users_purged, users_skipped_fk, notes_purged, wiki_purged,
       drive_delete_failures}
    """
    now = now or datetime.now(timezone.utc)
    threshold = now - timedelta(days=RECYCLE_RETENTION_DAYS)

    # notes/wiki store deleted_at as STRFTIME '%Y-%m-%dT%H:%M:%SZ'
    threshold_iso_T = threshold.strftime("%Y-%m-%dT%H:%M:%SZ")
    # users.deleted_at uses CURRENT_TIMESTAMP format '%Y-%m-%d %H:%M:%S'
    threshold_iso_space = threshold.strftime("%Y-%m-%d %H:%M:%S")

    summary = {
        "users_purged": 0,
        "users_skipped_fk": 0,
        "notes_purged": 0,
        "wiki_purged": 0,
        "drive_delete_failures": 0,
    }

    # ── Notes ─────────────────────────────────────────────────────────────────
    for n in deps.note_index.list_soft_deleted_notes_older_than(threshold_iso_T):
        meta = deps.note_index.hard_delete_note(n["id"])
        if meta is None:
            continue
        drive_ok = _try_drive_delete(deps.notes, meta.get("drive_file_id"))
        if not drive_ok:
            summary["drive_delete_failures"] += 1
        deps.audit.log(
            actor_user_id=None,
            action="recycle_purge",
            target_type="note",
            target_id=meta["id"],
            payload={
                "reason": "180d",
                "drive_file_id": meta.get("drive_file_id"),
                "drive_deleted": drive_ok,
            },
        )
        summary["notes_purged"] += 1

    # ── Wiki ──────────────────────────────────────────────────────────────────
    for w in deps.note_index.list_soft_deleted_wiki_older_than(threshold_iso_T):
        meta = deps.note_index.hard_delete_wiki(w["id"])
        if meta is None:
            continue
        drive_ok = _try_drive_delete(deps.wiki, meta.get("drive_file_id"))
        if not drive_ok:
            summary["drive_delete_failures"] += 1
        deps.audit.log(
            actor_user_id=None,
            action="recycle_purge",
            target_type="wiki",
            target_id=meta["id"],
            payload={
                "reason": "180d",
                "drive_file_id": meta.get("drive_file_id"),
                "drive_deleted": drive_ok,
            },
        )
        summary["wiki_purged"] += 1

    # ── Users ─────────────────────────────────────────────────────────────────
    for u in deps.user_store.list_deleted_users(older_than=threshold_iso_space):
        if deps.user_store.hard_delete_user(u.id):
            deps.audit.log(
                actor_user_id=None,
                action="recycle_purge",
                target_type="user",
                target_id=u.id,
                payload={"reason": "180d", "name": u.name},
            )
            summary["users_purged"] += 1
        else:
            deps.audit.log(
                actor_user_id=None,
                action="purge_skipped",
                target_type="user",
                target_id=u.id,
                payload={"reason": "fk_constraint", "name": u.name},
            )
            summary["users_skipped_fk"] += 1

    logger.info("purge_recycle_bin_180d: %s", summary)
    return summary


# ═══════════════════════════════════════════════════════════════════════════════
# Job 2 — Auto-purge for children turning 18
# ═══════════════════════════════════════════════════════════════════════════════


def purge_children_turning_18(deps: "CoreDeps", now: datetime | None = None) -> dict:
    """For each user whose 18th birthday was yesterday, purge their soft-deleted
    notes/wiki regardless of age. Live data is left untouched (Decision D5).

    Returns a summary dict per matched user, keyed by user_id:
      {user_id: {notes_purged, wiki_purged, drive_delete_failures}}
    """
    now = now or datetime.now(VN_TZ)
    # Use the bot's local date (VN_TZ) to decide "yesterday" — birthdays are
    # observed in the family's local calendar, not UTC.
    if now.tzinfo is None:
        today_local = now.date()
    else:
        today_local = now.astimezone(VN_TZ).date()
    yesterday = today_local - timedelta(days=1)

    summary: dict[int, dict] = {}

    for u in deps.user_store.find_users_turning_18(yesterday):
        per_user = {"notes_purged": 0, "wiki_purged": 0, "drive_delete_failures": 0}

        for n in deps.note_index.list_soft_deleted_notes_by_owner(u.id):
            meta = deps.note_index.hard_delete_note(n["id"])
            if meta is None:
                continue
            if not _try_drive_delete(deps.notes, meta.get("drive_file_id")):
                per_user["drive_delete_failures"] += 1
            per_user["notes_purged"] += 1

        for w in deps.note_index.list_soft_deleted_wiki_by_owner(u.id):
            meta = deps.note_index.hard_delete_wiki(w["id"])
            if meta is None:
                continue
            if not _try_drive_delete(deps.wiki, meta.get("drive_file_id")):
                per_user["drive_delete_failures"] += 1
            per_user["wiki_purged"] += 1

        deps.audit.log(
            actor_user_id=None,
            action="auto_purge_18",
            target_type="user",
            target_id=u.id,
            payload={
                "name": u.name,
                "birthdate": u.birthdate.isoformat() if u.birthdate else None,
                **per_user,
            },
        )
        summary[u.id] = per_user

    logger.info("purge_children_turning_18: matched %d users, summary=%s", len(summary), summary)
    return summary


# ═══════════════════════════════════════════════════════════════════════════════
# Job 3 — Notification flush (every 30 seconds)
# ═══════════════════════════════════════════════════════════════════════════════


async def flush_pending_notifications(
    deps: "CoreDeps", now: datetime | None = None,
) -> dict:
    """Deliver pending notifications from the queue via registered channel adapters.

    Calls NotificationService.flush_pending, which handles retry/backoff and
    audit emission internally. Returns the summary dict from that call, or an
    empty dict if no notification_service is wired.

    This job is async because channel adapters (e.g. TelegramAdapter.send) are
    coroutines. APScheduler's AsyncIOScheduler runs async callables natively.
    """
    if deps.notification_service is None:
        return {}
    try:
        summary = await deps.notification_service.flush_pending(now=now)
        if any(summary.values()):
            logger.info("flush_pending_notifications: %s", summary)
        return summary
    except Exception:
        logger.exception("flush_pending_notifications: unexpected error")
        return {}


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════


def _try_drive_delete(adapter, drive_file_id: str | None) -> bool:
    """Call adapter.delete_file safely. Returns True only on confirmed success."""
    if not drive_file_id or adapter is None:
        return False
    try:
        return bool(adapter.delete_file(drive_file_id))
    except Exception as e:
        logger.warning("Drive delete raised for file_id=%s: %s", drive_file_id, e)
        return False


# ═══════════════════════════════════════════════════════════════════════════════
# Registration
# ═══════════════════════════════════════════════════════════════════════════════


def register_jobs(scheduler: "BaseScheduler", deps: "CoreDeps") -> None:
    """Register FR-4 scheduled jobs with the given APScheduler instance.

    Both jobs run daily; the 18-birthday job is offset by 5 minutes from the
    180-day job to avoid lock contention on the same SQLite connection.
    """
    scheduler.add_job(
        purge_recycle_bin_180d,
        args=[deps],
        trigger=CronTrigger(hour=3, minute=0, timezone=VN_TZ),
        id=JOB_ID_180D,
        replace_existing=True,
    )
    scheduler.add_job(
        purge_children_turning_18,
        args=[deps],
        trigger=CronTrigger(hour=3, minute=5, timezone=VN_TZ),
        id=JOB_ID_TURN_18,
        replace_existing=True,
    )
    scheduler.add_job(
        flush_pending_notifications,
        args=[deps],
        trigger="interval",
        seconds=30,
        id=JOB_ID_NOTIF_FLUSH,
        replace_existing=True,
    )
