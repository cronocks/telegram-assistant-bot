"""interfaces.py — Protocol definitions for hexagonal architecture.

The core (core_handler) depends only on these abstractions, never on concrete
implementations (Anthropic, Google Drive, Telegram, etc.). To swap an adapter
(e.g. cloud Drive -> local FS, Anthropic -> Ollama), only the wiring in main.py
needs to change; the core stays untouched.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Protocol, runtime_checkable


# ─── Domain types ─────────────────────────────────────────────────────────────

@dataclass
class User:
    """A registered bot user backed by the SQLite users table."""
    id: int
    name: str
    role: str                        # 'admin' | 'manager' | 'member' | 'readonly'
    username: str | None = None      # login identifier; nullable until set by user
    birthdate: date | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    deleted_at: datetime | None = None

    @property
    def is_active(self) -> bool:
        return self.deleted_at is None

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def is_manager(self) -> bool:
        return self.role in ("admin", "manager")


@dataclass
class ChannelMessage:
    """One inbound message normalized by a ChannelAdapter for the core to handle."""
    channel: str           # "telegram" | "discord" | "web" (future)
    chat_id: str           # external conversation id (e.g. telegram chat_id)
    text: str
    raw: dict = field(default_factory=dict)   # original payload, kept for debugging


# ─── LLM ──────────────────────────────────────────────────────────────────────

@runtime_checkable
class LLMClient(Protocol):
    """Abstract LLM provider. Concrete impl: AnthropicLLM today; Ollama/OpenAI later."""

    def ask(self, user_message: str, notes_context: str = "") -> tuple[str, int]:
        """General Q&A. Returns (reply_text, total_tokens)."""
        ...

    def summarize_notes(self, notes: list[dict]) -> tuple[str, int]:
        """Summarize a list of notes. Returns (summary, total_tokens)."""
        ...

    def extract_search_intent(self, question: str) -> tuple[dict, int]:
        """Parse vague question into structured intent.

        Returns (intent_dict, total_tokens). intent_dict shape:
            {"needs_search": bool, "keywords": list[str], "days_back": int}
        """
        ...

    def extract_wiki_updates(
        self, raw_content: str, existing_topics: list[str]
    ) -> tuple[list[dict], int]:
        """Analyze raw content -> list of wiki updates to apply.

        Returns (updates, total_tokens). Each update has shape:
            {topic, type, action, existing_topic, content_to_add}
        """
        ...

    def answer_from_wiki(
        self, question: str, wiki_pages: list[dict]
    ) -> tuple[str, int]:
        """Answer a question using provided wiki pages as context."""
        ...

    def generate_wiki_tldr(self, topic: str, content: str) -> tuple[str, int]:
        """Generate a 1-sentence TLDR for a new wiki page (used in index)."""
        ...

    def select_wiki_pages_from_index(
        self, question: str, index_content: str
    ) -> tuple[list[str], int]:
        """Pick relevant wiki page filenames from the index. Returns (filenames, tokens)."""
        ...

    def curate_memory(
        self,
        recent_notes: list[dict],
        current_memory: str,
        current_user_profile: str,
    ) -> tuple[str, str, int]:
        """Refine L1 memory from recent notes.

        Returns (new_memory_md, new_user_md, total_tokens).
        new_memory_md  — updated rolling facts/preferences snapshot
        new_user_md    — updated stable user profile snapshot
        """
        ...

    def generate_chat_title(self, user_msg: str, bot_reply: str) -> tuple[str, int]:
        """Generate a short title (~3-7 words) for a web conversation from its first exchange.

        Uses a cheap model (Haiku). Returns (title, total_tokens).
        Caller should use a truncated fallback if this raises.
        """
        ...


# ─── Note store (raw notes / journal) ────────────────────────────────────────

@runtime_checkable
class NoteStore(Protocol):
    """Abstract storage for raw notes and the daily journal.

    Concrete impl: DriveNoteStore (cloud) today; LocalFSNoteStore later.
    """

    def save_note(
        self, title: str, content: str, custom_filename: str | None = None
    ) -> tuple[str, str]:
        """Create a new note file. Returns (filename, drive_file_id)."""
        ...

    def search_notes(self, keyword: str, max_results: int = 5) -> list[dict]:
        """Full-text search. Returns [{id, name, modified, content}]."""
        ...

    def get_recent_notes(self, days: int = 7, max_results: int = 5) -> list[dict]:
        """Notes modified within the last N days. Legacy helper. Returns [{id, name, modified, content}]."""
        ...

    def test_connection(self) -> dict:
        """Smoke-test storage connectivity. Returns provider-specific info dict."""
        ...

    def find_files_fuzzy(self, query: str) -> list[dict]:
        """Fuzzy match by filename substring. Returns [{id, name, modifiedTime}]."""
        ...

    def append_to_file(self, file_id: str, append_content: str) -> str:
        """Append content to the end of an existing file. Returns the filename."""
        ...

    def read_file_by_id(self, file_id: str) -> dict:
        """Read a file by its id. Returns {id, name, content, modifiedTime}."""
        ...

    def list_recent_files(self, limit: int | None = None) -> list[dict]:
        """List the N most recently modified files."""
        ...

    def list_all_notes(self) -> list[dict]:
        """List all files in the notes folder, newest-created first.

        Returns [{id, name, createdTime}].
        """
        ...

    def add_to_daily_journal(self, content: str) -> tuple[str, str, str]:
        """Append (or create) today's journal entry.

        Returns (filename, action, drive_file_id).
        action is "created" or "appended".
        """
        ...

    def get_today_journal(self) -> dict | None:
        """Return today's journal file content, or None if not created yet."""
        ...

    def smart_search(
        self, keywords: list[str], days_back: int = 0, max_per_keyword: int = 3
    ) -> list[dict]:
        """Multi-keyword search with an optional timeframe filter. Returns [{id, name, modified, content}]."""
        ...

    def get_current_week_notes(self, max_results: int = 20) -> list[dict]:
        """All notes modified during the current local week (Mon..Sun). Returns [{id, name, modified, content}]."""
        ...

    def delete_file(self, file_id: str) -> bool:
        """Permanently delete a file. Best-effort: returns False on failure, never raises."""
        ...


