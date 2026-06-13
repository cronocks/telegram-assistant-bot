"""Tests for family_tree.py — FR-11 Phase B."""
import sqlite3

import pytest

from db.migrations import run_migrations
import db.connection as db_mod
from family_store import SqliteFamilyStore
from family_tree import ancestors, descendants, family_roots, render_tree


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    orig = db_mod._conn
    db_mod._conn = c
    run_migrations()
    db_mod._conn = orig
    yield c
    c.close()


@pytest.fixture()
def store(conn):
    return SqliteFamilyStore(conn=conn)


@pytest.fixture()
def admin_id(conn):
    conn.execute(
        "INSERT INTO users (id, name, role, created_at) VALUES (1, 'Admin', 'admin', '2026-01-01')"
    )
    conn.commit()
    return 1


def _rel(store, admin_id, parent, child, rel_type="cha"):
    return store.create_relationship(
        created_by=admin_id, member_id=parent["id"], related_id=child["id"], rel_type=rel_type,
    )


def _member(store, admin_id, name, generation=None):
    return store.create_member(created_by=admin_id, full_name=name, generation=generation)


# ── ancestors ────────────────────────────────────────────────────────────────


def test_ancestors_empty_when_no_parents(conn, store, admin_id):
    a = _member(store, admin_id, "A")
    result = ancestors(conn, a["id"])
    assert result == []


def test_ancestors_single_parent(conn, store, admin_id):
    p = _member(store, admin_id, "Parent")
    c = _member(store, admin_id, "Child")
    _rel(store, admin_id, p, c, "cha")
    result = ancestors(conn, c["id"])
    assert len(result) == 1
    assert result[0]["id"] == p["id"]


def test_ancestors_two_levels(conn, store, admin_id):
    gp = _member(store, admin_id, "Grandparent")
    p = _member(store, admin_id, "Parent")
    c = _member(store, admin_id, "Child")
    _rel(store, admin_id, gp, p, "cha")
    _rel(store, admin_id, p, c, "cha")
    result = ancestors(conn, c["id"])
    ids = {r["id"] for r in result}
    assert p["id"] in ids
    assert gp["id"] in ids


# ── descendants ──────────────────────────────────────────────────────────────


def test_descendants_empty_when_no_children(conn, store, admin_id):
    a = _member(store, admin_id, "Leaf")
    result = descendants(conn, a["id"])
    assert result == []


def test_descendants_two_levels(conn, store, admin_id):
    gp = _member(store, admin_id, "GP")
    p = _member(store, admin_id, "P")
    c = _member(store, admin_id, "C")
    _rel(store, admin_id, gp, p, "cha")
    _rel(store, admin_id, p, c, "cha")
    result = descendants(conn, gp["id"])
    ids = {r["id"] for r in result}
    assert p["id"] in ids
    assert c["id"] in ids


# ── family_roots ──────────────────────────────────────────────────────────────


def test_roots_all_when_no_relationships(conn, store, admin_id):
    a = _member(store, admin_id, "A")
    b = _member(store, admin_id, "B")
    roots = family_roots(conn)
    root_ids = {r["id"] for r in roots}
    assert a["id"] in root_ids
    assert b["id"] in root_ids


def test_roots_excludes_children(conn, store, admin_id):
    p = _member(store, admin_id, "Parent")
    c = _member(store, admin_id, "Child")
    _rel(store, admin_id, p, c, "cha")
    roots = family_roots(conn)
    root_ids = {r["id"] for r in roots}
    assert p["id"] in root_ids
    assert c["id"] not in root_ids


def test_roots_excludes_deleted_members(conn, store, admin_id):
    a = _member(store, admin_id, "Active")
    d = _member(store, admin_id, "Deleted")
    store.soft_delete_member(d["id"])
    roots = family_roots(conn)
    root_ids = {r["id"] for r in roots}
    assert a["id"] in root_ids
    assert d["id"] not in root_ids


# ── render_tree ───────────────────────────────────────────────────────────────


def test_render_tree_empty(conn, store, admin_id):
    text = render_tree(conn)
    assert "Gia phả chưa có ai" in text or text == ""


def test_render_tree_single_root(conn, store, admin_id):
    _member(store, admin_id, "Nguyễn Văn A", generation=1)
    text = render_tree(conn)
    assert "Nguyễn Văn A" in text


def test_render_tree_parent_child_indent(conn, store, admin_id):
    p = _member(store, admin_id, "Cha", generation=1)
    c = _member(store, admin_id, "Con", generation=2)
    _rel(store, admin_id, p, c, "cha")
    text = render_tree(conn)
    lines = text.splitlines()
    parent_line = next(l for l in lines if "Cha" in l)
    child_line = next(l for l in lines if "Con" in l)
    # Child line must be indented more than parent line
    assert len(child_line) - len(child_line.lstrip()) > len(parent_line) - len(parent_line.lstrip())


def test_render_tree_from_specific_root(conn, store, admin_id):
    a = _member(store, admin_id, "A")
    b = _member(store, admin_id, "B")
    _member(store, admin_id, "C")  # unrelated root
    _rel(store, admin_id, a, b, "cha")
    text = render_tree(conn, root_id=a["id"])
    assert "A" in text
    assert "B" in text
    assert "C" not in text
