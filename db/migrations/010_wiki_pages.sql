-- 010_wiki_pages.sql
-- SQLite ACL/index layer for wiki pages stored on Google Drive.
-- Drive holds content; this table tracks owner + scope for access control.
-- scope defaults to 'everyone' — wiki is shared family knowledge by default.
-- topic: human-readable name; slug: filesystem-safe identifier (underscored).

CREATE TABLE IF NOT EXISTS wiki_pages (
    id            INTEGER  PRIMARY KEY AUTOINCREMENT,
    drive_file_id TEXT     NOT NULL UNIQUE,
    owner_user_id INTEGER  NOT NULL REFERENCES users(id),
    scope         TEXT     NOT NULL DEFAULT 'everyone'
                           CHECK (scope IN ('private', 'everyone')),
    topic         TEXT     NOT NULL,
    slug          TEXT     NOT NULL,
    created_at    TEXT     NOT NULL DEFAULT (STRFTIME('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at    TEXT     NOT NULL DEFAULT (STRFTIME('%Y-%m-%dT%H:%M:%SZ', 'now')),
    deleted_at    TEXT
);

CREATE INDEX IF NOT EXISTS idx_wiki_pages_owner ON wiki_pages (owner_user_id);
CREATE INDEX IF NOT EXISTS idx_wiki_pages_scope ON wiki_pages (scope);
CREATE INDEX IF NOT EXISTS idx_wiki_pages_slug  ON wiki_pages (slug);
