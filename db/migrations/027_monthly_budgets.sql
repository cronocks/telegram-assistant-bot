CREATE TABLE IF NOT EXISTS monthly_budgets (
    user_id          INTEGER NOT NULL REFERENCES users(id),
    month            TEXT    NOT NULL,                     -- 'YYYY-MM'
    expense_budget   INTEGER,                              -- VND; NULL = not set
    savings_target   INTEGER,                              -- VND; NULL = not set
    alerts_sent      TEXT    NOT NULL DEFAULT '[]',        -- JSON array: e.g. ["80", "100"]
    created_at       TEXT    NOT NULL,
    updated_at       TEXT    NOT NULL,
    PRIMARY KEY (user_id, month)
);
