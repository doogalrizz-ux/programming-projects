"""
Shared SQLite connection for HomeGhost.

Every skill (cog) — laundry today, shopping list / schedule / chess later —
talks to the same SQLite file through a single aiosqlite connection that
lives on the bot instance as `bot.db`. This module only owns the
connection itself; each cog is responsible for creating its own tables
(with `CREATE TABLE IF NOT EXISTS`) when it loads, so skills stay
self-contained instead of piling every table definition into one file.
"""
import logging

import aiosqlite

log = logging.getLogger("homeghost.database")


async def connect(db_path: str) -> aiosqlite.Connection:
    db = await aiosqlite.connect(db_path)
    db.row_factory = aiosqlite.Row
    # WAL mode so a long-running read (e.g. the status command) doesn't
    # block the reminder loop's writes, and vice versa.
    await db.execute("PRAGMA journal_mode=WAL;")
    await db.execute("PRAGMA foreign_keys=ON;")
    await db.commit()
    log.info("Connected to SQLite database at %s", db_path)
    return db
