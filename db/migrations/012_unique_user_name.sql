-- Prevent duplicate active usernames.
-- Soft-deleted users (deleted_at IS NOT NULL) are excluded so names can be reused.
CREATE UNIQUE INDEX idx_users_unique_active_name
    ON users(name)
    WHERE deleted_at IS NULL;
