import os
import sqlite3
import stat
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

from hub import backup


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "data" / "hub.db"
    path.parent.mkdir()
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    con.executemany("INSERT INTO t (v) VALUES (?)", [(f"row{i}",) for i in range(50)])
    con.commit()
    con.close()
    return path


def rows(path):
    con = sqlite3.connect(path)
    try:
        return con.execute("SELECT id, v FROM t ORDER BY id").fetchall()
    finally:
        con.close()


def test_copy_is_valid_and_has_same_rows(db, tmp_path):
    out = backup.run_backup(db, tmp_path / "bk", date(2026, 1, 5))
    assert out.name == "hub-2026-01-05.db"
    assert rows(out) == rows(db)
    con = sqlite3.connect(out)
    assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    con.close()


def test_includes_uncheckpointed_wal_data(db, tmp_path):
    # A writer holds recent rows only in the -wal file; a plain file copy would miss them.
    writer = sqlite3.connect(db)
    writer.execute("INSERT INTO t (v) VALUES ('fresh')")
    writer.commit()
    out = backup.run_backup(db, tmp_path / "bk", date(2026, 1, 5))
    writer.close()
    assert ("fresh" in [v for _, v in rows(out)])


def test_permissions(db, tmp_path):
    out = backup.run_backup(db, tmp_path / "bk", date(2026, 1, 5))
    assert stat.S_IMODE(os.stat(out).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(out.parent).st_mode) == 0o700


def test_no_temp_file_left_behind(db, tmp_path):
    out = backup.run_backup(db, tmp_path / "bk", date(2026, 1, 5))
    assert [p.name for p in out.parent.iterdir()] == [out.name]


def test_same_day_rerun_is_idempotent(db, tmp_path):
    bk = tmp_path / "bk"
    backup.run_backup(db, bk, date(2026, 1, 5))
    con = sqlite3.connect(db)
    con.execute("INSERT INTO t (v) VALUES ('later')")
    con.commit()
    con.close()
    out = backup.run_backup(db, bk, date(2026, 1, 5))
    assert len(list(bk.iterdir())) == 1
    assert rows(out) == rows(db)


def test_retention_keeps_newest_14_and_only_matching_files(db, tmp_path):
    bk = tmp_path / "bk"
    bk.mkdir()
    start = date(2026, 1, 1)
    for i in range(20):
        (bk / f"hub-{start + timedelta(days=i)}.db").write_bytes(b"x")
    keepers = ["notes.txt", "hub-latest.db", "hub-2026-01-01.db.bak", "other-2026-01-01.db"]
    for name in keepers:
        (bk / name).write_text("mine")
    backup.run_backup(db, bk, start + timedelta(days=20))
    names = sorted(p.name for p in bk.iterdir())
    dated = [n for n in names if backup._NAME.match(n)]
    assert len(dated) == 14
    assert dated[0] == "hub-2026-01-08.db" and dated[-1] == "hub-2026-01-21.db"
    for name in keepers:
        assert name in names


def test_missing_db_gives_clear_error(tmp_path):
    with pytest.raises(backup.BackupError, match="database not found"):
        backup.run_backup(tmp_path / "nope.db", tmp_path / "bk")
    assert not (tmp_path / "bk").exists()


def test_corrupt_db_fails_and_leaves_no_file(tmp_path):
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"this is not a sqlite database" * 100)
    with pytest.raises(backup.BackupError):
        backup.run_backup(bad, tmp_path / "bk", date(2026, 1, 5))
    assert list((tmp_path / "bk").iterdir()) == []


def test_cli_success_and_failure_without_token(db, tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "HUB_TOKEN"}
    env.update(HUB_DB=str(db), HUB_BACKUP_DIR=str(tmp_path / "bk"))
    ok = subprocess.run([sys.executable, "-m", "hub.backup"], env=env, capture_output=True, text=True)
    assert ok.returncode == 0, ok.stderr
    assert list((tmp_path / "bk").glob("hub-*.db"))

    env["HUB_DB"] = str(tmp_path / "missing.db")
    env["HUB_TOKEN"] = "secret-token-that-must-never-be-printed-123456"
    bad = subprocess.run([sys.executable, "-m", "hub.backup"], env=env, capture_output=True, text=True)
    assert bad.returncode == 1
    assert "database not found" in bad.stderr
    assert env["HUB_TOKEN"] not in bad.stdout + bad.stderr
