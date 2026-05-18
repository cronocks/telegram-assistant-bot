-- 011_user_memory.sql
-- L1 Memory store: one row per (user, kind) pair.
-- kind 'memory': rolling facts/preferences curated by LLM (MEMORY.md equivalent).
-- kind 'user':   stable user profile snapshot curated by LLM (USER.md equivalent).
-- content starts empty; updated by manual command or future cron curation.
-- curated_at: timestamp of last LLM curation pass (NULL = never curated).

CREATE TABLE IF NOT EXISTS user_memory (
    user_id     INTEGER  NOT NULL REFERENCES users(id),
    kind        TEXT     NOT NULL CHECK (kind IN ('memory', 'user')),
    content     TEXT     NOT NULL DEFAULT '',
    updated_at  TEXT     NOT NULL DEFAULT (STRFTIME('%Y-%m-%dT%H:%M:%SZ', 'now')),
    curated_at  TEXT,
    PRIMARY KEY (user_id, kind)
);
