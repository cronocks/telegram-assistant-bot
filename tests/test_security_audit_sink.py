"""Unit 6 (security hardening): security.audit_log routes to a SQLite sink.

Drive operation audit events previously only went to stdout via print(), so
they were invisible to `xem audit`. With an injected sink they are persisted to
the audit_log table; with no sink the original print() fallback is preserved.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import security


@pytest.fixture(autouse=True)
def _reset_sink():
    """Ensure module-level sink does not leak across tests."""
    security.set_audit_sink(None)
    yield
    security.set_audit_sink(None)


def test_routes_to_sink_when_set():
    sink = MagicMock()
    security.set_audit_sink(sink)

    security.audit_log(
        "file_created", file_id="abc123", filename="note.md",
        user="alice", details="created via bot",
    )

    sink.log.assert_called_once()
    kwargs = sink.log.call_args
    # action is positional or keyword — accept either form.
    args = kwargs.args
    kw = kwargs.kwargs
    action = kw.get("action") if "action" in kw else (args[1] if len(args) > 1 else None)
    assert action == "file_created"
    # payload should carry the contextual fields.
    payload = kw.get("payload")
    if payload is None and args:
        payload = args[-1]
    assert isinstance(payload, dict)
    assert payload.get("filename") == "note.md"
    assert payload.get("details") == "created via bot"


def test_falls_back_to_print_when_no_sink(capsys):
    # Sink is None (autouse fixture). audit_log must still print.
    security.audit_log("scope_validated", details="scopes=[...]")
    out = capsys.readouterr().out
    assert "[audit]" in out
    assert "scope_validated" in out


def test_print_fallback_on_sink_failure(capsys):
    sink = MagicMock()
    sink.log.side_effect = RuntimeError("db down")
    security.set_audit_sink(sink)

    # Must not raise; should degrade to print.
    security.audit_log("file_created", file_id="x")
    out = capsys.readouterr().out
    assert "[audit]" in out
