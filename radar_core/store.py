"""SQLite base layer shared by every radar's store.

Apps own their schemas; the core owns the connection discipline (WAL, foreign
keys), the timestamp conventions, the `meta` key-value table helpers and
generic pruning. The `meta` table itself must exist in the app schema:

    CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from pathlib import Path

import aiosqlite

# Same format SQLite's datetime('now') produces; healthchecks parse with it.
TIMESTAMP_FMT = "%Y-%m-%d %H:%M:%S"


def utcnow_str() -> str:
    return datetime.now(UTC).strftime(TIMESTAMP_FMT)


def iso_date(d: date | None) -> str | None:
    """Single-sourced DB date encoding for nullable dates."""
    return d.isoformat() if d else None


@asynccontextmanager
async def open_db(db_path: str) -> AsyncIterator[aiosqlite.Connection]:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(db_path)
    try:
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA foreign_keys=ON")
        conn.row_factory = aiosqlite.Row
        yield conn
    finally:
        await conn.close()


class BaseStore:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def commit(self) -> None:
        await self._conn.commit()

    # -- meta -----------------------------------------------------------------

    async def set_meta(self, key: str, value: str) -> None:
        await self._conn.execute(
            """
            INSERT INTO meta (key, value) VALUES (?, ?)
            ON CONFLICT (key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )
        await self._conn.commit()

    async def get_meta(self, key: str) -> str | None:
        cur = await self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,))
        row = await cur.fetchone()
        return None if row is None else str(row["value"])

    # -- generic pruning -------------------------------------------------------

    async def prune_older_than(self, table: str, ts_column: str, max_age_days: int) -> int:
        """Delete rows older than max_age_days. table/ts_column are code-owned
        identifiers (never user input)."""
        cur = await self._conn.execute(
            f"DELETE FROM {table} "
            f"WHERE {ts_column} < datetime('now', '-{int(max_age_days)} days')"
        )
        await self._conn.commit()
        return cur.rowcount
