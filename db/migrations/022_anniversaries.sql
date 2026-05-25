-- 022_anniversaries.sql
-- FR-8 — Anniversary / Memorial reminders.
-- One row per recurring annual event (giỗ, kỷ niệm cưới, sinh nhật người thân, ...).
-- Stores raw lunar/solar month-day; solar date is recomputed each year at runtime
-- (Decision #47 — single source of truth = original lunar date).

CREATE TABLE IF NOT EXISTS anniversaries (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id             INTEGER NOT NULL REFERENCES users(id),
    name                TEXT    NOT NULL,                          -- e.g. "Giỗ ông nội"
    date_type           TEXT    NOT NULL,                          -- 'lunar' | 'solar'
    month               INTEGER NOT NULL,                          -- 1-12
    day                 INTEGER NOT NULL,                          -- 1-30 (lunar may be 30)
    year                INTEGER,                                   -- optional: original year of the event
    category            TEXT    NOT NULL DEFAULT 'khac',           -- 'gio' | 'cuoi' | 'khac'
    is_leap_month       INTEGER NOT NULL DEFAULT 0,                -- 1 = lunar leap month (tháng nhuận)
    reminder_offsets    TEXT    NOT NULL DEFAULT '30,15,7,3,1,0',  -- CSV days before (0 = on the day)
    enabled             INTEGER NOT NULL DEFAULT 1,                -- 0 = paused, no reminders fired
    note                TEXT,                                      -- optional free-form note
    created_at          TEXT    NOT NULL,
    updated_at          TEXT    NOT NULL,
    deleted_at          TEXT                                       -- soft-delete (FR-4 recycle bin compat)
);

CREATE INDEX IF NOT EXISTS idx_anniversaries_user
    ON anniversaries(user_id)
    WHERE deleted_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_anniversaries_enabled
    ON anniversaries(enabled)
    WHERE enabled = 1 AND deleted_at IS NULL;
