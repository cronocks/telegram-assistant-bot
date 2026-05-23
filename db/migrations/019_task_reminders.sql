-- 019_task_reminders.sql
-- FR-7 — Per-task reminder rows.
-- Each task produces N reminder rows (one per offset in tasks.reminder_offsets).
-- The reminder engine scans this table every minute, fires due rows, and expands
-- recurring tasks by inserting fresh rows for the next occurrence (D11 lazy expansion).

CREATE TABLE IF NOT EXISTS task_reminders (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id         INTEGER NOT NULL REFERENCES tasks(id),
    fire_at         TEXT    NOT NULL,                   -- ISO datetime when to send the notification
    offset_seconds  INTEGER NOT NULL,                   -- seconds before deadline (0 for snoozed rows)
    kind            TEXT    NOT NULL DEFAULT 'scheduled', -- 'scheduled' | 'snoozed'
    status          TEXT    NOT NULL DEFAULT 'pending', -- 'pending' | 'fired' | 'missed' | 'cancelled'
    fired_at        TEXT,                               -- ISO datetime; NULL until fired
    created_at      TEXT    NOT NULL
);

-- Primary scan index: engine fetches WHERE status='pending' AND fire_at <= now.
-- Partial index keeps the scan fast even on a large table.
CREATE INDEX IF NOT EXISTS idx_reminders_ready
    ON task_reminders(fire_at, status)
    WHERE status = 'pending';

-- Lookup: cancel all pending reminders for a given task on complete/delete.
CREATE INDEX IF NOT EXISTS idx_reminders_task
    ON task_reminders(task_id);
