"""Sync rules, mirroring MockHub (packages/hub_sync/lib/src/testing/mock_hub.dart)."""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any

from .db import write_transaction
from .schemas import ChangeIn

PAGE_SIZE = 500
LOG_KEEP = 1000
MAX_FUTURE_MS = 24 * 60 * 60 * 1000

_COLS = "tbl, id, data_json, updated_at, deleted, seq, device_id"


def _wire(row: tuple[Any, ...]) -> dict[str, Any]:
    tbl, id_, data_json, updated_at, deleted, seq, _device = row
    return {
        "table": tbl,
        "id": id_,
        "updated_at": updated_at,
        "deleted": deleted,
        "data": json.loads(data_json),
        "seq": seq,
    }


def current_seq(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT value FROM meta WHERE key='seq'").fetchone()[0])


def instance_id(conn: sqlite3.Connection) -> str:
    return str(conn.execute("SELECT value FROM meta WHERE key='instance_id'").fetchone()[0])


def accept(
    conn: sqlite3.Connection, app: str, c: ChangeIn, device_id: str, seq: int,
    now_ms: int | None = None,
) -> tuple[int, dict[str, Any] | None]:
    """Store `c` if it is not older than the stored row. Returns (seq, refused_row)."""
    # Clamp: a bogus far-future updated_at (bad clock/bug) would otherwise win every later edit.
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    updated_at = min(c.updated_at, now_ms + MAX_FUTURE_MS)
    existing = conn.execute(
        f"SELECT {_COLS} FROM records WHERE app=? AND tbl=? AND id=?", (app, c.table, c.id)
    ).fetchone()
    if existing is not None and updated_at < existing[3]:
        return seq, _wire(existing)
    seq += 1
    conn.execute(
        "INSERT INTO records (app, tbl, id, data_json, updated_at, deleted, seq, device_id) "
        "VALUES (?,?,?,?,?,?,?,?) "
        "ON CONFLICT(app, tbl, id) DO UPDATE SET data_json=excluded.data_json, "
        "updated_at=excluded.updated_at, deleted=excluded.deleted, seq=excluded.seq, "
        "device_id=excluded.device_id",
        (app, c.table, c.id, json.dumps(c.data, separators=(",", ":"), allow_nan=False),
         updated_at, c.deleted, seq, device_id),
    )
    return seq, None


def run_sync(
    conn: sqlite3.Connection, app: str, device_id: str, cursor: int, changes: list[ChangeIn]
) -> dict[str, Any]:
    started = time.monotonic()
    now_ms = int(time.time() * 1000)
    with write_transaction(conn):
        seq = current_seq(conn)
        refused: list[dict[str, Any]] = []
        for c in changes:
            seq, stored = accept(conn, app, c, device_id, seq, now_ms)
            if stored is not None:
                refused.append(stored)
        conn.execute("UPDATE meta SET value=? WHERE key='seq'", (str(seq),))

        limit = max(0, min(PAGE_SIZE, PAGE_SIZE - len(refused)))
        # One extra row tells us whether there is another page.
        rows = conn.execute(
            f"SELECT {_COLS} FROM records WHERE app=? AND seq>? ORDER BY seq LIMIT ?",
            (app, cursor, limit + 1),
        ).fetchall()
        page = rows[:limit]
        more = len(rows) > limit
        if more:
            # With limit 0 (500 refused) the page is empty: the cursor stays put.
            new_cursor = page[-1][5] if page else cursor
        else:
            new_cursor = rows[-1][5] if rows else cursor
        # Rows last written by this device are skipped, but the cursor still moves past them.
        out = refused + [_wire(r) for r in page if r[6] != device_id]
        # A client ahead of this hub (hub was reset) is told the hub's position.
        reported = seq if cursor > seq else max(new_cursor, cursor)

        conn.execute(
            "INSERT INTO devices (device_id, app, first_seen, last_seen, last_cursor) "
            "VALUES (?,?,?,?,?) ON CONFLICT(device_id, app) DO UPDATE SET "
            "last_seen=excluded.last_seen, last_cursor=excluded.last_cursor",
            (device_id, app, now_ms, now_ms, reported),
        )
        conn.execute(
            "INSERT INTO sync_log (at, app, device_id, pushed, pulled, ms) VALUES (?,?,?,?,?,?)",
            (now_ms, app, device_id, len(changes), len(out),
             int((time.monotonic() - started) * 1000)),
        )
        conn.execute(
            "DELETE FROM sync_log WHERE rowid <= (SELECT MAX(rowid) FROM sync_log) - ?",
            (LOG_KEEP,),
        )
    return {
        "cursor": reported,
        "changes": out,
        "more": more,
        "instance_id": instance_id(conn),
    }


def _iso(ms: int) -> str:
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def health_apps(conn: sqlite3.Connection) -> dict[str, Any]:
    # Like MockHub, only apps that hold rows are listed.
    out: dict[str, Any] = {}
    for app, count in conn.execute("SELECT app, COUNT(*) FROM records GROUP BY app ORDER BY app"):
        last = conn.execute("SELECT MAX(last_seen) FROM devices WHERE app=?", (app,)).fetchone()[0]
        devices = [
            r[0]
            for r in conn.execute(
                "SELECT device_id FROM devices WHERE app=? ORDER BY first_seen, device_id", (app,)
            )
        ]
        out[app] = {
            "last_sync_at": _iso(last) if last is not None else None,
            "row_count": count,
            "devices": devices,
        }
    return out
