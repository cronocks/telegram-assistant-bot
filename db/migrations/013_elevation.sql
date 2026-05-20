-- 013_elevation.sql
-- FR-3.5 — Privilege Elevation (sudo).
-- elevation_sessions: one active row per (channel, chat_id); re-elevate refreshes expires_at.
-- sudo_attempts:     rate-limit counters; failed_count resets on success.

CREATE TABLE IF NOT EXISTS elevation_sessions (
    channel       TEXT     NOT NULL,
    chat_id       TEXT     NOT NULL,
    base_user_id  INTEGER  NOT NULL REFERENCES users(id),
    started_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at    DATETIME NOT NULL,
    PRIMARY KEY (channel, chat_id)
);

CREATE TABLE IF NOT EXISTS sudo_attempts (
    channel         TEXT     NOT NULL,
    chat_id         TEXT     NOT NULL,
    failed_count    INTEGER  NOT NULL DEFAULT 0,
    locked_until    DATETIME,
    last_attempt_at DATETIME,
    PRIMARY KEY (channel, chat_id)
);
