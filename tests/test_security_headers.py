"""Unit 2 (security hardening): SecurityHeadersMiddleware.

Adds standard defense-in-depth headers to every response. HSTS is emitted only
when running outside local (i.e. behind HTTPS in staging/production).
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from security_headers import SecurityHeadersMiddleware


def _client(hsts: bool) -> TestClient:
    app = FastAPI()
    app.add_middleware(SecurityHeadersMiddleware, hsts=hsts)

    @app.get("/ping")
    async def ping():
        return {"ok": True}

    return TestClient(app)


def test_static_headers_present():
    r = _client(hsts=False).get("/ping")
    assert r.headers["X-Frame-Options"] == "DENY"
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"


def test_csp_allows_unpkg_and_self():
    r = _client(hsts=False).get("/ping")
    csp = r.headers["Content-Security-Policy"]
    assert "default-src 'self'" in csp
    assert "https://unpkg.com" in csp
    # Alpine 3 requires eval; inline script/style blocks exist in base.html.
    assert "'unsafe-eval'" in csp
    assert "'unsafe-inline'" in csp


def test_hsts_only_when_enabled():
    assert "Strict-Transport-Security" not in _client(hsts=False).get("/ping").headers
    r = _client(hsts=True).get("/ping")
    assert "max-age=" in r.headers["Strict-Transport-Security"]
