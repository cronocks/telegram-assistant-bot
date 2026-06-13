"""web_context.py — Shared globals and helpers for all web sub-routers.

All store references are populated once at startup by init_web_router().
Sub-routers import this module (import web_context as ctx) and access
globals via ctx.<name> so they always read the current post-init value.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from fastapi import Response
from fastapi.templating import Jinja2Templates

import config
from backup_engine import BackupEngine, ParsedImport
from interfaces import AuditLog, ElevationStore, User, UserStore, WebConversationStore, WebSessionStore
from web_channel import WebChannelAdapter

# ── Store globals (populated by init_web_router at startup) ───────────────────
_templates: Jinja2Templates | None = None
_web_channel: WebChannelAdapter | None = None
_session_store: WebSessionStore | None = None
_user_store: UserStore | None = None
_audit: AuditLog | None = None
_elevation_store: ElevationStore | None = None
_conv_store: WebConversationStore | None = None
_backup_engine: BackupEngine | None = None
_task_store = None          # SqliteTaskStore | None
_anniversary_store = None   # SqliteAnniversaryStore | None
_anniversary_engine = None  # AnniversaryEngine | None
_ledger_store = None        # SqliteLedgerStore | None
_category_store = None      # SqliteCategoryStore | None
_budget_store = None        # SqliteBudgetStore | None
_ledger_reports = None      # LedgerReports | None
_family_store = None        # SqliteFamilyStore | None
_burial_store = None        # SqliteBurialStore | None

# ── Token stores ──────────────────────────────────────────────────────────────
_import_tokens: dict[str, dict] = {}
_IMPORT_TOKEN_TTL = timedelta(minutes=5)

_download_tokens: dict[str, dict] = {}
_DOWNLOAD_TOKEN_TTL = timedelta(seconds=60)

# ── Cookie config ─────────────────────────────────────────────────────────────
_COOKIE_NAME = "web_session"
_SESSION_MAX_AGE = config.WEB_SESSION_TTL_DAYS * 86_400  # seconds


# ── Init ──────────────────────────────────────────────────────────────────────

def init_web_router(
    templates: Jinja2Templates,
    web_channel: WebChannelAdapter,
    session_store: WebSessionStore,
    user_store: UserStore,
    audit: AuditLog,
    elevation_store: ElevationStore,
    conv_store: WebConversationStore,
    backup_engine: BackupEngine | None = None,
    task_store=None,
    anniversary_store=None,
    anniversary_engine=None,
    ledger_store=None,
    category_store=None,
    budget_store=None,
    ledger_reports=None,
    family_store=None,
    burial_store=None,
) -> None:
    """Wire dependencies into all web sub-routers (called once from main.py lifespan)."""
    global _templates, _web_channel, _session_store, _user_store
    global _audit, _elevation_store, _conv_store, _backup_engine, _task_store
    global _anniversary_store, _anniversary_engine
    global _ledger_store, _category_store, _budget_store, _ledger_reports
    global _family_store, _burial_store
    _templates = templates
    _web_channel = web_channel
    _session_store = session_store
    _user_store = user_store
    _audit = audit
    _elevation_store = elevation_store
    _conv_store = conv_store
    _backup_engine = backup_engine
    _task_store = task_store
    _anniversary_store = anniversary_store
    _anniversary_engine = anniversary_engine
    _ledger_store = ledger_store
    _category_store = category_store
    _budget_store = budget_store
    _ledger_reports = ledger_reports
    _family_store = family_store
    _burial_store = burial_store
    web_channel.set_conv_store(conv_store)
    if templates is not None:
        templates.env.filters["format_vnd"] = lambda v: f"{int(v):,}".replace(",", ".")


# ── Import token helpers ───────────────────────────────────────────────────────

def _cleanup_expired_tokens() -> None:
    now = datetime.now(timezone.utc)
    expired = [t for t, v in _import_tokens.items() if v["expires_at"] < now]
    for t in expired:
        del _import_tokens[t]


def _store_import_token(parsed: ParsedImport) -> str:
    _cleanup_expired_tokens()
    token = str(uuid.uuid4())
    _import_tokens[token] = {
        "parsed": parsed,
        "expires_at": datetime.now(timezone.utc) + _IMPORT_TOKEN_TTL,
    }
    return token


def _consume_import_token(token: str) -> ParsedImport | None:
    entry = _import_tokens.pop(token, None)
    if entry is None:
        return None
    if entry["expires_at"] < datetime.now(timezone.utc):
        return None
    return entry["parsed"]


# ── Download token helpers ────────────────────────────────────────────────────

def _store_download_token(zip_bytes: bytes, filename: str) -> str:
    now = datetime.now(timezone.utc)
    expired = [t for t, v in _download_tokens.items() if v["expires_at"] < now]
    for t in expired:
        del _download_tokens[t]
    token = str(uuid.uuid4())
    _download_tokens[token] = {
        "zip_bytes": zip_bytes,
        "filename": filename,
        "expires_at": now + _DOWNLOAD_TOKEN_TTL,
    }
    return token


def _consume_download_token(token: str) -> dict | None:
    """Return download entry by token; None if expired/missing. Token is NOT removed on read."""
    entry = _download_tokens.get(token)
    if entry is None:
        return None
    if entry["expires_at"] < datetime.now(timezone.utc):
        _download_tokens.pop(token, None)
        return None
    return entry


# ── Cookie helpers ─────────────────────────────────────────────────────────────

def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=_COOKIE_NAME,
        value=token,
        max_age=_SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=(config.APP_ENV != "local"),
    )


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(key=_COOKIE_NAME, httponly=True, samesite="lax")


# ── Auth helpers ───────────────────────────────────────────────────────────────

def _resolve_user(token: str | None) -> User | None:
    if not token or _session_store is None or _user_store is None:
        return None
    user_id = _session_store.find_active(token)
    if user_id is None:
        return None
    user = _user_store.get_user_by_id(user_id)
    if user is None or not user.is_active:
        return None
    return user


def _get_conv_or_403(conv_id: int, user: User) -> dict | None:
    assert _conv_store is not None
    conv = _conv_store.get(conv_id)
    if conv is None or conv["user_id"] != user.id:
        return None
    return conv
