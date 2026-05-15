-- 006_parent_links.sql
-- Tracks parent-child relationships between users.
-- A user may have at most one active parent at a time.
-- Deactivating is done by setting active = 0 (soft history kept).

CREATE TABLE IF NOT EXISTS parent_links (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id),
    parent_id   INTEGER NOT NULL REFERENCES users(id),
    set_by      INTEGER NOT NULL REFERENCES users(id),
    active      INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at  TEXT    NOT NULL DEFAULT (STRFTIME('%Y-%m-%dT%H:%M:%SZ', 'now')),
    removed_at  TEXT,
    CHECK (user_id != parent_id)
);

CREATE INDEX IF NOT EXISTS idx_parent_links_user   ON parent_links(user_id, active);
CREATE INDEX IF NOT EXISTS idx_parent_links_parent ON parent_links(parent_id, active);
