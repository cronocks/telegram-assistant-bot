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
"""
from __future__ import annotations

import hmac
import secrets

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

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


class CSRFMiddleware(BaseHTTPMiddleware):
    """Double-submit cookie CSRF middleware.

    Args:
        exempt_paths: set of exact paths that bypass CSRF validation entirely
                      (e.g. {"/webhook"} for the Telegram bot endpoint).
        secure: when True, the csrf_token cookie is marked Secure (HTTPS-only).
    """

    def __init__(
        self,
        app,
        exempt_paths: set[str] | None = None,
        secure: bool = False,
    ) -> None:
        super().__init__(app)
        self._exempt = exempt_paths or set()
        self._secure = secure

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path

        # Safe methods and exempt paths pass through; still set cookie if absent.
        if request.method in _SAFE_METHODS or path in self._exempt:
            response = await call_next(request)
            if _COOKIE_NAME not in request.cookies:
                _set_csrf_cookie(response, generate_csrf_token(), self._secure)
            return response

        # --- Unsafe method: validate token ---
        cookie_token = request.cookies.get(_COOKIE_NAME, "")
        if not cookie_token:
            return _reject("CSRF token cookie missing.")

        # Prefer header (htmx/fetch); fall back to form field.
        submitted = request.headers.get(_HEADER_NAME)
        if submitted is None:
            # Only read body for form submissions (avoid consuming JSON bodies).
            content_type = request.headers.get("content-type", "")
            if "form" in content_type or "multipart" in content_type:
                form = await request.form()
                submitted = form.get(_FORM_FIELD, "")
            else:
                submitted = ""

        if not _tokens_equal(cookie_token, submitted):
            return _reject("CSRF token invalid or missing.")

        return await call_next(request)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _set_csrf_cookie(response: Response, token: str, secure: bool) -> None:
    response.set_cookie(
        key=_COOKIE_NAME,
        value=token,
        httponly=False,    # must be JS-readable for htmx/fetch header injection
        samesite="lax",
        secure=secure,
        path="/",
    )


def _reject(detail: str) -> Response:
    return Response(content=detail, status_code=403, media_type="text/plain")