# ─── Wiki store (LLM-organized topic pages + index) ──────────────────────────

@runtime_checkable
class WikiStore(Protocol):
    """Abstract storage for the wiki layer (topic pages + index)."""

    def list_pages(self) -> list[dict]:
        """List all wiki pages excluding the index. Returns [{id, name, modifiedTime}]."""
        ...

    def get_topic_names(self) -> list[str]:
        """Return slugs (without .md, underscores -> spaces) for prompting."""
        ...

    def find_page(self, topic: str) -> dict | None:
        """Find a wiki page by topic (slug or partial match). Returns {id, name, content}."""
        ...

    def save_page(self, topic: str, content: str, file_id: str | None = None) -> tuple[str, str]:
        """Create or overwrite a wiki page. Returns (filename, drive_file_id)."""
        ...

    def append_to_page(self, file_id: str, new_section: str) -> str:
        """Append a timestamped section to an existing wiki page."""
        ...

    def build_new_page(self, topic: str, topic_type: str, content_to_add: str) -> str:
        """Build the full markdown body (with frontmatter) for a new wiki page."""
        ...

    def delete_file(self, file_id: str) -> bool:
        """Permanently delete a wiki page file. Best-effort: returns False on failure."""
        ...

    def build_section(self, content_to_add: str) -> str:
        """Build a timestamped section snippet for appending."""
        ...

    def add_to_index(
        self, topic: str, slug: str, topic_type: str, tldr: str
    ) -> None:
        """Append a row to the wiki index for a newly created page."""
        ...

    def retrieve_pages(
        self,
        question: str,
        keywords: list[str],
        visible_slugs: "set[str] | None" = None,
    ) -> list[dict]:
        """Single retrieval entry point.

        Today: read index -> filter by visible_slugs -> LLM picks filenames -> read pages.
        Future (vector DB): embed(question) -> top-k. Caller signature unchanged.

        visible_slugs: if provided, index rows are pre-filtered to slugs the viewer
            may read before being passed to the LLM (prevents information leakage).
            None means no filter (legacy / pre-ACL behaviour).

        Returns [{id, name, content}].
        """
        ...


# ─── Note / Wiki index (SQLite ACL layer) ────────────────────────────────────

