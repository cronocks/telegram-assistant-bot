-- 024_add_year_to_anniversaries.sql
-- Add optional year column to anniversaries table.
-- Existing DBs (staging/production) ran migration 022 before year was added;
-- this migration adds the column via ALTER TABLE.
-- Fresh deploys already have year from 022 — migration runner handles the
-- duplicate column error gracefully.

ALTER TABLE anniversaries ADD COLUMN year INTEGER;
