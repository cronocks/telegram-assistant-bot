"""permissions.py — Role-based access control helpers.

Pure functions that operate on User objects; no DB dependency.
"""
from __future__ import annotations

from datetime import date

from interfaces import User

# Role hierarchy (higher index = more privilege)
_ROLE_RANK: dict[str, int] = {
    "readonly": 0,
    "member":   1,
    "manager":  2,
    "admin":    3,
}


def role_rank(role: str) -> int:
    """Return numeric rank of a role string (higher = more privilege)."""
    return _ROLE_RANK.get(role, -1)


def has_role(user: User, *roles: str) -> bool:
    """Return True if user's role is one of the given roles."""
    return user.role in roles


def can_manage(user: User) -> bool:
    """Admin or manager — can approve requests and view all users."""
    return has_role(user, "admin", "manager")


def age_of(user: User, today: date | None = None) -> int:
    """Return age in full years, or -1 if birthdate is unknown."""
    if user.birthdate is None:
        return -1
    today = today or date.today()
    age = today.year - user.birthdate.year
    # Subtract 1 if birthday has not yet occurred this calendar year.
    if (today.month, today.day) < (user.birthdate.month, user.birthdate.day):
        age -= 1
    return age


def is_adult(user: User, today: date | None = None) -> bool:
    """Return True if user is 18 or older. False if unknown birthdate."""
    return age_of(user, today) >= 18
