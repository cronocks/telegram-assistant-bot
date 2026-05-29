"""security_headers.py — defense-in-depth HTTP response headers.

A Starlette middleware that stamps standard security headers on every response:
  - X-Frame-Options: DENY            → clickjacking protection
  - X-Content-Type-Options: nosniff  → MIME-sniffing protection
  - Referrer-Policy                  → limit referrer leakage
  - Content-Security-Policy          → restrict script/style/connect origins
  - Strict-Transport-Security        → HTTPS pinning (staging/production only)

CSP note: the policy intentionally allows 'unsafe-inline' and 'unsafe-eval'.
base.html ships an inline theme <script> and inline <style>, and Alpine.js 3
evaluates directives via new Function() (needs 'unsafe-eval'). A strict nonce-
based CSP would require an Alpine CSP build and is deliberately out of scope.
"""
from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware

# unpkg.com is whitelisted for htmx + Alpine (pinned via SRI in base.html).
_CSP = (
    "default-src 'self'; "
    "script-src 'self' https://unpkg.com 'unsafe-inline' 'unsafe-eval'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "font-src 'self'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)

_HSTS = "max-age=31536000; includeSubDomains"


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Stamp security headers on every outgoing response.

    Args:
        hsts: when True, also emit Strict-Transport-Security. Should be enabled
              only when served over HTTPS (staging/production), never local HTTP.
    """

    def __init__(self, app, hsts: bool = False) -> None:
        super().__init__(app)
        self.hsts = hsts

    async def dispatch(self, request, call_next):
        response = await call_next(request)
        headers = response.headers
        headers.setdefault("X-Frame-Options", "DENY")
        headers.setdefault("X-Content-Type-Options", "nosniff")
        headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        headers.setdefault("Content-Security-Policy", _CSP)
        if self.hsts:
            headers.setdefault("Strict-Transport-Security", _HSTS)
        return response
