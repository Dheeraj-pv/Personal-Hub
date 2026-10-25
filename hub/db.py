"""Raw sqlite3 access. One connection per request; every sync is one transaction."""

from __future__ import annotations

import contextlib
import os
import sqlite3
import uuid
from collections.abc import Iterator
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    app TEXT NOT NULL,
    tbl TEXT NOT NULL,
    id TEXT NOT NULL,
    data_json TEXT NOT NULL,
    updated_at INTEGER NOT NULL,
    deleted INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    device_id TEXT NOT NULL,
    PRIMARY KEY (app, tbl, id)
);
CREATE INDEX IF NOT EXISTS records_app_seq ON records (app, seq);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT OR IGNORE INTO meta (key, value) VALUES ('seq', '0');
CREATE TABLE IF NOT EXISTS devices (
    device_id TEXT NOT NULL,
    app TEXT NOT NULL,
    first_seen INTEGER NOT NULL,
    last_seen INTEGER NOT NULL,
    last_cursor INTEGER NOT NULL,
    PRIMARY KEY (device_id, app)
);
CREATE TABLE IF NOT EXISTS sync_log (
    at INTEGER NOT NULL,
    app TEXT NOT NULL,
    device_id TEXT NOT NULL,
    pushed INTEGER NOT NULL,
    pulled INTEGER NOT NULL,
    ms INTEGER NOT NULL
);
"""


def connect(path: Path | str) -> sqlite3.Connection:
    # isolation_level=None: no implicit transactions; we issue BEGIN IMMEDIATE ourselves.
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.execute("PRAGMA busy_timeout=30000")
    # FULL: the phone marks rows synced as soon as the hub answers, so the hub must not
    # lose a commit it has acknowledged (NORMAL in WAL mode can drop it on power loss).
    conn.execute("PRAGMA synchronous=FULL")
    return conn


def init_db(path: Path) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    conn = connect(path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")  # persistent: stored in the file
        conn.executescript(SCHEMA)
        # Identifies this database; a wiped/replaced hub gets a new one, which clients notice.
        conn.execute(
            "INSERT OR IGNORE INTO meta (key, value) VALUES ('instance_id', ?)",
            (str(uuid.uuid4()),),
        )
    finally:
        conn.close()
    # Financial data: keep the file private to the owner (best effort).
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)


@contextlib.contextmanager
def open_db(path: Path | str) -> Iterator[sqlite3.Connection]:
    conn = connect(path)
    try:
        yield conn
    finally:
        conn.close()


@contextlib.contextmanager
def write_transaction(conn: sqlite3.Connection) -> Iterator[None]:
    """BEGIN IMMEDIATE takes the write lock up front, so concurrent syncs are
    serialised and the seq counter cannot be read-then-overwritten by two requests."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
