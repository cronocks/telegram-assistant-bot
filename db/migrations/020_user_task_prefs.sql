-- 020_user_task_prefs.sql
-- FR-7 — Per-user task preference columns on the existing users table.
-- Kept as a separate migration (not in 018) because this touches a different table.
--
-- Semantics:
--   daily_summary_time  NULL => default 21:00 VN | 'off' => disabled | 'HH:MM' => custom  (D8)
--   morning_default_time NULL => default 09:00 VN | 'HH:MM' => custom                     (D16)
--
-- Both columns are NULLABLE so no backfill is needed for existing rows; the
-- application layer interprets NULL as "use system default".

ALTER TABLE users ADD COLUMN daily_summary_time  TEXT;
ALTER TABLE users ADD COLUMN morning_default_time TEXT;
