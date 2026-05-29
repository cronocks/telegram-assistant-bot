"""Unit 1 (security hardening): SRI on CDN <script> tags in base.html.

Guards against a compromised CDN serving malicious JS. Both htmx and alpine
must carry an `integrity` (sha384) attribute plus `crossorigin="anonymous"`.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_BASE_HTML = Path(__file__).resolve().parents[1] / "templates" / "base.html"


def _script_tag(src_substring: str) -> str:
    html = _BASE_HTML.read_text(encoding="utf-8")
    # Match the full <script ...></script> opening tag containing the src.
    pattern = re.compile(r"<script\b[^>]*" + re.escape(src_substring) + r"[^>]*>")
    m = pattern.search(html)
    assert m, f"no <script> tag referencing {src_substring!r} found in base.html"
    return m.group(0)


@pytest.mark.parametrize("src", ["htmx.org", "alpinejs"])
def test_cdn_script_has_integrity(src: str):
    tag = _script_tag(src)
    assert "integrity=" in tag, f"{src} script tag missing integrity attribute"
    assert re.search(r'integrity="sha(256|384|512)-', tag), (
        f"{src} integrity must be a sha256/384/512 hash"
    )


@pytest.mark.parametrize("src", ["htmx.org", "alpinejs"])
def test_cdn_script_has_crossorigin(src: str):
    tag = _script_tag(src)
    assert "crossorigin" in tag, f"{src} script tag missing crossorigin attribute"
