"""anniversary_engine.py — FR-8 anniversary reminder lifecycle.

`AnniversaryEngine` drives the annual lifecycle:
  - `compute_year(year)`: for each active anniversary, compute its solar date in
    the target year and insert one reminder row per offset_days. Idempotent via
    UNIQUE(anniversary_id, year, offset_days) on the table.
  - `tick()`: called every minute by APScheduler; fires due reminders and marks
    overdue-by-grace ones as missed (12h window — anniversaries are day-based).
  - `cancel_all_for_anniversary(id)`: cancels all pending reminders for an
    anniversary on delete or disable.

Reminder rows fire at 08:00 VN on each scheduled day (anniversaries are
day-based — early morning ping makes sense as a heads-up).
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import date, datetime, timedelta
from typing import Callable

from lunar_utils import compute_anniversary_solar_date, day_of_week_vn
from timeutils import VIETNAM_TZ

logger = logging.getLogger(__name__)

# Reminders fire at 08:00 VN — early enough to plan the day around the event.
FIRE_HOUR = 8
FIRE_MINUTE = 0

# Grace window: reminders overdue by more than this are marked 'missed'.
# Larger than ReminderEngine's 1h because anniversaries are day-scoped.
GRACE_HOURS = 12


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _parse_iso(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=VIETNAM_TZ)
    return dt


def _parse_offsets(offsets_csv: str) -> list[int]:
    return [int(x.strip()) for x in offsets_csv.split(",") if x.strip()]


def _age_in_years(birthdate: date, today: date) -> int:
    years = today.year - birthdate.year
    if (today.month, today.day) < (birthdate.month, birthdate.day):
        years -= 1
    return years


class AnniversaryEngine:
    def __init__(
        self,
        anniv_store,
        user_store,
        notification_service,
        audit,
        conn: sqlite3.Connection,
        now_fn: Callable[[], datetime] | None = None,
        burial_store=None,
    ) -> None:
        self._anniv = anniv_store
        self._users = user_store
        self._notif = notification_service
        self._audit = audit
        self._conn = conn
        self._now_fn = now_fn or (lambda: datetime.now(VIETNAM_TZ))
        self._burial_store = burial_store

    # ── compute_year ──────────────────────────────────────────────────────────

    def compute_year(self, year: int) -> int:
        """Generate reminder rows for all active anniversaries for `year`.

        Idempotent — UNIQUE(anniversary_id, year, offset_days) means repeat
        invocations only insert truly-new rows.
        Returns the number of rows actually inserted.
        """
        inserted = 0
        now_iso = _iso(self._now_fn())
        for a in self._anniv.list_all_active():
            try:
                anniv_date = compute_anniversary_solar_date(
                    a["date_type"], a["month"], a["day"], year,
                    is_leap_month=bool(a.get("is_leap_month", 0)),
                )
            except Exception:
                logger.exception(
                    "compute_year: failed to compute date for anniversary %s",
                    a["id"],
                )
                continue
            offsets = _parse_offsets(a["reminder_offsets"])
            for offset_days in offsets:
                fire_date = anniv_date - timedelta(days=offset_days)
                fire_dt = datetime(
                    fire_date.year, fire_date.month, fire_date.day,
                    FIRE_HOUR, FIRE_MINUTE, tzinfo=VIETNAM_TZ,
                )
                cur = self._conn.execute(
                    """
                    INSERT OR IGNORE INTO anniversary_reminders
                        (anniversary_id, year, fire_at, offset_days, status, created_at)
                    VALUES (?, ?, ?, ?, 'pending', ?)
                    """,
                    (a["id"], year, _iso(fire_dt), offset_days, now_iso),
                )
                inserted += cur.rowcount
        self._conn.commit()
        return inserted

    # ── tick ──────────────────────────────────────────────────────────────────

    def tick(self) -> dict:
        """Process all reminders due now. Called every minute by APScheduler."""
        now = self._now_fn()
        now_iso = _iso(now)
        grace_cutoff = now - timedelta(hours=GRACE_HOURS)
        stats = {"fired": 0, "missed": 0}

        rows = self._conn.execute(
            """
            SELECT ar.*, a.user_id, a.name, a.category, a.enabled,
                   a.deleted_at AS anniv_deleted_at, a.family_member_id
            FROM anniversary_reminders ar
            JOIN anniversaries a ON a.id = ar.anniversary_id
            WHERE ar.status = 'pending' AND ar.fire_at <= ?
            ORDER BY ar.fire_at ASC, ar.id ASC
            """,
            (now_iso,),
        ).fetchall()

        for row in rows:
            row = dict(row)
            fire_at = _parse_iso(row["fire_at"])

            # Skip if anniversary was disabled or soft-deleted after compute.
            if row["enabled"] != 1 or row["anniv_deleted_at"] is not None:
                self._cancel_reminder(row["id"])
                continue

            # Grace window check.
            if fire_at < grace_cutoff:
                self._mark_missed(row["id"])
                self._audit.log(
                    actor_user_id=None,
                    action="anniversary_reminder_missed",
                    target_type="anniversary",
                    target_id=row["anniversary_id"],
                    payload={
                        "fire_at": row["fire_at"],
                        "missed_seconds": int((now - fire_at).total_seconds()),
                    },
                )
                stats["missed"] += 1
                continue

            owner = self._users.get_user_by_id(row["user_id"])
            if owner is None:
                self._mark_missed(row["id"])
                stats["missed"] += 1
                continue

            self._emit(row, owner)
            self._mark_fired(row["id"], now_iso)
            stats["fired"] += 1

        return stats

    # ── cancel_all_for_anniversary ────────────────────────────────────────────

    def cancel_all_for_anniversary(self, anniversary_id: int) -> int:
        with self._conn:
            cur = self._conn.execute(
                "UPDATE anniversary_reminders SET status = 'cancelled' "
                "WHERE anniversary_id = ? AND status = 'pending'",
                (anniversary_id,),
            )
        return cur.rowcount

    # ── Internal ──────────────────────────────────────────────────────────────

    def _emit(self, row: dict, owner) -> None:
        burial = None
        if self._burial_store is not None and row.get("family_member_id"):
            burial = self._burial_store.get_current_for_member(row["family_member_id"])
        text = _build_text(row, burial=burial)
        payload = {
            "kind": "anniversary",
            "anniversary_id": row["anniversary_id"],
            "text": text,
            "offset_days": row["offset_days"],
        }
        self._notif.enqueue(owner.id, "telegram", payload)
        mirrored_to: list[int] = []

        # Parent mirror — runtime age check (Decision #22).
        if owner.birthdate is not None:
            today_vn = self._now_fn().astimezone(VIETNAM_TZ).date()
            if _age_in_years(owner.birthdate, today_vn) < 18:
                try:
                    parent = self._users.get_parent(owner.id)
                except Exception:
                    parent = None
                if parent is not None:
                    mirror = {**payload, "mirrored_from_user_id": owner.id}
                    self._notif.enqueue(parent.id, "telegram", mirror)
                    mirrored_to.append(parent.id)

        self._audit.log(
            actor_user_id=None,
            action="anniversary_reminder_fired",
            target_type="anniversary",
            target_id=row["anniversary_id"],
            payload={
                "offset_days": row["offset_days"],
                "channels_delivered": ["telegram"],
                "mirrored_to_parent": mirrored_to,
            },
        )

    def _mark_fired(self, reminder_id: int, fired_at_iso: str) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE anniversary_reminders SET status = 'fired', fired_at = ? "
                "WHERE id = ? AND status = 'pending'",
                (fired_at_iso, reminder_id),
            )

    def _mark_missed(self, reminder_id: int) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE anniversary_reminders SET status = 'missed' "
                "WHERE id = ? AND status = 'pending'",
                (reminder_id,),
            )

    def _cancel_reminder(self, reminder_id: int) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE anniversary_reminders SET status = 'cancelled' "
                "WHERE id = ? AND status = 'pending'",
                (reminder_id,),
            )


def _build_text(row: dict, burial: dict | None = None) -> str:
    """Compose the user-facing reminder text with solar date and day-of-week.

    When burial is provided and category is 'gio', appends burial location info
    (cemetery name, address, GPS link, plot info).
    """
    name = row.get("name", "Kỷ niệm")
    offset = row.get("offset_days", 0)
    category = row.get("category", "khac")
    emoji = {"gio": "🕯", "cuoi": "💐", "khac": "📅"}.get(category, "📅")

    # The anniversary solar date = fire_at date + offset_days.
    fire_at_date = _parse_iso(row["fire_at"]).date()
    anniv_date = fire_at_date + timedelta(days=offset)
    dow = day_of_week_vn(anniv_date)
    date_str = f"{anniv_date.day:02d}/{anniv_date.month:02d}/{anniv_date.year}"

    if offset == 0:
        text = f"{emoji} Hôm nay: {name} — {dow}, {date_str}"
    elif offset == 1:
        text = f"{emoji} Ngày mai: {name} — {dow}, {date_str}"
    else:
        text = f"{emoji} Còn {offset} ngày: {name} — {dow}, {date_str}"

    if burial and category == "gio":
        cemetery = burial.get("cemetery_name", "")
        if cemetery:
            text += f"\n🪦 {cemetery}"
        if burial.get("address"):
            text += f"\n📍 {burial['address']}"
        lat, lng = burial.get("lat"), burial.get("lng")
        if lat is not None and lng is not None:
            text += f"\n🗺 https://maps.google.com/?q={lat},{lng}"
        if burial.get("plot_info"):
            text += f"\n🧭 {burial['plot_info']}"

    return text
