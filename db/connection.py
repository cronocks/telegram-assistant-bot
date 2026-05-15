import sqlite3
import threading
import config

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


def get_connection() -> sqlite3.Connection:
    """Return the module-level SQLite singleton, creating it on first call."""
    global _conn
    if _conn is None:
        with _lock:
            if _conn is None:
                _conn = _create_connection()
    return _conn


def _create_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(
        config.SQLITE_PATH,
        check_same_thread=False,
        detect_types=sqlite3.PARSE_DECLTYPES,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.commit()
    return conn
