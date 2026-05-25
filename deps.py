"""deps.py — CoreDeps: dependency bundle shared across all command modules.

Extracted from core_handler.py so that cmd_*.py modules can import it without
creating circular imports (core_handler imports cmd_*, cmd_* imports CoreDeps).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from anniversary_engine import AnniversaryEngine
    from anniversary_store import SqliteAnniversaryStore
    from backup_engine import BackupEngine
    from interfaces import ReminderStore, TaskStore
    from reminder_engine import ReminderEngine
    from task_parser import TaskParser

from interfaces import (
    AuditLog,
    ChannelAdapter,
    ElevationStore,
    LLMClient,
    MemoryStore,
    NoteIndex,
    NoteStore,
    NotificationService,
    UserStore,
    WebSessionStore,
    WikiStore,
)


@dataclass
class CoreDeps:
    """Bundle of adapter instances injected by main.py into every command handler."""
    llm: LLMClient
    notes: NoteStore
    wiki: WikiStore
    channel: ChannelAdapter
    user_store: UserStore
    note_index: NoteIndex
    memory_store: MemoryStore
    elevation_store: ElevationStore
    audit: AuditLog
    notification_service: "NotificationService | None" = None
    web_session_store: "WebSessionStore | None" = None
    backup_engine: "BackupEngine | None" = None
    task_store: "TaskStore | None" = None
    reminder_store: "ReminderStore | None" = None
    reminder_engine: "ReminderEngine | None" = None
    task_parser: "TaskParser | None" = None
    anniversary_store: "SqliteAnniversaryStore | None" = None
    anniversary_engine: "AnniversaryEngine | None" = None
