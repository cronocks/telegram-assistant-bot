-- FR-5: Web UI — add must_change_password flag to users + web session table

ALTER TABLE users ADD COLUMN must_change_password INTEGER NOT NULL DEFAULT 0;

CREATE TABLE web_sessions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id),
    token       TEXT NOT NULL UNIQUE,
    created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at  DATETIME NOT NULL,
    revoked_at  DATETIME
);

CREATE INDEX idx_web_sessions_token ON web_sessions(token);
CREATE INDEX idx_web_sessions_user  ON web_sessions(user_id);
