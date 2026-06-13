-- 030_family_members.sql
-- FR-11 Phase A: family tree member profiles.
-- Separate from `users` — most members are deceased or not bot users.
-- Partial dates allowed (year only, or approximate) for old records.
-- Original lunar/solar dates are stored raw; conversion happens at runtime
-- only when computing reminder dates for a target year (FR-8 Decision #47).

CREATE TABLE IF NOT EXISTS family_members (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    full_name       TEXT    NOT NULL,
    alias_name      TEXT,
    gender          TEXT    CHECK (gender IN ('nam', 'nu') OR gender IS NULL),
    generation      INTEGER,
    branch          TEXT,
    birth_date_type TEXT    CHECK (birth_date_type IN ('lunar', 'solar') OR birth_date_type IS NULL),
    birth_year      INTEGER,
    birth_month     INTEGER,
    birth_day       INTEGER,
    birth_leap      INTEGER NOT NULL DEFAULT 0 CHECK (birth_leap IN (0, 1)),
    birth_approx    INTEGER NOT NULL DEFAULT 0 CHECK (birth_approx IN (0, 1)),
    death_date_type TEXT    CHECK (death_date_type IN ('lunar', 'solar') OR death_date_type IS NULL),
    death_year      INTEGER,
    death_month     INTEGER,
    death_day       INTEGER,
    death_leap      INTEGER NOT NULL DEFAULT 0 CHECK (death_leap IN (0, 1)),
    death_approx    INTEGER NOT NULL DEFAULT 0 CHECK (death_approx IN (0, 1)),
    bio             TEXT,
    photo_drive_id  TEXT,
    linked_user_id  INTEGER REFERENCES users(id),
    created_by      INTEGER NOT NULL REFERENCES users(id),
    created_at      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL,
    deleted_at      TEXT
);

CREATE INDEX IF NOT EXISTS idx_family_members_name ON family_members(full_name);
CREATE INDEX IF NOT EXISTS idx_family_members_gen  ON family_members(generation);
