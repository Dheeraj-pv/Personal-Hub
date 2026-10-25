"""Daily backup of the hub database: `python -m hub.backup`.

Uses SQLite's online backup API, so the copy is consistent even while the hub
is writing (a plain file copy could catch a half-written page). Reads only
HUB_DB and HUB_BACKUP_DIR, so it works without HUB_TOKEN.
"""

import os
import re
import sqlite3
import sys
from datetime import date
from pathlib import Path

KEEP = 14
_NAME = re.compile(r"^hub-\d{4}-\d{2}-\d{2}\.db$")


class BackupError(Exception):
    pass


def _paths() -> tuple[Path, Path]:
    home = Path.home()
    db = Path(os.environ.get("HUB_DB") or home / "PersonalHub" / "data" / "hub.db")
    out = Path(os.environ.get("HUB_BACKUP_DIR") or home / "PersonalHub" / "backups")
    return db.expanduser(), out.expanduser()


def run_backup(db_path: Path, backup_dir: Path, today: date | None = None) -> Path:
    if not db_path.is_file():
        raise BackupError(f"database not found: {db_path}")
    today = today or date.today()
    backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(backup_dir, 0o700)
    final = backup_dir / f"hub-{today.isoformat()}.db"
    tmp = backup_dir / f".hub-{today.isoformat()}.db.tmp"
    tmp.unlink(missing_ok=True)

    try:
        # Create the temp file with mode 600 before any data is written to it.
        os.close(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
        src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
        dst = sqlite3.connect(tmp)
        try:
            src.backup(dst)
            result = dst.execute("PRAGMA integrity_check").fetchone()
        finally:
            dst.close()
            src.close()
        if not result or result[0] != "ok":
            raise BackupError(f"integrity check failed on the copy: {result}")
        os.chmod(tmp, 0o600)
        os.replace(tmp, final)  # atomic: readers never see a partial file
    except sqlite3.Error as exc:
        raise BackupError(f"sqlite error while backing up: {exc}") from exc
    finally:
        tmp.unlink(missing_ok=True)

    _prune(backup_dir)
    return final


def _prune(backup_dir: Path) -> None:
    # Dated names sort chronologically. Only files matching our own pattern are touched.
    mine = sorted(p for p in backup_dir.iterdir() if p.is_file() and _NAME.match(p.name))
    for old in mine[:-KEEP]:
        old.unlink()


def main() -> int:
    db, out = _paths()
    try:
        final = run_backup(db, out)
    except (BackupError, OSError) as exc:
        print(f"backup failed: {exc}", file=sys.stderr)
        return 1
    print(f"backup written: {final}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
