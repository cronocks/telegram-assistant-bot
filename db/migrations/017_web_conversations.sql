-- FR-5.5: Web chat history — conversations + messages tables

CREATE TABLE web_conversations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id),
    title       TEXT,                           -- NULL until LLM generates; UI shows "New chat"
    created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP  -- bumped on every new message
);

CREATE INDEX idx_web_conversations_user_updated
    ON web_conversations(user_id, updated_at DESC);

CREATE TABLE web_messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL REFERENCES web_conversations(id),
    role            TEXT    NOT NULL,           -- 'user' | 'bot'
    text            TEXT    NOT NULL,
    created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_web_messages_conv_time
    ON web_messages(conversation_id, created_at);

CREATE INDEX idx_web_messages_text
    ON web_messages(conversation_id, text);
