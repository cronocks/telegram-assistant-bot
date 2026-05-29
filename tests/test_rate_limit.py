"""Unit 5 (security hardening): RateLimitMiddleware.

An in-house sliding-window rate limiter keyed by (client_ip, path). Specific
sensitive paths (e.g. /login) get a tight cap; all other unsafe methods share a
more generous default. GET/HEAD are never limited.
"""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from rate_limit import RateLimitMiddleware


def _build_app(path_limits: dict, default_limit: tuple = (100, 60)) -> TestClient:
    """Build a minimal app with the middleware and two test routes."""
    app = FastAPI()
    app.add_middleware(
        RateLimitMiddleware,
        path_limits=path_limits,
        default_limit=default_limit,
    )

    @app.post("/login")
    async def fake_login():
        return {"ok": True}

    @app.get("/login")
    async def fake_login_get():
        return {"ok": True}

    @app.post("/other")
    async def other():
        return {"ok": True}

    return TestClient(app, raise_server_exceptions=False)


def test_post_limited_after_threshold():
    client = _build_app(path_limits={"/login": (3, 60)})
    for _ in range(3):
        assert client.post("/login").status_code == 200
    assert client.post("/login").status_code == 429


def test_get_not_rate_limited():
    """GET requests to a POST-limited path must never be blocked."""
    client = _build_app(path_limits={"/login": (2, 60)})
    for _ in range(10):
        assert client.get("/login").status_code == 200


def test_separate_path_has_independent_bucket():
    """Exceeding /login limit does not affect /other."""
    client = _build_app(path_limits={"/login": (1, 60)}, default_limit=(100, 60))
    client.post("/login")  # exhaust /login
    client.post("/login")  # should be 429
    # /other has its own bucket — must still be 200.
    assert client.post("/other").status_code == 200


def test_429_response_has_retry_after_header():
    client = _build_app(path_limits={"/login": (1, 60)})
    client.post("/login")
    r = client.post("/login")
    assert r.status_code == 429
    assert "Retry-After" in r.headers
