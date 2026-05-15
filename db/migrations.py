import logging
import os
import re
import sqlite3

from db.connection import get_connection

logger = logging.getLogger(__name__)

_MIGRATIONS_DIR = os.path.join(os.path.dirname(__file__), "migrations")
_FILENAME_RE = re.compile(r"^(\d+)_.+\.sql$")


def run_migrations() -> None:
    """Apply all pending SQL migration files in numeric order. Idempotent."""
    conn = get_connection()
    _ensure_schema_version_table(conn)

    applied = _applied_versions(conn)
    pending = _pending_files(applied)

    if not pending:
        logger.info("DB migrations: already up to date (version %s)", max(applied, default=0))
        return

    for version, path in pending:
        _apply(conn, version, path)

    logger.info("DB migrations: applied up to version %s", max(v for v, _ in pending))


def _ensure_schema_version_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS _schema_version (
            version     INTEGER PRIMARY KEY,
            applied_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.commit()


def _applied_versions(conn: sqlite3.Connection) -> set[int]:
    rows = conn.execute("SELECT version FROM _schema_version").fetchall()
    return {row[0] for row in rows}


def _pending_files(applied: set[int]) -> list[tuple[int, str]]:
    """Return sorted list of (version, filepath) not yet applied."""
    result = []
    for filename in os.listdir(_MIGRATIONS_DIR):
        m = _FILENAME_RE.match(filename)
        if not m:
            continue
        version = int(m.group(1))
        if version not in applied:
            result.append((version, os.path.join(_MIGRATIONS_DIR, filename)))
    return sorted(result, key=lambda t: t[0])


def _apply(conn: sqlite3.Connection, version: int, path: str) -> None:
    sql = open(path, encoding="utf-8").read()
    logger.info("DB migrations: applying version %s (%s)", version, os.path.basename(path))
    with conn:
        conn.executescript(sql)
        conn.execute(
            "INSERT INTO _schema_version (version) VALUES (?)", (version,)
        )
    logger.info("DB migrations: version %s applied", version)
