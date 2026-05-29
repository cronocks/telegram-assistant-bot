"""rate_limit.py — in-house sliding-window rate-limit middleware.

Keyed by (client_ip, path_key). Unsafe methods (POST/PUT/PATCH/DELETE) are
counted; GET/HEAD/OPTIONS are never blocked.

Specific sensitive paths (e.g. /login) get a configurable tight cap.
All other unsafe requests share the `default_limit` bucket per client IP.

No external dependencies — uses collections.deque for O(1) eviction.
asyncio-safe without locking: deque mutation happens only outside `await`
points, so cooperative multitasking cannot interleave bucket checks.
"""
from __future__ import annotations

import time
from collections import defaultdict, deque

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Sliding-window rate limiter for sensitive endpoints.

    Args:
        path_limits: mapping of exact path → (max_requests, window_seconds).
                     E.g. {"/login": (10, 60)}.
        default_limit: fallback (max_requests, window_seconds) for all other
                       unsafe requests not matched by path_limits.
    """

    def __init__(
        self,
        app,
        path_limits: dict[str, tuple[int, int]],
        default_limit: tuple[int, int] = (120, 60),
    ) -> None:
        super().__init__(app)
        self._path_limits = path_limits      # {"/login": (10, 60), ...}
        self._default_limit = default_limit  # (120, 60) = 120 req / 60s default
        # Buckets: (client_ip, path_key) → deque of epoch timestamps.
        self._buckets: dict[tuple, deque] = defaultdict(deque)

    def _client_ip(self, request: Request) -> str:
        """Best-effort client IP extraction."""
        # Respect X-Forwarded-For when running behind a reverse proxy (Render).
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            return forwarded.split(",")[0].strip()
        if request.client:
            return request.client.host
        return "unknown"

    def _match(self, path: str) -> tuple[str, int, int]:
        """Return (path_key, max_requests, window_seconds) for this path."""
        if path in self._path_limits:
            max_req, window = self._path_limits[path]
            return path, max_req, window
        max_req, window = self._default_limit
        return "default", max_req, window

    async def dispatch(self, request: Request, call_next):
        if request.method not in _UNSAFE_METHODS:
            return await call_next(request)

        ip = self._client_ip(request)
        path_key, max_req, window = self._match(request.url.path)
        bucket_key = (ip, path_key)

        now = time.monotonic()
        cutoff = now - window
        bucket = self._buckets[bucket_key]

        # Evict timestamps outside the window.
        while bucket and bucket[0] < cutoff:
            bucket.popleft()

        if len(bucket) >= max_req:
            return Response(
                content="Rate limit exceeded. Please slow down.",
                status_code=429,
                headers={"Retry-After": str(window)},
            )

        bucket.append(now)
        return await call_next(request)
