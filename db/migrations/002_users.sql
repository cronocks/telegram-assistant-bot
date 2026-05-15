CREATE TABLE users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT UNIQUE COLLATE NOCASE
                  CHECK (
                      username IS NULL
                      OR (length(username) BETWEEN 3 AND 32
                          AND username GLOB '[A-Za-z0-9_.-]*')
                  ),
    name          TEXT NOT NULL,
    role          TEXT NOT NULL CHECK (role IN ('admin', 'manager', 'member', 'readonly')),
    birthdate     DATE,
    created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    deleted_at    DATETIME
);

CREATE INDEX idx_users_role ON users(role) WHERE deleted_at IS NULL;
