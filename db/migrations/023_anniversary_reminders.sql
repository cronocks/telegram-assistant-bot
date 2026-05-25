-- 023_anniversary_reminders.sql
-- FR-8 — Per-anniversary, per-year reminder rows.
-- The annual compute job inserts (anniversary_id, year, offset_days) for every
-- active anniversary at startup and on Jan 1st. UNIQUE constraint makes the job
-- idempotent — re-running never produces duplicates.

CREATE TABLE IF NOT EXISTS anniversary_reminders (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    anniversary_id  INTEGER NOT NULL REFERENCES anniversaries(id),
    year            INTEGER NOT NULL,                  -- solar year this row fires in
    fire_at         TEXT    NOT NULL,                  -- ISO datetime (08:00 VN of the reminder day)
    offset_days     INTEGER NOT NULL,                  -- days before the anniversary (0 = on the day)
    status          TEXT    NOT NULL DEFAULT 'pending', -- 'pending' | 'fired' | 'missed' | 'cancelled'
    fired_at        TEXT,                              -- ISO datetime; NULL until fired
    created_at      TEXT    NOT NULL,
    UNIQUE(anniversary_id, year, offset_days)
);

-- Primary scan index: engine fetches WHERE status='pending' AND fire_at <= now.
CREATE INDEX IF NOT EXISTS idx_anniv_reminders_ready
    ON anniversary_reminders(fire_at, status)
    WHERE status = 'pending';

-- Lookup: cancel all reminders when an anniversary is deleted or disabled.
CREATE INDEX IF NOT EXISTS idx_anniv_reminders_parent
    ON anniversary_reminders(anniversary_id);
