-- 015_notifications.sql
-- FR-4 sub 4.5 — Persistent notification queue.
-- One row per notification request. Producer (FR-7 reminders, FR-4 audit
-- failures, etc.) inserts with status='pending'. The scheduled flush job
-- attempts delivery via the registered ChannelAdapter and transitions to
-- 'delivered' on success or 'failed' after max retries.

CREATE TABLE IF NOT EXISTS pending_notifications (
    id              INTEGER  PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER  NOT NULL REFERENCES users(id),
    channel         TEXT     NOT NULL,
    payload         TEXT     NOT NULL,   -- JSON dict; minimum: {kind, text}
    status          TEXT     NOT NULL DEFAULT 'pending',  -- 'pending'|'delivered'|'failed'
    attempts        INTEGER  NOT NULL DEFAULT 0,
    last_error      TEXT,
    next_retry_at   DATETIME,            -- NULL = ready immediately
    created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    delivered_at    DATETIME
);

-- Partial index keeps the scheduled flush query cheap on a large table.
CREATE INDEX IF NOT EXISTS idx_notif_pending_ready
    ON pending_notifications(next_retry_at)
    WHERE status = 'pending';
