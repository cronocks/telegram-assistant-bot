-- 008_password.sql
-- Adds password_hash column to users for future local-auth support.
-- NULL means the user has not set a password (OAuth / invite-only flow).

ALTER TABLE users ADD COLUMN password_hash TEXT;
