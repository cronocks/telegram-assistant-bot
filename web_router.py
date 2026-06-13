"""web_router.py — Thin aggregator: imports sub-routers and wires them together.

All business logic lives in the domain sub-routers:
  web_auth.py          — /, /login, /logout, /setup-password, /settings/password
  web_chat.py          — /chat, /chat/stream, /chat/{conv_id}, /chat/send,
                          /chat/{conv_id}/send, /api/conversations*
  web_tasks.py         — /tasks, /api/tasks*
  web_anniversaries.py — /anniversaries*
  web_ledger.py        — /ledger*
  web_admin.py         — /admin/*, /settings/export*
  web_family.py        — /family*

Shared globals and init_web_router() live in web_context.py.
main.py imports init_web_router and router from this module (unchanged API).
"""
from __future__ import annotations

from fastapi import APIRouter

# Re-export init_web_router so main.py callers need only import from web_router.
from web_context import init_web_router  # noqa: F401

# Re-export helpers that tests and other modules may import from web_router directly.
from web_context import (  # noqa: F401
    _consume_import_token,
    _consume_download_token,
    _store_import_token,
    _store_download_token,
    _import_tokens,
    _download_tokens,
    _resolve_user,
    _get_conv_or_403,
    _set_session_cookie,
    _clear_session_cookie,
    _COOKIE_NAME,
    _SESSION_MAX_AGE,
)
from web_admin import _export_filename, _zip_response  # noqa: F401

import web_context as _ctx


def __getattr__(name: str):
    """Forward attribute lookups to web_context for backward-compat with test code
    that accesses store globals via `import web_router as wr; wr._user_store`."""
    try:
        return getattr(_ctx, name)
    except AttributeError:
        raise AttributeError(f"module 'web_router' has no attribute {name!r}")


import web_auth
import web_anniversaries
import web_admin
import web_chat
import web_family
import web_ledger
import web_tasks

router = APIRouter()

# Auth and root routes first (includes /, /login, /logout, /setup-password, /settings/password)
router.include_router(web_auth.router)

# Chat UI and conversation REST API
router.include_router(web_chat.router)

# Tasks
router.include_router(web_tasks.router)

# Anniversaries
router.include_router(web_anniversaries.router)

# Ledger (expense/income tracking)
router.include_router(web_ledger.router)

# Admin + export/import
router.include_router(web_admin.router)

# Family tree
router.include_router(web_family.router)
