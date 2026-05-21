"""acl.py — Scope-based access control helpers.

Scope values:
  'private'  — readable by owner only (with stealth-read exception below)
  'everyone' — readable by all active users

FR-4 stealth-read: an admin viewer may read another user's PRIVATE content if
all the following hold:
  - viewer.role == 'admin'
  - owner has at least one active parent_link entry
  - owner.birthdate is set AND age(owner) < 18

When stealth-read is the only reason access is granted, the returned tuple's
second element (`is_stealth`) is True so the caller can write an audit row.
The note_shares parameter is reserved for future per-person sharing.
"""
from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

from interfaces import User

if TYPE_CHECKING:
    from interfaces import UserStore


def can_read(
    viewer: User,
    scope: str,
    owner_user_id: int,
    *,
    user_store: "UserStore | None" = None,
    note_shares: list[int] | None = None,
) -> tuple[bool, bool]:
    """Return (allowed, is_stealth).

    `is_stealth` is True iff access was granted solely by the FR-4 admin-reads-
    minor-child rule. In every other "allowed" path it is False. Callers are
    expected to log an audit row when stealth is True.

    Backward-compatible behavior: when `user_store` is None, the stealth path
    is disabled — the function behaves exactly as in FR-3.
    """
    # Public scope wins immediately, regardless of viewer.
    if scope == "everyone":
        return (True, False)

    # Owner reading own content is never stealth.
    if owner_user_id == viewer.id:
        return (True, False)

    # Explicit per-resource shares (reserved for future) also bypass stealth.
    if note_shares and viewer.id in note_shares:
        return (True, False)

    # Stealth-read path (FR-4). Only possible when caller supplied user_store.
    if (
        user_store is not None
        and viewer.role == "admin"
        and _is_minor_child(owner_user_id, user_store)
    ):
        return (True, True)

    return (False, False)


def filter_visible(
    viewer: User,
    rows: list[dict],
    *,
    user_store: "UserStore | None" = None,
) -> list[dict]:
    """Filter resource rows to those the viewer may read.

    Each row must contain 'scope' and 'owner_user_id'. Rows missing either are
    excluded defensively. Rows admitted via the stealth-read path get an
    additional key `is_stealth_read=True` (the caller decides whether to log
    audit rows). A shallow copy is made before mutation to avoid surprising
    the caller's data.
    """
    result: list[dict] = []
    for row in rows:
        scope = row.get("scope")
        owner = row.get("owner_user_id")
        if scope is None or owner is None:
            continue
        allowed, is_stealth = can_read(
            viewer, scope, owner, user_store=user_store
        )
        if not allowed:
            continue
        if is_stealth:
            row = {**row, "is_stealth_read": True}
        result.append(row)
    return result


# ── Internal helpers ──────────────────────────────────────────────────────────


def _is_minor_child(owner_user_id: int, user_store: "UserStore") -> bool:
    """Return True iff owner has an active parent_link AND age < 18.

    Both conditions are required (FR-4-PLAN.md decision D3). Returns False
    defensively if the user is unknown, has no birthdate, or has no parent.
    """
    parent = user_store.get_parent(owner_user_id)
    if parent is None:
        return False
    owner = user_store.get_user_by_id(owner_user_id)
    if owner is None or owner.birthdate is None:
        return False
    return _age_in_years(owner.birthdate, date.today()) < 18


def _age_in_years(birthdate: date, today: date) -> int:
    """Whole-year age, decrementing if the birthday hasn't happened yet this year.

    Handles Feb 29 birthdates by treating Mar 1 in non-leap years as the
    effective birthday (i.e. someone born 2008-02-29 is 18 on 2026-03-01,
    still 17 on 2026-02-28).
    """
    years = today.year - birthdate.year
    # Has the birthday passed this year? Compare (month, day) tuples.
    birth_md = (birthdate.month, birthdate.day)
    today_md = (today.month, today.day)
    if today_md < birth_md:
        years -= 1
    return years
