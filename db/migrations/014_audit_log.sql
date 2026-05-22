-- 014_audit_log.sql
-- FR-4 — Audit log (immutable, append-only).
-- One row per noteworthy event. Application layer enforces immutability:
-- only SqliteAuditLog.log() writes here; no UPDATE/DELETE adapter method exists.
-- Payload is JSON text (nullable). actor_user_id is nullable for system events
-- (scheduled jobs, auto-purge, etc.).

CREATE TABLE IF NOT EXISTS audit_log (
    id              INTEGER  PRIMARY KEY AUTOINCREMENT,
    actor_user_id   INTEGER  REFERENCES users(id),
    action          TEXT     NOT NULL,
    target_type     TEXT,
    target_id       TEXT,
    payload         TEXT,
    created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_audit_actor_time
    ON audit_log(actor_user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_audit_target_time
    ON audit_log(target_type, target_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_audit_action_time
    ON audit_log(action, created_at DESC);
