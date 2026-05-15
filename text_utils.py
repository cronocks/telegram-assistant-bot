"""text_utils.py — Vietnamese text normalization and command prefix matching."""
from __future__ import annotations

import re
import unicodedata


# ── Public API ────────────────────────────────────────────────────────────────

def normalize_vn(text: str) -> str:
    """Lowercase + strip Vietnamese diacritics + collapse whitespace.

    Handles đ/Đ explicitly because they don't decompose under NFD.
    """
    return _normalize_text(text)


def match_command(
    text: str,
    command_table: dict[str, list[str]],
) -> tuple[str, str] | None:
    """Match text against a command table using longest-prefix-first.

    Args:
        text: Original user input (diacritics preserved).
        command_table: {command_id: [prefix, ...]} where each prefix may be
            Vietnamese (with or without diacritics) or an English alias.
            A trailing space in a prefix means "must be followed by content"
            (e.g. "xem wiki " vs "xem wiki").

    Returns:
        (command_id, remainder) where remainder is sliced from the *original*
        text (preserving diacritics) after the matched prefix.
        Returns None if no prefix matches.
    """
    normalized_text = _normalize_text(text)

    best_command: str | None = None
    best_norm_len: int = 0
    best_orig_len: int = 0

    for command_id, prefixes in command_table.items():
        for prefix in prefixes:
            norm_prefix = _normalize_prefix(prefix)
            if normalized_text.startswith(norm_prefix):
                if len(norm_prefix) > best_norm_len:
                    best_norm_len = len(norm_prefix)
                    best_command = command_id
                    best_orig_len = _original_prefix_length(text, len(norm_prefix))

    if best_command is None:
        return None

    remainder = text[best_orig_len:].strip()
    return best_command, remainder


# ── Username validation ───────────────────────────────────────────────────────

RESERVED_USERNAMES: frozenset[str] = frozenset({
    "admin", "root", "bot", "system", "support",
    "owner", "null", "undefined", "me", "you",
})

_USERNAME_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")


def validate_username(name: str) -> str | None:
    """Validate a username. Returns an error message string, or None if valid.

    Rules:
    - Length 3–32 characters
    - Only [A-Za-z0-9_.-] allowed
    - Not a reserved name (case-insensitive)
    """
    if len(name) < 3:
        return "Username phải có ít nhất 3 ký tự."
    if len(name) > 32:
        return "Username không được vượt quá 32 ký tự."
    if not _USERNAME_RE.match(name):
        return "Username chỉ được chứa chữ cái, số, dấu gạch dưới (_), dấu chấm (.) và dấu gạch ngang (-)."
    if name.lower() in RESERVED_USERNAMES:
        return f"Username '{name}' là tên dành riêng, không thể sử dụng."
    return None


# ── Internal helpers ──────────────────────────────────────────────────────────

def _normalize_text(text: str) -> str:
    """Strip diacritics, lowercase, collapse + strip whitespace."""
    text = text.replace("đ", "d").replace("Đ", "D")
    text = unicodedata.normalize("NFD", text)
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    text = text.lower()
    return re.sub(r"\s+", " ", text).strip()


def _normalize_prefix(prefix: str) -> str:
    """Like _normalize_text but preserves a trailing space.

    A trailing space distinguishes "xem wiki " (page query) from "xem wiki"
    (exact/list command) after normalization.
    """
    has_trailing = prefix.endswith(" ")
    normalized = _normalize_text(prefix)
    return normalized + (" " if has_trailing else "")


def _normalize_char(ch: str) -> str:
    """Per-character diacritic strip + lowercase, without whitespace collapsing.

    Used only for mapping normalized prefix length back to original char count.
    """
    ch = ch.replace("đ", "d").replace("Đ", "D")
    ch = unicodedata.normalize("NFD", ch)
    ch = "".join(c for c in ch if unicodedata.category(c) != "Mn")
    return ch.lower()


def _original_prefix_length(original: str, norm_len: int) -> int:
    """Return how many original chars produce exactly norm_len normalized chars.

    Uses per-character normalization (no whitespace collapsing) so spaces are
    counted as 1 normalized char each.
    """
    count = 0
    for i, ch in enumerate(original):
        if count >= norm_len:
            return i
        count += len(_normalize_char(ch))
    return len(original)
