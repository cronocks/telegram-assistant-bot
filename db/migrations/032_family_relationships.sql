-- Migration 032: family_relationships table + link anniversaries to family members (FR-11 Phase B)

CREATE TABLE IF NOT EXISTS family_relationships (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    member_id   INTEGER NOT NULL REFERENCES family_members(id),
    related_id  INTEGER NOT NULL REFERENCES family_members(id),
    rel_type    TEXT    NOT NULL CHECK (rel_type IN ('cha', 'me', 'vo', 'chong', 'con_nuoi')),
    note        TEXT,
    created_by  INTEGER NOT NULL REFERENCES users(id),
    created_at  TEXT    NOT NULL,
    deleted_at  TEXT,
    CHECK (member_id != related_id),
    UNIQUE (member_id, related_id, rel_type)
);

CREATE INDEX IF NOT EXISTS idx_family_rel_member  ON family_relationships(member_id);
CREATE INDEX IF NOT EXISTS idx_family_rel_related ON family_relationships(related_id);

ALTER TABLE anniversaries ADD COLUMN family_member_id INTEGER REFERENCES family_members(id);