@runtime_checkable
class NoteIndex(Protocol):
    """SQLite ACL/index layer that maps Drive file IDs to owner + scope.

    Concrete impl: SqliteNoteIndex (note_index.py).
    Drive holds content; this index controls who can see what.
    """

    # ── Write ─────────────────────────────────────────────────────────────────

    def add_note(
        self,
        drive_file_id: str,
        owner_user_id: int,
        kind: str = "note",
        title: str | None = None,
        scope: str = "private",
    ) -> int:
        """Insert a new note row. Returns the SQLite row id."""
        ...

    def add_wiki_page(
        self,
        drive_file_id: str,
        owner_user_id: int,
        topic: str,
        slug: str,
        scope: str = "everyone",
    ) -> int:
        """Insert a new wiki_page row. Returns the SQLite row id."""
        ...

    def touch_note(self, drive_file_id: str) -> None:
        """Bump updated_at for an existing note (called on append)."""
        ...

    def touch_wiki_page(self, drive_file_id: str) -> None:
        """Bump updated_at for an existing wiki page (called on append)."""
        ...

    def set_note_scope(
        self, drive_file_id: str, scope: str, requester_id: int
    ) -> bool:
        """Change note scope. Returns False if requester is not the owner."""
        ...

    def set_wiki_scope(
        self, drive_file_id: str, scope: str, requester_id: int
    ) -> bool:
        """Change wiki page scope. Returns False if requester is not the owner."""
        ...

    # ── FR-4 recycle bin ──────────────────────────────────────────────────────

    def soft_delete_note(self, note_id: int) -> bool: ...
    def soft_delete_wiki(self, wiki_id: int) -> bool: ...
    def list_deleted_notes(self) -> list[dict]: ...
    def list_deleted_wiki_pages(self) -> list[dict]: ...
    def restore_note(self, note_id: int) -> bool: ...
    def restore_wiki(self, wiki_id: int) -> bool: ...
    def hard_delete_note(self, note_id: int) -> "dict | None": ...
    def hard_delete_wiki(self, wiki_id: int) -> "dict | None": ...
    def list_soft_deleted_notes_older_than(self, threshold_iso: str) -> list[dict]: ...
    def list_soft_deleted_wiki_older_than(self, threshold_iso: str) -> list[dict]: ...
    def list_soft_deleted_notes_by_owner(self, owner_user_id: int) -> list[dict]: ...
    def list_soft_deleted_wiki_by_owner(self, owner_user_id: int) -> list[dict]: ...

    # ── Read ──────────────────────────────────────────────────────────────────

    def get_note_meta(self, drive_file_id: str) -> "dict | None":
        """Return {id, drive_file_id, owner_user_id, scope, kind, title} or None."""
        ...

    def get_wiki_meta(self, drive_file_id: str) -> "dict | None":
        """Return {id, drive_file_id, owner_user_id, scope, topic, slug} or None."""
        ...

    def note_meta_for_ids(self, drive_file_ids: "list[str]") -> "list[dict]":
        """Return note metadata rows for a list of Drive file IDs.

        Used by retrieval paths to ACL-filter Drive search results.
        File IDs with no SQLite row (orphans) are omitted — safe default.
        """
        ...

    def visible_wiki_slugs(self, viewer_id: int) -> "set[str]":
        """Return slugs of wiki pages the viewer may read.

        Used to pre-filter _index.md before LLM page selection so the LLM
        never sees slugs of pages it should not access.
        """
        ...


# ─── L1 Memory store (SQLite user_memory table) ──────────────────────────────

@runtime_checkable
class MemoryStore(Protocol):
    """Abstract L1 memory store. Concrete impl: SqliteMemoryStore.

    Each user has two named slots: 'memory' (rolling facts) and 'user' (profile).
    Content starts empty and is populated by LLM curation on demand.
    """

    def get(self, user_id: int, kind: str) -> str:
        """Return stored content for (user_id, kind), or '' if none yet."""
        ...

    def get_meta(self, user_id: int, kind: str) -> "dict | None":
        """Return full metadata row {user_id, kind, content, updated_at, curated_at} or None."""
        ...

    def set(self, user_id: int, kind: str, content: str, mark_curated: bool = False) -> None:
        """Upsert content for (user_id, kind). Pass mark_curated=True after LLM curation."""
        ...


# ─── Embedding (placeholder for future vector layer) ─────────────────────────

