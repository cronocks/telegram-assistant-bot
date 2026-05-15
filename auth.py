"""auth.py — Password hashing and verification using argon2id.

Pure functions with no DB dependency. The UserStore layer calls these
to produce/consume hashes stored in users.password_hash.
"""
from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, VerificationError, InvalidHashError

_ph = PasswordHasher()


def hash_password(plain: str) -> str:
    """Return an argon2id hash of plain."""
    return _ph.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    """Return True if plain matches hashed, False otherwise.

    Never raises — bad hash or mismatch both return False.
    """
    try:
        return _ph.verify(hashed, plain)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(hashed: str) -> bool:
    """Return True if the hash was produced with outdated parameters."""
    return _ph.check_needs_rehash(hashed)
