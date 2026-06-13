"""Tests for family_tree.build_tree_structure (FR-11 web visual tree)."""
from __future__ import annotations

import sqlite3

import pytest

from db.migrations import run_migrations


def _make_db() -> sqlite3.Connection:
    import db.connection as db_mod
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    original = db_mod._conn
    db_mod._conn = conn
    run_migrations()
    db_mod._conn = original
    return conn


def _insert_user(conn: sqlite3.Connection, uid: int = 1) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO users (id, name, role, created_at, deleted_at) "
        "VALUES (?, 'admin', 'admin', datetime('now'), NULL)",
        (uid,),
    )
    conn.commit()


def _add_member(conn, name: str, generation: int | None = None) -> int:
    from family_store import SqliteFamilyStore
    store = SqliteFamilyStore(conn=conn)
    kwargs = {}
    if generation is not None:
        kwargs["generation"] = generation
    row = store.create_member(created_by=1, full_name=name, **kwargs)
    return row["id"]


def _add_rel(conn, parent_id: int, child_id: int, rel_type: str = "cha") -> None:
    from family_store import SqliteFamilyStore
    store = SqliteFamilyStore(conn=conn)
    store.create_relationship(
        created_by=1, member_id=parent_id, related_id=child_id, rel_type=rel_type,
    )


# ── tests ─────────────────────────────────────────────────────────────────────

def test_empty_db_returns_empty_list():
    from family_tree import build_tree_structure
    conn = _make_db()
    _insert_user(conn)
    result = build_tree_structure(conn)
    assert result == []


def test_single_isolated_member_is_root():
    from family_tree import build_tree_structure
    conn = _make_db()
    _insert_user(conn)
    _add_member(conn, "Nguyễn Văn A", generation=1)
    result = build_tree_structure(conn)
    assert len(result) == 1
    assert result[0]["member"]["full_name"] == "Nguyễn Văn A"
    assert result[0]["children"] == []


def test_two_independent_roots():
    from family_tree import build_tree_structure
    conn = _make_db()
    _insert_user(conn)
    _add_member(conn, "Cụ Lập", generation=1)
    _add_member(conn, "Cụ Bắc", generation=1)
    result = build_tree_structure(conn)
    assert len(result) == 2
    names = {n["member"]["full_name"] for n in result}
    assert names == {"Cụ Lập", "Cụ Bắc"}


def test_parent_with_two_children():
    from family_tree import build_tree_structure
    conn = _make_db()
    _insert_user(conn)
    pid = _add_member(conn, "Cha", generation=1)
    c1 = _add_member(conn, "Con 1", generation=2)
    c2 = _add_member(conn, "Con 2", generation=2)
    _add_rel(conn, pid, c1)
    _add_rel(conn, pid, c2)
    result = build_tree_structure(conn)
    assert len(result) == 1
    root = result[0]
    assert root["member"]["full_name"] == "Cha"
    child_names = {c["member"]["full_name"] for c in root["children"]}
    assert child_names == {"Con 1", "Con 2"}


def test_three_generations():
    from family_tree import build_tree_structure
    conn = _make_db()
    _insert_user(conn)
    gp = _add_member(conn, "Ông nội", generation=1)
    p = _add_member(conn, "Bố", generation=2)
    ch = _add_member(conn, "Con", generation=3)
    _add_rel(conn, gp, p)
    _add_rel(conn, p, ch)
    result = build_tree_structure(conn)
    assert len(result) == 1
    assert result[0]["member"]["full_name"] == "Ông nội"
    assert len(result[0]["children"]) == 1
    assert result[0]["children"][0]["member"]["full_name"] == "Bố"
    assert result[0]["children"][0]["children"][0]["member"]["full_name"] == "Con"


def test_adopted_child_not_root():
    from family_tree import build_tree_structure
    conn = _make_db()
    _insert_user(conn)
    parent = _add_member(conn, "Bố nuôi", generation=2)
    child = _add_member(conn, "Con nuôi", generation=3)
    _add_rel(conn, parent, child, rel_type="con_nuoi")
    result = build_tree_structure(conn)
    assert len(result) == 1
    assert result[0]["member"]["full_name"] == "Bố nuôi"
    assert result[0]["children"][0]["member"]["full_name"] == "Con nuôi"


def test_spouse_vo_chong_not_treated_as_parent_child():
    from family_tree import build_tree_structure
    conn = _make_db()
    _insert_user(conn)
    h = _add_member(conn, "Chồng", generation=2)
    w = _add_member(conn, "Vợ", generation=2)
    _add_rel(conn, h, w, rel_type="vo")
    result = build_tree_structure(conn)
    # both are roots — vo/chong edges do NOT make one the child of the other
    assert len(result) == 2
    names = {n["member"]["full_name"] for n in result}
    assert names == {"Chồng", "Vợ"}
