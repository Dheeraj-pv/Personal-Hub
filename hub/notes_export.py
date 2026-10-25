"""Markdown export of the `notes` app: one file per note, rebuilt from the records table.

Rows are untrusted input (any device holding the token can push anything), so everything that
reaches the file system or the file content is sanitised here. Export is derived data: it can
always be rebuilt with `python -m hub.notes_export`, so failures are logged and never raised.
Log lines carry only a fixed message and an exception type name, never note content.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import sqlite3
import sys
import threading
import unicodedata
from collections.abc import Iterable, Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import notes_dir_from_env
from .db import open_db

log = logging.getLogger("hub")

APP = "notes"
TABLE = "notes"
TRASH = ".trash"
MAX_SLUG = 50
MAX_TEXT = 200_000
MAX_LINE = 2_000
MAX_ITEMS = 500
KINDS = frozenset({"idea", "todo", "link"})
_BATCH = 500
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
# One export at a time: two overlapping syncs touching the same note must not interleave
# their rename/replace steps.
_lock = threading.Lock()


# ---- sanitising -------------------------------------------------------------------------


def _clean(value: Any, *, multiline: bool = False, limit: int = MAX_LINE) -> str:
    """Return text with control chars, lone surrogates and line/paragraph separators removed.

    Single-line mode turns every line break into a space; multiline keeps \\n only."""
    if not isinstance(value, str):
        return ""
    value = value[: MAX_TEXT if multiline else limit * 4]
    out: list[str] = []
    for ch in value:
        if ch in "\r\n  \x85":
            out.append("\n" if multiline and ch in "\r\n" else " ")
        elif ch == "\t":
            out.append(" " if not multiline else "\t")
        elif unicodedata.category(ch) in ("Cc", "Cs", "Zl", "Zp"):
            continue
        else:
            out.append(ch)
    text = "".join(out)
    if multiline:
        return re.sub(r"\n{3,}", "\n\n", text.replace("\r", "")).strip()
    return text.strip()[:limit]


def _yaml_str(value: str) -> str:
    # A JSON string is a valid YAML double-quoted scalar; _clean already removed anything
    # YAML treats as a line break, so the value cannot end its line or open a new key.
    return json.dumps(value, ensure_ascii=False)


def slugify(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", folded.lower()).strip("-")
    return slug[:MAX_SLUG].strip("-")


def short_id(note_id: str) -> str:
    """First 8 characters of the id, reduced to [a-z0-9] so it is safe inside a file name."""
    cleaned = re.sub(r"[^a-z0-9]", "", note_id.lower())[:8]
    if cleaned:
        return cleaned
    return hashlib.sha1(note_id.encode("utf-8", "replace")).hexdigest()[:8]


def _first_body_line(body: str) -> str:
    for line in body.splitlines():
        if line.strip():
            return line
    return ""


def file_name(note_id: str, data: dict[str, Any]) -> str:
    title = _clean(data.get("title"))
    body = _clean(data.get("body"), multiline=True)
    slug = slugify(title) or slugify(_first_body_line(body)) or "untitled"
    return f"{slug}-{short_id(note_id)}.md"


def _iso(ms: Any) -> str | None:
    if isinstance(ms, bool) or not isinstance(ms, (int, float)):
        return None
    try:
        dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def render(note_id: str, data: dict[str, Any], updated_at: int) -> str:
    kind = data.get("kind")
    kind = kind if isinstance(kind, str) and kind in KINDS else "idea"
    tag = _clean(data.get("tag"))
    title = _clean(data.get("title")) or "Untitled"
    body = _clean(data.get("body"), multiline=True)
    url = _clean(data.get("url"))
    created = _iso(data.get("created_at_ms"))
    updated = _iso(updated_at)
    head = [
        "---",
        f"id: {_yaml_str(_clean(note_id))}",
        f"kind: {kind}",
        f"tag: {_yaml_str(tag) if tag else 'null'}",
        f"pinned: {'true' if data.get('pinned') is True else 'false'}",
        f"archived: {'true' if data.get('archived') is True else 'false'}",
        f"created: {created or 'null'}",
        f"updated: {updated or 'null'}",
        "---",
    ]
    parts = ["\n".join(head), f"# {title}"]
    if kind == "link" and url:
        parts.append(url)
    if kind == "todo":
        items = data.get("checklist")
        lines: list[str] = []
        for item in (items if isinstance(items, list) else [])[:MAX_ITEMS]:
            if not isinstance(item, dict):
                continue
            text = _clean(item.get("t"))
            if text:
                lines.append(f"- [{'x' if item.get('d') is True else ' '}] {text}")
        if lines:
            parts.append("\n".join(lines))
    if body:
        parts.append(body)
    return "\n\n".join(parts) + "\n"


# ---- file system (all operations are relative to an open directory fd) ---------------------


def _open_root(notes_dir: Path) -> int:
    root = notes_dir.expanduser()
    created = not root.exists()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(root.resolve(), _DIR_FLAGS & ~os.O_NOFOLLOW)  # the configured dir is trusted
    if created:
        os.fchmod(fd, 0o700)
    return fd


def _open_trash(root_fd: int) -> int:
    try:
        os.mkdir(TRASH, 0o700, dir_fd=root_fd)
    except FileExistsError:
        pass
    return os.open(TRASH, _DIR_FLAGS, dir_fd=root_fd)  # O_NOFOLLOW: a symlinked .trash fails


def _safe_name(name: str) -> bool:
    return (
        bool(name)
        and name == os.path.basename(name)
        and "/" not in name
        and "\x00" not in name
        and not name.startswith(".")
        and ".." not in name
        and name.endswith(".md")
    )


def _existing(root_fd: int, short: str) -> list[str]:
    suffix = f"-{short}.md"
    with os.scandir(root_fd) as it:
        return sorted(
            e.name
            for e in it
            if e.name.endswith(suffix) and e.is_file(follow_symlinks=False)
        )


def _read(root_fd: int, name: str) -> bytes | None:
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=root_fd)
    except OSError:
        return None
    with os.fdopen(fd, "rb") as fh:
        return fh.read(MAX_TEXT * 8)


def _write_atomic(root_fd: int, name: str, content: bytes) -> None:
    tmp = f".tmp-{secrets.token_hex(8)}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=root_fd)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(content)
        os.replace(tmp, name, src_dir_fd=root_fd, dst_dir_fd=root_fd)
    except BaseException:
        try:
            os.unlink(tmp, dir_fd=root_fd)
        except OSError:
            pass
        raise


def _export_one(root_fd: int, note_id: str, data: Any, updated_at: int, deleted: bool) -> bool:
    """Apply one record to the folder. Returns True when something on disk changed."""
    data = data if isinstance(data, dict) else {}
    short = short_id(note_id)
    existing = _existing(root_fd, short)
    changed = False
    if deleted:
        if existing:
            trash_fd = _open_trash(root_fd)
            try:
                for name in existing:
                    os.replace(name, name, src_dir_fd=root_fd, dst_dir_fd=trash_fd)
                    changed = True
            finally:
                os.close(trash_fd)
        return changed
    name = file_name(note_id, data)
    if not _safe_name(name):  # cannot happen by construction; belt and braces
        raise ValueError("unsafe file name")
    content = render(note_id, data, updated_at).encode("utf-8")
    if not (name in existing and _read(root_fd, name) == content):
        _write_atomic(root_fd, name, content)
        changed = True
    for old in existing:
        if old != name:  # the title changed: the old file is superseded
            os.unlink(old, dir_fd=root_fd)
            changed = True
    return changed


# ---- public API -----------------------------------------------------------------------------


def _rows(conn: sqlite3.Connection, ids: Iterable[str] | None) -> Iterator[tuple[Any, ...]]:
    sql = "SELECT id, data_json, updated_at, deleted FROM records WHERE app=? AND tbl=?"
    if ids is None:
        yield from conn.execute(sql + " ORDER BY seq", (APP, TABLE))
        return
    unique = list(dict.fromkeys(ids))
    for i in range(0, len(unique), _BATCH):
        chunk = unique[i : i + _BATCH]
        marks = ",".join("?" * len(chunk))
        yield from conn.execute(sql + f" AND id IN ({marks})", (APP, TABLE, *chunk))


def _export(db_path: Path, notes_dir: Path, ids: Iterable[str] | None) -> tuple[int, int]:
    """Returns (files changed, notes that failed)."""
    changed = failed = 0
    with _lock:
        root_fd = _open_root(notes_dir)
        try:
            with open_db(db_path) as conn:
                for note_id, data_json, updated_at, deleted in _rows(conn, ids):
                    try:
                        data = json.loads(data_json)
                        changed += _export_one(root_fd, note_id, data, updated_at, bool(deleted))
                    except Exception as exc:  # one bad row must not stop the others
                        failed += 1
                        log.error("notes export failed for a note: %s", type(exc).__name__)
        finally:
            os.close(root_fd)
    return changed, failed


def export_notes(db_path: Path, notes_dir: Path, ids: Iterable[str]) -> None:
    """Export the given note ids from the DB. Never raises (runs after the sync response)."""
    try:
        _export(db_path, notes_dir, list(ids))
    except Exception as exc:
        log.error("notes export failed: %s", type(exc).__name__)


def export_all(db_path: Path, notes_dir: Path) -> tuple[int, int]:
    """Idempotent full rebuild from the DB. Returns (files changed, notes that failed)."""
    return _export(db_path, notes_dir, None)


def main() -> int:
    from .config import db_path_from_env

    changed, failed = export_all(db_path_from_env(), notes_dir_from_env())
    print(f"notes export: {changed} file(s) changed, {failed} note(s) failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
