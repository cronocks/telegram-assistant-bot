-- 009_notes.sql
-- SQLite ACL/index layer for notes and journal files stored on Google Drive.
-- Drive holds content; this table tracks owner + scope for access control.
-- scope: 'private' = owner only; 'everyone' = all active users.
-- kind:  'note' = regular note; 'journal' = daily journal entry.

CREATE TABLE IF NOT EXISTS notes (
    id            INTEGER  PRIMARY KEY AUTOINCREMENT,
    drive_file_id TEXT     NOT NULL UNIQUE,
    owner_user_id INTEGER  NOT NULL REFERENCES users(id),
    scope         TEXT     NOT NULL DEFAULT 'private'
                           CHECK (scope IN ('private', 'everyone')),
    kind          TEXT     NOT NULL DEFAULT 'note'
                           CHECK (kind IN ('note', 'journal')),
    title         TEXT,
    created_at    TEXT     NOT NULL DEFAULT (STRFTIME('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at    TEXT     NOT NULL DEFAULT (STRFTIME('%Y-%m-%dT%H:%M:%SZ', 'now')),
    deleted_at    TEXT
);

CREATE INDEX IF NOT EXISTS idx_notes_owner  ON notes (owner_user_id);
CREATE INDEX IF NOT EXISTS idx_notes_scope  ON notes (scope);
