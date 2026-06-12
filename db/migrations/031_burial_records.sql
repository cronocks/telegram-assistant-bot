-- 031_burial_records.sql
-- FR-11 Phase A: burial/grave location records for family members.
-- 1-n with family_members: a member may be reburied/relocated; the newest
-- record has is_current = 1, older records are kept as history.
-- photo_drive_ids holds a JSON array of Google Drive file IDs.

CREATE TABLE IF NOT EXISTS burial_records (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    member_id       INTEGER NOT NULL REFERENCES family_members(id),
    cemetery_name   TEXT    NOT NULL,
    address         TEXT,
    lat             REAL,
    lng             REAL,
    plot_info       TEXT,
    buried_date     TEXT,
    relocated_date  TEXT,
    is_current      INTEGER NOT NULL DEFAULT 1 CHECK (is_current IN (0, 1)),
    photo_drive_ids TEXT,
    note            TEXT,
    created_by      INTEGER NOT NULL REFERENCES users(id),
    created_at      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL,
    deleted_at      TEXT
);

CREATE INDEX IF NOT EXISTS idx_burial_member ON burial_records(member_id, is_current);
