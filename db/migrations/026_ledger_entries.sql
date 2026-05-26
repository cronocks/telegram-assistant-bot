CREATE TABLE IF NOT EXISTS ledger_entries (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL REFERENCES users(id),
    kind          TEXT    NOT NULL,                        -- 'income' | 'expense'
    amount        INTEGER NOT NULL,                        -- VND, never FLOAT
    category_id   INTEGER REFERENCES categories(id),
    note          TEXT,                                    -- raw user description
    occurred_at   TEXT    NOT NULL,                        -- ISO datetime VN
    source        TEXT    NOT NULL,                        -- 'telegram' | 'web'
    created_at    TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL,
    voided_at     TEXT                                     -- soft-delete
);

CREATE INDEX idx_ledger_user_occurred ON ledger_entries(user_id, occurred_at DESC)
    WHERE voided_at IS NULL;
CREATE INDEX idx_ledger_user_cat_occurred ON ledger_entries(user_id, category_id, occurred_at DESC)
    WHERE voided_at IS NULL;
