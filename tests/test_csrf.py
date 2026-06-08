"""Unit 4 (security hardening): CSRFMiddleware — double-submit cookie.

The middleware:
  - Sets a non-HttpOnly csrf_token cookie on safe (GET/HEAD) responses when absent.
  - On unsafe (POST/PUT/PATCH/DELETE) requests validates that:
      • cookie token is present, AND
      • it matches either the X-CSRF-Token request header (htmx/fetch)
        OR the csrf_token form field (regular HTML form POST).
  - Specific paths (e.g. /webhook) are exempt from validation.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI, Form, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from csrf import CSRFMiddleware

_TOKEN = "test-csrf-token-abc123"


def _build_app(exempt_paths: set | None = None) -> FastAPI:
    app = FastAPI()
    app.add_middleware(CSRFMiddleware, exempt_paths=exempt_paths or set())

    @app.get("/page")
    async def page():
        return JSONResponse({"ok": True})

    @app.post("/protected")
    async def protected(csrf_token: str = Form(default="")):
        return JSONResponse({"ok": True})

    @app.post("/login")
    async def login(username: str = Form(...), password: str = Form(...)):
        return JSONResponse({"username": username, "password": password})

    @app.post("/webhook")
    async def webhook():
        return JSONResponse({"ok": True})

    return app


# ── GET sets cookie ────────────────────────────────────────────────────────────

def test_get_sets_csrf_cookie_when_absent():
    client = TestClient(_build_app())
    r = client.get("/page")
    assert r.status_code == 200
    assert "csrf_token" in r.cookies


def test_get_does_not_overwrite_existing_cookie():
    client = TestClient(_build_app())
    client.cookies.set("csrf_token", _TOKEN)
    r = client.get("/page")
    # Cookie value should remain the same (server does not overwrite).
    assert r.cookies.get("csrf_token", _TOKEN) == _TOKEN


# ── POST validation ───────────────────────────────────────────────────────────

def test_post_without_any_token_returns_403():
    client = TestClient(_build_app(), raise_server_exceptions=False)
    r = client.post("/protected", data={"other": "value"})
    assert r.status_code == 403


def test_post_with_matching_form_field_passes():
    client = TestClient(_build_app(), raise_server_exceptions=False)
    client.cookies.set("csrf_token", _TOKEN)
    r = client.post("/protected", data={"csrf_token": _TOKEN})
    assert r.status_code == 200


def test_post_with_mismatched_form_field_returns_403():
    client = TestClient(_build_app(), raise_server_exceptions=False)
    client.cookies.set("csrf_token", _TOKEN)
    r = client.post("/protected", data={"csrf_token": "wrong-token"})
    assert r.status_code == 403


def test_post_with_matching_header_passes():
    """htmx/fetch flow: X-CSRF-Token header instead of form field."""
    client = TestClient(_build_app(), raise_server_exceptions=False)
    client.cookies.set("csrf_token", _TOKEN)
    r = client.post("/protected", headers={"X-CSRF-Token": _TOKEN}, data={})
    assert r.status_code == 200


def test_post_with_mismatched_header_returns_403():
    client = TestClient(_build_app(), raise_server_exceptions=False)
    client.cookies.set("csrf_token", _TOKEN)
    r = client.post("/protected", headers={"X-CSRF-Token": "bad"}, data={})
    assert r.status_code == 403


# ── Body available after CSRF pass ───────────────────────────────────────────

def test_form_body_available_to_handler_after_csrf_pass():
    """Route handler must still read username/password after CSRF middleware
    has consumed the form body to validate the csrf_token field.
    Regression test for Starlette BaseHTTPMiddleware body-consumption bug."""
    client = TestClient(_build_app(), raise_server_exceptions=False)
    client.cookies.set("csrf_token", _TOKEN)
    r = client.post(
        "/login",
        data={"username": "alice", "password": "secret", "csrf_token": _TOKEN},
    )
    assert r.status_code == 200
    assert r.json() == {"username": "alice", "password": "secret"}


# ── Exempt paths ──────────────────────────────────────────────────────────────

def test_exempt_path_bypasses_validation():
    """Telegram /webhook must never require a CSRF token."""
    app = _build_app(exempt_paths={"/webhook"})
    client = TestClient(app, raise_server_exceptions=False)
    # No cookie, no token — should still succeed.
    r = client.post("/webhook", data={})
    assert r.status_code == 200
