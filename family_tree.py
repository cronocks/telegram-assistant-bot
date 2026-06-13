"""family_tree.py — Recursive CTE queries and text-tree rendering for FR-11.

All queries work directly on the SQLite connection so they can be used both
from command handlers (via CoreDeps) and from standalone scripts/tests without
pulling in the full store layer.
"""
from __future__ import annotations

import sqlite3


# ── CTE helpers ───────────────────────────────────────────────────────────────

_ANCESTOR_CTE = """
WITH RECURSIVE anc(id) AS (
    SELECT member_id
    FROM family_relationships
    WHERE related_id = ? AND deleted_at IS NULL
    UNION ALL
    SELECT fr.member_id
    FROM family_relationships fr
    JOIN anc ON fr.related_id = anc.id
    WHERE fr.deleted_at IS NULL
)
SELECT fm.* FROM family_members fm
JOIN anc ON fm.id = anc.id
WHERE fm.deleted_at IS NULL
"""

_DESCENDANT_CTE = """
WITH RECURSIVE desc_cte(id) AS (
    SELECT related_id
    FROM family_relationships
    WHERE member_id = ? AND deleted_at IS NULL
    UNION ALL
    SELECT fr.related_id
    FROM family_relationships fr
    JOIN desc_cte ON fr.member_id = desc_cte.id
    WHERE fr.deleted_at IS NULL
)
SELECT fm.* FROM family_members fm
JOIN desc_cte ON fm.id = desc_cte.id
WHERE fm.deleted_at IS NULL
"""


def ancestors(conn: sqlite3.Connection, member_id: int) -> list[dict]:
    """All ancestors (parents, grandparents, …) of member_id, in any order."""
    rows = conn.execute(_ANCESTOR_CTE, (member_id,)).fetchall()
    return [dict(r) for r in rows]


def descendants(conn: sqlite3.Connection, member_id: int) -> list[dict]:
    """All descendants (children, grandchildren, …) of member_id, in any order."""
    rows = conn.execute(_DESCENDANT_CTE, (member_id,)).fetchall()
    return [dict(r) for r in rows]


def family_roots(conn: sqlite3.Connection) -> list[dict]:
    """Members with no active incoming parent edge (cha/me/con_nuoi) — tree roots."""
    rows = conn.execute(
        """
        SELECT fm.*
        FROM family_members fm
        WHERE fm.deleted_at IS NULL
          AND fm.id NOT IN (
              SELECT related_id FROM family_relationships
              WHERE deleted_at IS NULL
                AND rel_type IN ('cha', 'me', 'con_nuoi')
          )
        ORDER BY fm.generation ASC NULLS LAST, fm.full_name ASC
        """
    ).fetchall()
    return [dict(r) for r in rows]


# ── Text-tree rendering ───────────────────────────────────────────────────────

def _format_node(row: dict) -> str:
    gen = f" [đời {row['generation']}]" if row.get("generation") else ""
    birth_year = row.get("birth_year")
    death_year = row.get("death_year")
    years = ""
    if birth_year or death_year:
        years = f" ({birth_year or '?'} – {death_year or 'nay'})"
    return f"{row['full_name']}{gen}{years}"


def _render_subtree(
    conn: sqlite3.Connection,
    member_id: int,
    visited: set[int],
    indent: int,
) -> list[str]:
    """Recursively render a subtree rooted at member_id."""
    if member_id in visited:
        return []  # safety guard against unexpected cycles in data
    visited.add(member_id)

    row = conn.execute(
        "SELECT * FROM family_members WHERE id = ? AND deleted_at IS NULL",
        (member_id,),
    ).fetchone()
    if row is None:
        return []

    prefix = "  " * indent + ("└─ " if indent > 0 else "")
    lines = [prefix + _format_node(dict(row))]

    children = conn.execute(
        """
        SELECT fm.* FROM family_members fm
        JOIN family_relationships fr ON fr.related_id = fm.id
        WHERE fr.member_id = ? AND fr.deleted_at IS NULL AND fm.deleted_at IS NULL
        ORDER BY fm.generation ASC NULLS LAST, fm.full_name ASC
        """,
        (member_id,),
    ).fetchall()

    for child in children:
        lines.extend(_render_subtree(conn, child["id"], visited, indent + 1))

    return lines


_PARENT_REL_TYPES = ("cha", "me", "con_nuoi")


def build_tree_structure(conn: sqlite3.Connection) -> list[dict]:
    """Return a nested structure for the web visual tree.

    Each node is: {"member": dict, "children": [node, ...]}.
    Only cha/me/con_nuoi edges define the parent→child hierarchy;
    vo/chong edges are ignored so spouses remain separate root nodes.
    """
    members = {
        row["id"]: dict(row)
        for row in conn.execute(
            "SELECT * FROM family_members WHERE deleted_at IS NULL ORDER BY generation ASC NULLS LAST, full_name ASC"
        ).fetchall()
    }
    if not members:
        return []

    # Build parent_id → [child_id] mapping from parent-type edges only.
    children_of: dict[int, list[int]] = {mid: [] for mid in members}
    child_ids: set[int] = set()
    for row in conn.execute(
        "SELECT member_id, related_id FROM family_relationships "
        "WHERE rel_type IN ('cha', 'me', 'con_nuoi') AND deleted_at IS NULL"
    ).fetchall():
        parent_id, child_id = row["member_id"], row["related_id"]
        if parent_id in children_of and child_id in members:
            if child_id not in children_of[parent_id]:
                children_of[parent_id].append(child_id)
            child_ids.add(child_id)

    def _build_node(mid: int, visited: set[int]) -> dict:
        visited.add(mid)
        child_nodes = []
        for cid in sorted(children_of.get(mid, []), key=lambda x: (members[x].get("generation") or 999, members[x]["full_name"])):
            if cid not in visited:
                child_nodes.append(_build_node(cid, visited))
        return {"member": members[mid], "children": child_nodes}

    visited: set[int] = set()
    roots = [
        mid for mid in members
        if mid not in child_ids
    ]
    roots.sort(key=lambda mid: (members[mid].get("generation") or 999, members[mid]["full_name"]))
    return [_build_node(mid, visited) for mid in roots]


def render_tree(conn: sqlite3.Connection, root_id: int | None = None) -> str:
    """Render the family tree as indented text.

    If root_id is given, render the subtree rooted at that member.
    Otherwise render all roots and their subtrees.
    """
    if root_id is not None:
        roots = [conn.execute(
            "SELECT * FROM family_members WHERE id = ? AND deleted_at IS NULL",
            (root_id,),
        ).fetchone()]
        roots = [dict(r) for r in roots if r is not None]
    else:
        roots = family_roots(conn)

    if not roots:
        return "Gia phả chưa có ai."

    visited: set[int] = set()
    lines: list[str] = []
    for root in roots:
        lines.extend(_render_subtree(conn, root["id"], visited, indent=0))

    return "\n".join(lines)
