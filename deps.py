"""deps.py — CoreDeps: dependency bundle shared across all command modules.

Extracted from core_handler.py so that cmd_*.py modules can import it without
creating circular imports (core_handler imports cmd_*, cmd_* imports CoreDeps).
"""
from __future__ import annotations

from dataclasses import dataclass

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
