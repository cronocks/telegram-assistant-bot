CREATE TABLE IF NOT EXISTS credit_cards (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER REFERENCES users(id),          -- NULL = family-shared
    name         TEXT    NOT NULL,
    created_at   TEXT    NOT NULL,
    updated_at   TEXT    NOT NULL,
    deleted_at   TEXT
);

CREATE INDEX idx_credit_cards_user ON credit_cards(user_id) WHERE deleted_at IS NULL;
