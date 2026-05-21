"""Tests for the `doi role` admin command — _cmd_doi_role in core_handler.

Uses asyncio.run() rather than pytest-asyncio because the project has no
async-pytest plugin configured.
"""
import asyncio

from core_handler import CoreDeps, _cmd_doi_role


class FakeChannel:
    """Capture outbound messages instead of sending them."""
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send(self, chat_id: str, text: str, use_markdown: bool = True) -> None:
        self.sent.append((chat_id, text))

    @property
    def last_text(self) -> str:
        return self.sent[-1][1] if self.sent else ""


def _make_deps(store) -> CoreDeps:
    """Build a CoreDeps with only the fields _cmd_doi_role needs.

    Other dependencies are set to None — _cmd_doi_role does not touch them.
    """
    return CoreDeps(
        llm=None,  # type: ignore[arg-type]
        notes=None,  # type: ignore[arg-type]
        wiki=None,  # type: ignore[arg-type]
        channel=FakeChannel(),
        user_store=store,
        note_index=None,  # type: ignore[arg-type]
        memory_store=None,  # type: ignore[arg-type]
        elevation_store=None,  # type: ignore[arg-type]
    )


def _run(coro):
    """Helper: run an async coroutine to completion in a sync test."""
    return asyncio.run(coro)


def test_admin_changes_member_to_manager(store, sample_admin, member_user):
    deps = _make_deps(store)
    _run(_cmd_doi_role("chat1", f"{member_user.id} manager", sample_admin, deps))

    updated = store.get_user_by_id(member_user.id)
    assert updated.role == "manager"
    assert "member" in deps.channel.last_text
    assert "manager" in deps.channel.last_text


def test_admin_changes_by_name(store, sample_admin):
    # Use a single-token name because the parser splits on whitespace
    # (same limitation as `dat quota`).
    target = store.create_user(name="bob", role="member")
    deps = _make_deps(store)
    _run(_cmd_doi_role("chat1", "bob readonly", sample_admin, deps))

    updated = store.get_user_by_id(target.id)
    assert updated.role == "readonly"


def test_non_admin_is_rejected(store, sample_admin, member_user):
    deps = _make_deps(store)
    _run(_cmd_doi_role("chat1", f"{sample_admin.id} member", member_user, deps))

    # Role unchanged
    assert store.get_user_by_id(sample_admin.id).role == "admin"
    assert "Chỉ admin" in deps.channel.last_text


def test_invalid_role_rejected(store, sample_admin, member_user):
    deps = _make_deps(store)
    _run(_cmd_doi_role("chat1", f"{member_user.id} superuser", sample_admin, deps))

    assert store.get_user_by_id(member_user.id).role == "member"
    assert "không hợp lệ" in deps.channel.last_text.lower()


def test_missing_arguments_show_usage(store, sample_admin):
    deps = _make_deps(store)
    _run(_cmd_doi_role("chat1", "", sample_admin, deps))
    assert "Cú pháp" in deps.channel.last_text


def test_single_argument_shows_usage(store, sample_admin):
    deps = _make_deps(store)
    _run(_cmd_doi_role("chat1", "alice", sample_admin, deps))
    assert "Cú pháp" in deps.channel.last_text


def test_target_not_found(store, sample_admin):
    deps = _make_deps(store)
    _run(_cmd_doi_role("chat1", "99999 member", sample_admin, deps))
    # _resolve_user_or_reply sends an error; just assert at least one message went out.
    assert len(deps.channel.sent) >= 1


def test_admin_cannot_self_demote(store, sample_admin):
    deps = _make_deps(store)
    _run(_cmd_doi_role("chat1", f"{sample_admin.id} member", sample_admin, deps))

    # Role unchanged
    assert store.get_user_by_id(sample_admin.id).role == "admin"
    assert "tự hạ role" in deps.channel.last_text


def test_admin_keeping_self_as_admin_is_noop(store, sample_admin):
    deps = _make_deps(store)
    _run(_cmd_doi_role("chat1", f"{sample_admin.id} admin", sample_admin, deps))

    assert store.get_user_by_id(sample_admin.id).role == "admin"
    # Hits the "already at this role" branch.
    assert "đã ở role" in deps.channel.last_text


def test_no_change_when_role_same(store, sample_admin, member_user):
    deps = _make_deps(store)
    _run(_cmd_doi_role("chat1", f"{member_user.id} member", sample_admin, deps))

    assert store.get_user_by_id(member_user.id).role == "member"
    assert "đã ở role" in deps.channel.last_text
