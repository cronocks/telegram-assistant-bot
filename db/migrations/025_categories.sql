CREATE TABLE IF NOT EXISTS categories (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER REFERENCES users(id),          -- NULL = family-shared
    name         TEXT    NOT NULL,
    kind         TEXT    NOT NULL,                      -- 'income' | 'expense'
    parent_id    INTEGER REFERENCES categories(id),     -- nested (v1: unused, schema-ready)
    created_at   TEXT    NOT NULL,
    updated_at   TEXT    NOT NULL,
    deleted_at   TEXT
);

CREATE INDEX idx_categories_user ON categories(user_id) WHERE deleted_at IS NULL;
CREATE INDEX idx_categories_kind ON categories(kind) WHERE deleted_at IS NULL;
