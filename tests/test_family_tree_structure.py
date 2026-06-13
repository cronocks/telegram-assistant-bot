"""Tests for family_tree.build_tree_structure (FR-11 web visual tree).

Return format:
    {
        "has_data": bool,
        "rows": [
            {
                "gen": int | None,
                "gen_label": str,
                "units": [
                    {
                        "id": str,
                        "primary": dict,
                        "spouse": dict | None,
                        "parent_unit_id": str | None,
                        "child_unit_ids": list[str],
                    }
                ],
            }
        ],
    }
"""
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


def _add_rel(conn, member_id: int, related_id: int, rel_type: str = "cha") -> None:
    from family_store import SqliteFamilyStore
    store = SqliteFamilyStore(conn=conn)
    store.create_relationship(
        created_by=1, member_id=member_id, related_id=related_id, rel_type=rel_type,
    )


# ── helpers ───────────────────────────────────────────────────────────────────

def _unit_names(result: dict, gen: int) -> set[str]:
    """Names of primary members in the row for given generation."""
    for row in result["rows"]:
        if row["gen"] == gen:
            return {u["primary"]["full_name"] for u in row["units"]}
    return set()


def _all_units(result: dict) -> list[dict]:
    return [u for row in result["rows"] for u in row["units"]]


# ── tests ─────────────────────────────────────────────────────────────────────

def test_empty_db_returns_empty_structure():
    from family_tree import build_tree_structure
    conn = _make_db()
    _insert_user(conn)
    result = build_tree_structure(conn)
    assert result == {"has_data": False, "rows": []}


def test_single_isolated_member():
    from family_tree import build_tree_structure
    conn = _make_db()
    _insert_user(conn)
    _add_member(conn, "Nguyễn Văn A", generation=1)
    result = build_tree_structure(conn)
    assert result["has_data"] is True
    assert len(result["rows"]) == 1
    units = result["rows"][0]["units"]
    assert len(units) == 1
    assert units[0]["primary"]["full_name"] == "Nguyễn Văn A"
    assert units[0]["spouse"] is None
    assert units[0]["child_unit_ids"] == []
    assert units[0]["parent_unit_id"] is None


def test_two_independent_members_different_generations():
    """Two unrelated members in different generations → two rows."""
    from family_tree import build_tree_structure
    conn = _make_db()
    _insert_user(conn)
    _add_member(conn, "Cụ Lập", generation=1)
    _add_member(conn, "Ông Hùng", generation=2)
    result = build_tree_structure(conn)
    assert len(result["rows"]) == 2
    assert _unit_names(result, 1) == {"Cụ Lập"}
    assert _unit_names(result, 2) == {"Ông Hùng"}


def test_same_generation_grouped_in_one_row():
    """Unrelated members with the same generation share the same row."""
    from family_tree import build_tree_structure
    conn = _make_db()
    _insert_user(conn)
    _add_member(conn, "Cụ Lập", generation=1)
    _add_member(conn, "Cụ Bắc", generation=1)
    result = build_tree_structure(conn)
    assert len(result["rows"]) == 1
    assert _unit_names(result, 1) == {"Cụ Lập", "Cụ Bắc"}


def test_parent_with_two_children_different_generations():
    from family_tree import build_tree_structure
    conn = _make_db()
    _insert_user(conn)
    pid = _add_member(conn, "Cha", generation=1)
    c1 = _add_member(conn, "Con 1", generation=2)
    c2 = _add_member(conn, "Con 2", generation=2)
    _add_rel(conn, pid, c1)
    _add_rel(conn, pid, c2)
    result = build_tree_structure(conn)
    assert len(result["rows"]) == 2
    assert _unit_names(result, 1) == {"Cha"}
    assert _unit_names(result, 2) == {"Con 1", "Con 2"}
    # parent unit links to both child units
    parent_unit = result["rows"][0]["units"][0]
    assert len(parent_unit["child_unit_ids"]) == 2


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
    assert len(result["rows"]) == 3
    gen_labels = [r["gen_label"] for r in result["rows"]]
    assert gen_labels == ["Đời 1", "Đời 2", "Đời 3"]
    # edges: grandparent unit → parent unit → child unit
    gp_unit = result["rows"][0]["units"][0]
    p_unit = result["rows"][1]["units"][0]
    ch_unit = result["rows"][2]["units"][0]
    assert p_unit["id"] in gp_unit["child_unit_ids"]
    assert ch_unit["id"] in p_unit["child_unit_ids"]
    assert p_unit["parent_unit_id"] == gp_unit["id"]
    assert ch_unit["parent_unit_id"] == p_unit["id"]