@runtime_checkable
class EmbedClient(Protocol):
    """Abstract embedding provider. Not used in FR-1; defined to lock the shape."""

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Return one embedding vector per input text."""
        ...

    @property
    def dim(self) -> int:
        """Embedding dimension (e.g. 512 for voyage-3-lite, 1024 for BGE-M3)."""
        ...

    @property
    def model_name(self) -> str:
        """Provider/model identifier, stored alongside vectors for compat checks."""
        ...


# ─── Channel adapter (inbound/outbound for one messaging channel) ────────────

@runtime_checkable
class ChannelAdapter(Protocol):
    """Abstract messaging channel. Concrete impl: TelegramAdapter today."""

    def parse_webhook(self, payload: dict) -> ChannelMessage | None:
        """Convert a raw provider webhook payload into a ChannelMessage.

        Returns None if the payload should be ignored (no text, edit event, etc.).
        """
        ...

    def is_authorized(self, msg: ChannelMessage) -> bool:
        """Check whether the sender is allowed to use the bot."""
        ...

    async def send(
        self, chat_id: str, text: str, use_markdown: bool = True
    ) -> None:
        """Send a message to the given conversation."""
        ...

    async def delete_message(self, chat_id: str, message_id: int) -> bool:
        """Best-effort delete of a previously-sent message. Returns True on success.

        Used for password hygiene (erasing plaintext password messages after
        processing). Adapters that don't support deletion should return False.
        """
        ...


# ─── Elevation store (FR-3.5 — sudo) ─────────────────────────────────────────

@runtime_checkable
class ElevationStore(Protocol):
    """Abstract privilege-elevation store. Concrete impl: SqliteElevationStore.

    Each (channel, chat_id) pair can hold at most one active session that
    overrides the bound user's role to 'admin' for a fixed TTL. Failed sudo
    attempts are rate-limited.
    """

    def get_active_session(self, channel: str, chat_id: str) -> "dict | None": ...
    def elevate(
        self,
        channel: str,
        chat_id: str,
        base_user_id: int,
        ttl_minutes: int | None = None,
    ) -> str: ...
    def drop_session(self, channel: str, chat_id: str) -> bool: ...
    def get_attempts(self, channel: str, chat_id: str) -> dict: ...
    def is_locked(self, channel: str, chat_id: str) -> "tuple[bool, str | None]": ...
    def record_failure(
        self,
        channel: str,
        chat_id: str,
        max_fails: int | None = None,
        lockout_minutes: int | None = None,
    ) -> dict: ...
    def reset_failures(self, channel: str, chat_id: str) -> None: ...


# ─── Notification framework (FR-4 sub 4.5) ───────────────────────────────────

@runtime_checkable
class NotificationStore(Protocol):
    """Abstract pending-notification queue. Concrete impl: SqliteNotificationStore."""

    def enqueue(self, user_id: int, channel: str, payload: dict) -> int: ...
    def get_by_id(self, notif_id: int) -> "dict | None": ...
    def get_pending_ready(self, now=None, limit: int = 100) -> list[dict]: ...
    def mark_delivered(self, notif_id: int, now=None) -> bool: ...
    def record_failed_attempt(
        self, notif_id: int, error: str, max_attempts: int = 5, now=None,
    ) -> dict: ...


@runtime_checkable
class NotificationService(Protocol):
    """Producer-facing notification API + scheduled flush.

    Concrete impl: notification_service.NotificationService.
    """

    def enqueue(self, user_id: int, channel: str, payload: dict) -> int: ...

    async def flush_pending(self, now=None) -> dict: ...


# ─── Audit log (FR-4) ─────────────────────────────────────────────────────────

@runtime_checkable
class AuditLog(Protocol):
    """Abstract append-only audit log. Concrete impl: SqliteAuditLog.

    Only `log()` writes; immutability is enforced by NOT exposing update/delete.
    `actor_user_id=None` denotes a system event. Payload is an optional dict
    serialized to JSON. See docs/FR-4-PLAN.md section 2.3 for the action
    taxonomy.
    """

    def log(
        self,
        actor_user_id: int | None,
        action: str,
        target_type: str | None = None,
        target_id: "str | int | None" = None,
        payload: "dict | None" = None,
    ) -> int: ...

    def list_recent(
        self,
        limit: int = 50,
        offset: int = 0,
        actor_user_id: int | None = None,
        action: str | None = None,
        target_type: str | None = None,
        target_id: "str | int | None" = None,
    ) -> list: ...


# ─── User store ───────────────────────────────────────────────────────────────

@runtime_checkable
class UserStore(Protocol):
    """Abstract user registry. Concrete impl: SqliteUserStore."""

    def get_user_by_id(self, user_id: int) -> User | None: ...
    def list_users(self, include_deleted: bool = False) -> list[User]: ...
    def find_by_channel(self, channel: str, chat_id: str) -> User | None: ...
    def create_user(
        self,
        name: str,
        role: str,
        birthdate: date | None = None,
        username: str | None = None,
    ) -> User: ...
    def soft_delete_user(self, user_id: int) -> None: ...
    def list_deleted_users(self, older_than: str | None = None) -> list[User]: ...
    def restore_user(self, user_id: int) -> bool: ...
    def hard_delete_user(self, user_id: int) -> bool: ...
    def find_users_turning_18(self, on_date: date) -> list[User]: ...
    def get_chat_id_for_user(self, user_id: int, channel: str) -> "str | None": ...
    def update_user_role(self, user_id: int, role: str) -> None: ...
    def bind_channel(self, user_id: int, channel: str, chat_id: str) -> None: ...
    def create_invite_code(
        self, intended_user_id: int, created_by: int, ttl_days: int = 7
    ) -> str: ...
    def consume_invite_code(
        self, code: str, channel: str, chat_id: str
    ) -> User | None: ...
    def set_username_direct(self, user_id: int, username: str) -> None: ...
    def request_username_change(self, user_id: int, new_username: str) -> int: ...
    def get_pending_username_change(self, user_id: int) -> dict | None: ...
    def list_pending_username_changes(self) -> list[dict]: ...
    def approve_username_change(self, request_id: int, approver_id: int) -> bool: ...
    def reject_username_change(self, request_id: int, approver_id: int, note: str = "") -> bool: ...
    def request_birthdate_change(self, user_id: int, new_birthdate: date) -> int: ...
    def get_pending_birthdate_change(self, user_id: int) -> dict | None: ...
    def list_pending_birthdate_changes(self) -> list[dict]: ...
    def approve_birthdate_change(self, request_id: int, approver_id: int) -> bool: ...
    def reject_birthdate_change(self, request_id: int, approver_id: int, note: str = "") -> bool: ...
    def get_quota(self, user_id: int) -> "dict | None": ...
    def set_quota(self, user_id: int, monthly_token_limit: int) -> None: ...
    def record_usage(self, user_id: int, tokens: int) -> None: ...
    def reset_usage(self, user_id: int) -> bool: ...
    def set_parent(self, user_id: int, parent_id: int, set_by: int) -> None: ...
    def get_parent(self, user_id: int) -> "User | None": ...
    def get_children(self, parent_id: int) -> "list[User]": ...
    def remove_parent(self, user_id: int, removed_by: int) -> bool: ...
    def set_password(self, user_id: int, plain: str) -> None: ...
    def check_password(self, user_id: int, plain: str) -> bool: ...
    def set_must_change_password(self, user_id: int, flag: bool) -> None: ...
    def get_must_change_password(self, user_id: int) -> bool: ...
    def get_password_hash(self, user_id: int) -> "str | None": ...
    def find_by_username_or_name(self, login: str) -> "User | None": ...
    def bootstrap_admin(self) -> "User | None": ...


# ─── Web session store (FR-5) ─────────────────────────────────────────────────

@runtime_checkable
class WebSessionStore(Protocol):
    """Abstract web session store. Concrete impl: SqliteWebSessionStore.

    Tokens are 32-byte random hex (256-bit entropy). Sessions are server-side
    revocable: logout sets revoked_at rather than relying solely on cookie deletion.
    """

    def create(self, user_id: int) -> str:
        """Create a new session for user_id. Returns the session token."""
        ...

    def find_active(self, token: str) -> "int | None":
        """Return user_id for a valid (non-expired, non-revoked) token, or None."""
        ...

    def revoke(self, token: str) -> bool:
        """Mark a session as revoked. Returns True if token existed."""
        ...

    def revoke_all_for_user(self, user_id: int) -> int:
        """Revoke all active sessions for a user. Returns count revoked."""
        ...


# ─── Web conversation store (FR-5.5) ─────────────────────────────────────────

@runtime_checkable
class WebConversationStore(Protocol):
    """Abstract web conversation + message store. Concrete impl: SqliteWebConversationStore.

    Conversations are lazy-created: a row is only inserted when the user sends
    their first message (not on "New chat" button press). SSE queues are keyed
    by conversation_id so multi-tab users get replies in the correct tab.
    """

    def create(self, user_id: int) -> int:
        """Create an empty conversation for user_id. Returns conversation id."""
        ...

    def get(self, conv_id: int) -> "dict | None":
        """Return {id, user_id, title, created_at, updated_at} or None."""
        ...

    def list_for_user(self, user_id: int) -> "list[dict]":
        """Return all conversations for user ordered by updated_at DESC."""
        ...

    def rename(self, conv_id: int, new_title: str) -> bool:
        """Update title. Returns True if the conversation exists."""
        ...

    def set_title_if_null(self, conv_id: int, title: str) -> bool:
        """Set title only when currently NULL (idempotent for async title gen).

        Returns True if the title was actually written (was NULL before).
        """
        ...

    def add_message(self, conv_id: int, role: str, text: str) -> int:
        """Insert a message and bump conversation.updated_at. Returns message id."""
        ...

    def list_messages(self, conv_id: int) -> "list[dict]":
        """Return [{id, role, text, created_at}] in chronological order."""
        ...

    def count_messages(self, conv_id: int) -> int:
        """Return total message count for a conversation."""
        ...

    def search(
        self, user_id: int, query: str, limit: int = 50
    ) -> "list[dict]":
        """LIKE-based search across messages for a user.

        Returns [{conv_id, conv_title, message_id, role, snippet, created_at}].
        query is escaped so % and _ are treated as literals.
        """
        ...

    def admin_list_for_user(self, target_user_id: int) -> "list[dict]":
        """Admin stealth-read path: list conversations of any user.

        Bypasses ownership check. Caller is responsible for verifying admin
        role and under-18 status before calling this method.
        """
        ...


# ─── Task store (FR-7) ───────────────────────────────────────────────────────

@runtime_checkable
class TaskStore(Protocol):
    """Abstract task store. Concrete impl: SqliteTaskStore (task_store.py).

    Covers CRUD for tasks and the helpers used by daily summary / reminder engine.
    """

    def create_task(
        self,
        user_id: int,
        title: str,
        deadline: str,
        *,
        description: "str | None" = None,
        category: str = "task",
        scope: str = "private",
        recurring_rule: "str | None" = None,
        reminder_offsets: str = "7200,3600,1800,900",
        source: str = "telegram",
    ) -> dict: ...

    def get_task(self, task_id: int) -> "dict | None": ...

    def list_for_user(
        self,
        user_id: int,
        *,
        status: "str | None" = None,
        include_deleted: bool = False,
    ) -> "list[dict]": ...

    def list_pending_due(
        self,
        before_iso: str,
        *,
        user_id: "int | None" = None,
    ) -> "list[dict]": ...

    def list_completed_on(self, user_id: int, date_prefix: str) -> "list[dict]": ...

    def update_task(self, task_id: int, **fields) -> "dict | None": ...
    def complete_task(self, task_id: int, completed_at: "str | None" = None) -> "dict | None": ...
    def cancel_task(self, task_id: int) -> "dict | None": ...
    def increment_snooze(self, task_id: int) -> int: ...
    def soft_delete_task(self, task_id: int) -> bool: ...
    def restore_task(self, task_id: int) -> bool: ...


# ─── Reminder store (FR-7) ───────────────────────────────────────────────────

@runtime_checkable
class ReminderStore(Protocol):
    """Abstract reminder store. Concrete impl: SqliteReminderStore (reminder_store.py).

    Covers per-task reminder row lifecycle used by reminder engine and task CRUD.
    """

    def bulk_create_for_task(
        self,
        task_id: int,
        deadline_iso: str,
        offset_seconds_list: "list[int]",
    ) -> "list[dict]": ...

    def create_snoozed(self, task_id: int, fire_at_iso: str) -> dict: ...
    def get_reminder(self, reminder_id: int) -> "dict | None": ...
    def list_for_task(self, task_id: int) -> "list[dict]": ...

    def list_ready_to_fire(
        self,
        now_iso: "str | None" = None,
        limit: int = 200,
    ) -> "list[dict]": ...

    def count_pending_for_task(self, task_id: int) -> int: ...
    def mark_fired(self, reminder_id: int, fired_at: "str | None" = None) -> bool: ...
    def mark_missed(self, reminder_id: int) -> bool: ...
    def cancel_for_task(self, task_id: int) -> int: ...
