"""csrf.py — double-submit cookie CSRF protection.

Strategy: a random token is stored in a non-HttpOnly cookie (`csrf_token`).
Every unsafe HTML form submission must echo the token back in a hidden
`csrf_token` field; every htmx/fetch POST must echo it in the `X-CSRF-Token`
header. The middleware compares cookie vs submitted value — no server-side
session state required.

Safe methods (GET / HEAD / OPTIONS / TRACE) are never blocked.
Specific paths (e.g. /webhook for the Telegram webhook) are exempt.

Why this works despite SameSite=Lax on the session cookie:
  - SameSite=Lax already blocks cross-site POST form submissions for the session
    cookie. The CSRF token adds a second, defence-in-depth layer that also
    protects subdomain attacks and older browsers that ignore SameSite.

Cookie flags for csrf_token:
  - HttpOnly=False  → must be JS-readable so htmx/fetch can include it in headers
  - SameSite=Lax   → attacker site cannot read this cookie even if httponly=False
  - Secure=True     → only in staging/production (over HTTPS)
  - Path=/          → available for all routes

Implementation note: this is a pure ASGI middleware (not BaseHTTPMiddleware).
BaseHTTPMiddleware drains the ASGI receive channel when the middleware calls
request.form(), leaving nothing for the downstream route handler to read.
The pure ASGI approach lets us buffer the body explicitly and replay it via a
fresh receive coroutine, so route handlers always see the full form data.
"""
from __future__ import annotations

import hmac
import secrets
from typing import TYPE_CHECKING

from starlette.datastructures import MutableHeaders
from starlette.requests import Request

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send

_SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}
_COOKIE_NAME = "csrf_token"
_HEADER_NAME = "X-CSRF-Token"
_FORM_FIELD = "csrf_token"


def generate_csrf_token() -> str:
    """Return a 32-byte URL-safe random token."""
    return secrets.token_urlsafe(32)


def _tokens_equal(a: str, b: str) -> bool:
    """Constant-time comparison to prevent timing attacks."""
    return bool(a) and hmac.compare_digest(a, b)


class CSRFMiddleware:
    """Double-submit cookie CSRF middleware (pure ASGI).

    Args:
        exempt_paths: set of exact paths that bypass CSRF validation entirely
                      (e.g. {"/webhook"} for the Telegram bot endpoint).
        secure: when True, the csrf_token cookie is marked Secure (HTTPS-only).
    """

    def __init__(
        self,
        app: "ASGIApp",
        exempt_paths: set[str] | None = None,
        secure: bool = False,
    ) -> None:
        self.app = app
        self._exempt = exempt_paths or set()
        self._secure = secure

    async def __call__(self, scope: "Scope", receive: "Receive", send: "Send") -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Use Request only for headers/cookies — never reads the body here.
        request = Request(scope)
        method: str = scope["method"]
        path: str = scope["path"]
        cookie_token = request.cookies.get(_COOKIE_NAME, "")

        # Safe methods and exempt paths pass through; set cookie if absent.
        if method in _SAFE_METHODS or path in self._exempt:
            if not cookie_token:
                new_token = generate_csrf_token()
                await self.app(scope, receive, _cookie_send(send, new_token, self._secure))
            else:
                await self.app(scope, receive, send)
            return

        # --- Unsafe method: validate token ---
        if not cookie_token:
            await _reject(send, "CSRF token cookie missing.")
            return

        # Prefer X-CSRF-Token header (htmx/fetch) — no body read needed.
        submitted = request.headers.get(_HEADER_NAME)
        if submitted is not None:
            if not _tokens_equal(cookie_token, submitted):
                await _reject(send, "CSRF token invalid or missing.")
                return
            await self.app(scope, receive, send)
            return

        # Fall back to form field for regular HTML form submissions.
        content_type = request.headers.get("content-type", "")
        if "form" in content_type or "multipart" in content_type:
            body = await _buffer_body(receive)

            # Parse the buffered body using a replay Request so Starlette's
            # form parser handles both urlencoded and multipart transparently.
            parse_req = Request(scope, _make_replay_receive(body))
            form = await parse_req.form()
            submitted = str(form.get(_FORM_FIELD, ""))
            await form.close()

            if not _tokens_equal(cookie_token, submitted):
                await _reject(send, "CSRF token invalid or missing.")
                return

            # Replay the body for the downstream route handler.
            await self.app(scope, _make_replay_receive(body), send)
            return

        # No header and not a form submission — reject.
        await _reject(send, "CSRF token invalid or missing.")


# ── Helpers ────────────────────────────────────────────────────────────────────

async def _buffer_body(receive: "Receive") -> bytes:
    """Drain the ASGI receive channel and return the full body bytes."""
    chunks: list[bytes] = []
    while True:
        message = await receive()
        chunk = message.get("body", b"")
        if chunk:
            chunks.append(chunk)
        if not message.get("more_body", False):
            break
    return b"".join(chunks)


def _make_replay_receive(body: bytes) -> "Receive":
    """Return a receive coroutine that replays *body* once, then disconnects."""
    sent = False

    async def _receive() -> dict:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    return _receive


def _cookie_send(send: "Send", token: str, secure: bool) -> "Send":
    """Wrap *send* to append a Set-Cookie header on the response start message."""
    cookie_val = f"{_COOKIE_NAME}={token}; Path=/; SameSite=Lax"
    if secure:
        cookie_val += "; Secure"

    async def _send(message: dict) -> None:
        if message["type"] == "http.response.start":
            headers = MutableHeaders(scope=message)
            headers.append("Set-Cookie", cookie_val)
        await send(message)

    return _send


async def _reject(send: "Send", detail: str) -> None:
    """Send a 403 plain-text rejection response."""
    body = detail.encode()
    await send(
        {
            "type": "http.response.start",
            "status": 403,
            "headers": [
                (b"content-type", b"text/plain; charset=utf-8"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body, "more_body": False})
