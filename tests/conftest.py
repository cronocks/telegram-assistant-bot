"""conftest.py — shared pytest fixtures."""
import sqlite3

import pytest

from db.migrations import run_migrations
from memory_store import SqliteMemoryStore
from note_index import SqliteNoteIndex
from user_store import SqliteUserStore


@pytest.fixture()
def db_conn():
    """In-memory SQLite connection with all migrations applied."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")

    # Patch get_connection so migration runner uses this in-memory conn.
    import db.connection as db_mod
    original = db_mod._conn
    db_mod._conn = conn
    run_migrations()
    yield conn
    db_mod._conn = original
    conn.close()


@pytest.fixture()
def store(db_conn):
    """SqliteUserStore wired to the in-memory connection."""
    return SqliteUserStore(conn=db_conn)


@pytest.fixture()
def sample_admin(store):
    """A pre-created admin user."""
    return store.create_user(name="Admin User", role="admin")


@pytest.fixture()
def member_user(store):
    """A pre-created member user."""
    return store.create_user(name="Member User", role="member")


@pytest.fixture()
def another_user(store):
    """A second member user — used to test cross-user access."""
    return store.create_user(name="Another User", role="member")


@pytest.fixture()
def note_index(db_conn):
    """SqliteNoteIndex wired to the in-memory connection."""
    return SqliteNoteIndex(conn=db_conn)


@pytest.fixture()
def memory_store(db_conn):
    """SqliteMemoryStore wired to the in-memory connection."""
    return SqliteMemoryStore(conn=db_conn)
