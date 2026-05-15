CREATE TABLE channel_bindings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id),
    channel     TEXT NOT NULL,
    chat_id     TEXT NOT NULL,
    bound_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(channel, chat_id)
);

CREATE INDEX idx_bindings_user ON channel_bindings(user_id);

CREATE TABLE invite_codes (
    code              TEXT PRIMARY KEY,
    intended_user_id  INTEGER NOT NULL REFERENCES users(id),
    created_by        INTEGER NOT NULL REFERENCES users(id),
    created_at        DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at        DATETIME NOT NULL,
    used_at           DATETIME,
    used_channel      TEXT,
    used_chat_id      TEXT
);

CREATE INDEX idx_invite_unused ON invite_codes(expires_at) WHERE used_at IS NULL;
