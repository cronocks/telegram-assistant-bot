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


# ─── Note store (raw notes / journal) ────────────────────────────────────────

@runtime_checkable
class NoteStore(Protocol):
    """Abstract storage for raw notes and the daily journal.

    Concrete impl: DriveNoteStore (cloud) today; LocalFSNoteStore later.
    """

    def save_note(
        self, title: str, content: str, custom_filename: str | None = None
    ) -> str:
        """Create a new note file. Returns the resulting filename."""
        ...

    def search_notes(self, keyword: str, max_results: int = 5) -> list[dict]:
        """Full-text search. Returns [{name, modified, content}]."""
        ...

    def get_recent_notes(self, days: int = 7, max_results: int = 5) -> list[dict]:
        """Notes modified within the last N days. Legacy helper."""
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

    def add_to_daily_journal(self, content: str) -> tuple[str, str]:
        """Append (or create) today's journal entry. Returns (filename, action).

        action is "created" or "appended".
        """
        ...

    def get_today_journal(self) -> dict | None:
        """Return today's journal file content, or None if not created yet."""
        ...

    def smart_search(
        self, keywords: list[str], days_back: int = 0, max_per_keyword: int = 3
    ) -> list[dict]:
        """Multi-keyword search with an optional timeframe filter."""
        ...

    def get_current_week_notes(self, max_results: int = 20) -> list[dict]:
        """All notes modified during the current local week (Mon..Sun)."""
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

    def save_page(self, topic: str, content: str, file_id: str | None = None) -> str:
        """Create or overwrite a wiki page. Returns the filename."""
        ...

    def append_to_page(self, file_id: str, new_section: str) -> str:
        """Append a timestamped section to an existing wiki page."""
        ...

    def build_new_page(self, topic: str, topic_type: str, content_to_add: str) -> str:
        """Build the full markdown body (with frontmatter) for a new wiki page."""
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
        self, question: str, keywords: list[str]
    ) -> list[dict]:
        """Single retrieval entry point.

        Today: read index -> LLM picks filenames -> read pages.
        Future (vector DB): embed(question) -> top-k. Caller signature unchanged.

        Returns [{id, name, content}].
        """
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
    def bootstrap_admin(self) -> "User | None": ...
