"""acl.py — Scope-based access control helpers.

Only two scope values exist in FR-3:
  'private'  — readable by owner only
  'everyone' — readable by all active users

The note_shares parameter is reserved for future per-person sharing (FR-4+).
Adding it here keeps the call-site signature stable when that feature lands.
"""
from __future__ import annotations

from interfaces import User


def can_read(
    viewer: User,
    scope: str,
    owner_user_id: int,
    note_shares: list[int] | None = None,
) -> bool:
    """Return True if viewer is allowed to read a resource.

    Args:
        viewer:        The user attempting to read.
        scope:         'private' or 'everyone'.
        owner_user_id: The user_id of the resource owner.
        note_shares:   Reserved — list of user_ids granted explicit access
                       (unused in FR-3; pass None or omit).
    """
    if scope == "everyone":
        return True
    if owner_user_id == viewer.id:
        return True
    if note_shares and viewer.id in note_shares:
        return True
    return False


def filter_visible(viewer: User, rows: list[dict]) -> list[dict]:
    """Filter a list of resource rows to those readable by viewer.

    Each row must contain 'scope' (str) and 'owner_user_id' (int).
    Rows missing either field are excluded defensively.
    """
    result = []
    for row in rows:
        scope = row.get("scope")
        owner = row.get("owner_user_id")
        if scope is None or owner is None:
            continue
        if can_read(viewer, scope, owner):
            result.append(row)
    return result
