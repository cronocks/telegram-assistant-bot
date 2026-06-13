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
          AND fr.rel_type IN ('cha', 'me', 'con_nuoi')
        ORDER BY fm.generation ASC NULLS LAST, fm.full_name ASC
        """,
        (member_id,),
    ).fetchall()

    for child in children:
        lines.extend(_render_subtree(conn, child["id"], visited, indent + 1))

    return lines


_PARENT_REL_TYPES = ("cha", "me", "con_nuoi")


def build_tree_structure(conn: sqlite3.Connection) -> dict:
    """Return a generation-grouped structure for the web visual tree.

    Return format::

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

    Only cha/me/con_nuoi edges define the parent→child hierarchy.
    vo/chong edges pair spouses into a single couple unit; their children
    are merged under that unit.
    Members without a generation value are placed in a final "unclassified" row.
    """
    from collections import defaultdict

    members: dict[int, dict] = {
        row["id"]: dict(row)
        for row in conn.execute(
            "SELECT * FROM family_members WHERE deleted_at IS NULL"
        ).fetchall()
    }
    if not members:
        return {"has_data": False, "rows": []}

    # --- parent-child edges (cha / me / con_nuoi) ----------------------------
    children_of: dict[int, set[int]] = defaultdict(set)
    for row in conn.execute(
        "SELECT member_id, related_id FROM family_relationships "
        "WHERE rel_type IN ('cha', 'me', 'con_nuoi') AND deleted_at IS NULL"
    ).fetchall():
        parent_id, child_id = row["member_id"], row["related_id"]
        if parent_id in members and child_id in members:
            children_of[parent_id].add(child_id)

    # --- spouse edges (vo / chong) -------------------------------------------
    spouse_of: dict[int, int] = {}
    for row in conn.execute(
        "SELECT member_id, related_id FROM family_relationships "
        "WHERE rel_type IN ('vo', 'chong') AND deleted_at IS NULL"
    ).fetchall():
        a, b = row["member_id"], row["related_id"]
        if a in members and b in members and a not in spouse_of and b not in spouse_of:
            spouse_of[a] = b
            spouse_of[b] = a

    # --- build couple units --------------------------------------------------
    # Sort deterministically so the member processed first becomes "primary".
    # Priority: member with children becomes primary; tie-break by name.
    sorted_ids = sorted(
        members,
        key=lambda mid: (0 if children_of.get(mid) else 1, members[mid]["full_name"]),
    )

    units: dict[str, dict] = {}          # unit_id -> unit
    member_to_unit: dict[int, str] = {}  # member_id -> unit_id
    _counter = 0

    for mid in sorted_ids:
        if mid in member_to_unit:
            continue
        _counter += 1
        uid = f"u{_counter}"
        spouse_id = spouse_of.get(mid)
        if spouse_id and spouse_id not in member_to_unit:
            units[uid] = {"id": uid, "primary_id": mid, "spouse_id": spouse_id}
            member_to_unit[mid] = uid
            member_to_unit[spouse_id] = uid
        else:
            units[uid] = {"id": uid, "primary_id": mid, "spouse_id": None}
            member_to_unit[mid] = uid

    # --- compute unit-level edges --------------------------------------------
    unit_children: dict[str, set[str]] = defaultdict(set)  # parent_uid -> child_uids
    unit_parent: dict[str, str] = {}                        # child_uid -> parent_uid

    for parent_id, child_ids in children_of.items():
        parent_uid = member_to_unit.get(parent_id)
        if parent_uid is None:
            continue
        for child_id in child_ids:
            child_uid = member_to_unit.get(child_id)
            if child_uid and child_uid != parent_uid:
                unit_children[parent_uid].add(child_uid)
                unit_parent[child_uid] = parent_uid

    # --- group units by generation, sort rows --------------------------------
    gen_to_unit_ids: dict = defaultdict(list)
    for uid, unit in units.items():
        gen = members[unit["primary_id"]].get("generation")
        gen_to_unit_ids[gen].append(uid)

    sorted_gens = sorted(gen_to_unit_ids, key=lambda g: (g is None, g or 0))

    # Track column positions per unit for child-ordering within rows.
    unit_col: dict[str, int] = {}

    rows = []
    for gen in sorted_gens:
        uid_list = gen_to_unit_ids[gen]

        # Sort: units with a parent come after roots, grouped by parent position.
        def _sort_key(uid: str) -> tuple:
            p = unit_parent.get(uid)
            parent_col = unit_col.get(p, -1) if p else -1
            return (0 if p is None else 1, parent_col, members[units[uid]["primary_id"]]["full_name"])

        uid_list.sort(key=_sort_key)

        row_units = []
        for col_idx, uid in enumerate(uid_list):
            unit = units[uid]
            unit_col[uid] = col_idx
            primary = members[unit["primary_id"]]
            spouse = members[unit["spouse_id"]] if unit["spouse_id"] else None
            row_units.append({
                "id": uid,
                "primary": primary,
                "spouse": spouse,
                "parent_unit_id": unit_parent.get(uid),
                "child_unit_ids": sorted(unit_children.get(uid, set())),
            })

        gen_label = f"Đời {gen}" if gen is not None else "Chưa phân đời"
        rows.append({"gen": gen, "gen_label": gen_label, "units": row_units})

    return {"has_data": True, "rows": rows}


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
