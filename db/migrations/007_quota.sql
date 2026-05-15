-- 007_quota.sql
-- Per-user monthly token quota tracking.
-- One row per user; month column resets automatically on first usage of a new month.
-- monthly_token_limit = 0 means unlimited.

CREATE TABLE IF NOT EXISTS user_quotas (
    user_id              INTEGER PRIMARY KEY REFERENCES users(id),
    monthly_token_limit  INTEGER NOT NULL DEFAULT 0,
    used_tokens          INTEGER NOT NULL DEFAULT 0,
    month                TEXT    NOT NULL DEFAULT (STRFTIME('%Y-%m', 'now')),
    updated_at           TEXT    NOT NULL DEFAULT (STRFTIME('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
