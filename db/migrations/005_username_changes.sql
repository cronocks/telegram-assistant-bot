CREATE TABLE username_changes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL REFERENCES users(id),
    old_username    TEXT,
    new_username    TEXT NOT NULL,
    requested_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    approved_by     INTEGER REFERENCES users(id),
    approved_at     DATETIME,
    rejected_at     DATETIME,
    rejection_note  TEXT
);

CREATE INDEX idx_username_pending ON username_changes(user_id)
    WHERE approved_at IS NULL AND rejected_at IS NULL;
