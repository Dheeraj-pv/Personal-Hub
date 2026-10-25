"""sync.py exercised directly, without HTTP."""

from hub.db import init_db, open_db
from hub.schemas import ChangeIn
from hub.sync import current_seq, run_sync


def c(id, ts, **kw):
    return ChangeIn(table="t", id=id, updated_at=ts, **kw)


def test_seq_rises_and_refused_row_is_returned(tmp_path):
    p = tmp_path / "h.db"
    init_db(p)
    with open_db(p) as conn:
        run_sync(conn, "notes", "A", 0, [c("1", 10, data={"x": 1})])
        out = run_sync(conn, "notes", "B", 0, [c("1", 5, data={"x": 2})])
        assert current_seq(conn) == 1  # refused change consumed no seq
    assert [r["data"] for r in out["changes"]][0] == {"x": 1}
    assert out["changes"][0]["seq"] == 1


def test_deleted_is_int_on_the_wire(tmp_path):
    p = tmp_path / "h.db"
    init_db(p)
    with open_db(p) as conn:
        run_sync(conn, "notes", "A", 0, [c("1", 10, deleted=True)])
        out = run_sync(conn, "notes", "B", 0, [])
    assert out["changes"][0]["deleted"] == 1
    assert type(out["changes"][0]["deleted"]) is int


def test_500_refused_leaves_empty_page_and_cursor_unchanged(tmp_path):
    p = tmp_path / "h.db"
    init_db(p)
    with open_db(p) as conn:
        run_sync(conn, "notes", "A", 0, [c(str(i), 100) for i in range(500)])
        out = run_sync(conn, "notes", "B", 0, [c(str(i), 1) for i in range(500)])
    assert len(out["changes"]) == 500  # exactly the refused rows, no page rows
    assert out["more"] is True
    assert out["cursor"] == 0


def test_far_future_updated_at_is_clamped_and_a_real_edit_can_win(tmp_path):
    import time

    p = tmp_path / "h.db"
    init_db(p)
    year_2100 = 4_102_444_800_000
    with open_db(p) as conn:
        run_sync(conn, "notes", "A", 0, [c("1", year_2100, data={"v": "bogus"})])
        stored = conn.execute("SELECT updated_at FROM records").fetchone()[0]
        now_ms = int(time.time() * 1000)
        assert stored <= now_ms + 24 * 3600 * 1000 + 5000
        # A real edit made two days from now (after the clamp) wins over the clamped row.
        run_sync(conn, "notes", "B", 0, [c("1", now_ms + 2 * 24 * 3600 * 1000, data={"v": "real"})])
        out = run_sync(conn, "notes", "C", 0, [])
    assert out["changes"][0]["data"] == {"v": "real"}


def test_instance_id_is_stable_and_in_every_response(tmp_path):
    p = tmp_path / "h.db"
    init_db(p)
    with open_db(p) as conn:
        a = run_sync(conn, "notes", "A", 0, [])["instance_id"]
        b = run_sync(conn, "expenses", "B", 5, [c("1", 1)])["instance_id"]
    init_db(p)  # restart keeps it
    with open_db(p) as conn:
        assert run_sync(conn, "notes", "A", 0, [])["instance_id"] == a == b


def test_new_database_gets_a_new_instance_id(tmp_path):
    ids = []
    for name in ("one.db", "two.db"):
        init_db(tmp_path / name)
        with open_db(tmp_path / name) as conn:
            ids.append(run_sync(conn, "notes", "A", 0, [])["instance_id"])
    assert ids[0] != ids[1] and len(ids[0]) == 36


def test_synchronous_is_full(tmp_path):
    init_db(tmp_path / "h.db")
    with open_db(tmp_path / "h.db") as conn:
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2