def test_adopted_child_appears_in_correct_generation():
    from family_tree import build_tree_structure
    conn = _make_db()
    _insert_user(conn)
    parent = _add_member(conn, "Bố nuôi", generation=2)
    child = _add_member(conn, "Con nuôi", generation=3)
    _add_rel(conn, parent, child, rel_type="con_nuoi")
    result = build_tree_structure(conn)
    assert len(result["rows"]) == 2
    assert _unit_names(result, 2) == {"Bố nuôi"}
    assert _unit_names(result, 3) == {"Con nuôi"}
    parent_unit = result["rows"][0]["units"][0]
    child_unit = result["rows"][1]["units"][0]
    assert child_unit["id"] in parent_unit["child_unit_ids"]


def test_spouse_paired_into_one_unit():
    """Spouses with vo/chong relationship → single couple unit, not two units."""
    from family_tree import build_tree_structure
    conn = _make_db()
    _insert_user(conn)
    h = _add_member(conn, "Chồng", generation=2)
    w = _add_member(conn, "Vợ", generation=2)
    _add_rel(conn, h, w, rel_type="vo")
    result = build_tree_structure(conn)
    assert len(result["rows"]) == 1
    assert len(result["rows"][0]["units"]) == 1
    unit = result["rows"][0]["units"][0]
    names = {unit["primary"]["full_name"], unit["spouse"]["full_name"]}
    assert names == {"Chồng", "Vợ"}
    # vo/chong does NOT create parent-child edge
    assert unit["child_unit_ids"] == []
    assert unit["parent_unit_id"] is None


def test_couple_unit_combined_children():
    """Both parents' children are merged under the couple unit."""
    from family_tree import build_tree_structure
    conn = _make_db()
    _insert_user(conn)
    father = _add_member(conn, "Cha", generation=1)
    mother = _add_member(conn, "Mẹ", generation=1)
    c1 = _add_member(conn, "Con 1", generation=2)
    c2 = _add_member(conn, "Con 2", generation=2)
    _add_rel(conn, father, mother, rel_type="vo")  # pair as couple
    _add_rel(conn, father, c1)          # father → Con 1
    _add_rel(conn, mother, c2, "me")    # mother → Con 2
    result = build_tree_structure(conn)
    assert len(result["rows"]) == 2
    couple_unit = result["rows"][0]["units"][0]
    # Both children belong to the couple unit
    assert len(couple_unit["child_unit_ids"]) == 2
    child_names = _unit_names(result, 2)
    assert child_names == {"Con 1", "Con 2"}


def test_edges_connect_parent_to_child_unit():
    """Edge data: parent_unit_id on child matches id on parent."""
    from family_tree import build_tree_structure
    conn = _make_db()
    _insert_user(conn)
    p = _add_member(conn, "Cha", generation=1)
    c = _add_member(conn, "Con", generation=2)
    _add_rel(conn, p, c)
    result = build_tree_structure(conn)
    parent_unit = result["rows"][0]["units"][0]
    child_unit = result["rows"][1]["units"][0]
    assert child_unit["parent_unit_id"] == parent_unit["id"]
    assert child_unit["id"] in parent_unit["child_unit_ids"]
