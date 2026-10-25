import sqlite3
import threading

from fastapi.testclient import TestClient

from conftest import auth


def test_concurrent_syncs_never_duplicate_or_skip_seq(app, settings):
    n_threads, rounds, per = 4, 15, 5
    barrier = threading.Barrier(n_threads)
    errors = []

    def worker(k):
        c = TestClient(app, raise_server_exceptions=False)
        barrier.wait()
        for r in range(rounds):
            changes = [{"table": "t", "id": f"{k}-{r}-{i}", "updated_at": 1, "deleted": 0, "data": {}}
                       for i in range(per)]
            resp = c.post("/sync/expenses", headers=auth(),
                          json={"device_id": f"d{k}", "cursor": 0, "changes": changes})
            if resp.status_code != 200:
                errors.append(resp.status_code)

    ts = [threading.Thread(target=worker, args=(k,)) for k in range(n_threads)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert errors == []

    total = n_threads * rounds * per
    conn = sqlite3.connect(settings.db_path)
    seqs = sorted(r[0] for r in conn.execute("SELECT seq FROM records"))
    assert seqs == list(range(1, total + 1))
    assert int(conn.execute("SELECT value FROM meta WHERE key='seq'").fetchone()[0]) == total
    assert conn.execute("SELECT COUNT(*) FROM sync_log").fetchone()[0] == n_threads * rounds


def test_sync_log_is_trimmed_to_1000(client, settings):
    for i in range(1010):
        client.post("/sync/notes", headers=auth(), json={"device_id": "A", "cursor": 0, "changes": []})
    conn = sqlite3.connect(settings.db_path)
    assert conn.execute("SELECT COUNT(*) FROM sync_log").fetchone()[0] == 1000
