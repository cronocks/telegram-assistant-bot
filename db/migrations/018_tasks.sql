-- 018_tasks.sql
-- FR-7 — Task management.
-- Core task table: one row per task created by a user via Telegram or Web.
-- Soft-deleted rows (deleted_at IS NOT NULL) are excluded from all active queries
-- via partial indexes. Reminder rows live in task_reminders (migration 019).

CREATE TABLE IF NOT EXISTS tasks (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id             INTEGER NOT NULL REFERENCES users(id),
    title               TEXT    NOT NULL,
    description         TEXT,                               -- optional free-form detail
    deadline            TEXT    NOT NULL,                   -- ISO datetime, stored in VN TZ (Asia/Ho_Chi_Minh)
    category            TEXT    NOT NULL DEFAULT 'task',    -- 'task' | 'study' | 'reminder'
    scope               TEXT    NOT NULL DEFAULT 'private', -- v1: 'private' only (D13)
    recurring_rule      TEXT,                               -- NULL = one-shot; e.g. 'weekly:MON,WED@07:00'
    reminder_offsets    TEXT    NOT NULL DEFAULT '7200,3600,1800,900', -- CSV of seconds before deadline (D5)
    status              TEXT    NOT NULL DEFAULT 'pending', -- 'pending' | 'completed' | 'cancelled'
    completed_at        TEXT,                               -- ISO datetime; NULL until marked done
    snooze_count        INTEGER NOT NULL DEFAULT 0,         -- cumulative snoozes; max 3 (D6)
    source              TEXT    NOT NULL DEFAULT 'telegram', -- 'telegram' | 'web'
    created_at          TEXT    NOT NULL,
    updated_at          TEXT    NOT NULL,
    deleted_at          TEXT                                -- soft-delete timestamp (FR-4 recycle bin compat)
);

-- Active tasks by user + status (primary list/filter query).
CREATE INDEX IF NOT EXISTS idx_tasks_user_status
    ON tasks(user_id, status)
    WHERE deleted_at IS NULL;

-- Active pending tasks ordered by deadline (reminder engine + daily summary).
CREATE INDEX IF NOT EXISTS idx_tasks_deadline
    ON tasks(deadline)
    WHERE status = 'pending' AND deleted_at IS NULL;

-- Recurring tasks only (engine expands next occurrence after each fire).
CREATE INDEX IF NOT EXISTS idx_tasks_recurring
    ON tasks(recurring_rule)
    WHERE recurring_rule IS NOT NULL AND deleted_at IS NULL;
