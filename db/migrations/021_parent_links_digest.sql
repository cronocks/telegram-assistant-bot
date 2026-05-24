-- 021_parent_links_digest.sql
-- Add digest configuration columns to parent_links (FR-7 sub-task 7.6).
--
-- digest_frequency: how often to send the parent digest
--   'daily'   → every day at digest_time (HH:MM)
--   'weekly'  → every week on digest_time (DOW HH:MM, e.g. 'SUN 20:00')
--   'monthly' → every month on digest_time ('DAY HH:MM' or 'LAST HH:MM')
--   'off'     → disabled
--
-- digest_time: schedule string; format depends on digest_frequency.
--   NULL → use system default '21:00' (treated as daily 21:00).
--
-- last_digest_at: ISO-8601 timestamp of the last digest sent; NULL = never.

ALTER TABLE parent_links ADD COLUMN digest_frequency TEXT NOT NULL DEFAULT 'daily';
ALTER TABLE parent_links ADD COLUMN digest_time      TEXT;
ALTER TABLE parent_links ADD COLUMN last_digest_at   TEXT;
