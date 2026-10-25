"""Contract tests: the behaviours the Dart client depends on (see MockHub)."""

from conftest import ch, sync


def by_id(resp):
    return {c["id"]: c for c in resp["changes"]}


def test_wire_shape(client):
    sync(client, "A", changes=[ch("1", 5, data={"n": "x"})])
    r = sync(client, "B")
    assert r["more"] is False and r["cursor"] == 1
    assert r["changes"] == [{"table": "t", "id": "1", "updated_at": 5, "deleted": 0,
                             "data": {"n": "x"}, "seq": 1}]


def test_newer_wins_older_refused_and_returned_tie_keeps_incoming(client):
    sync(client, "A", changes=[ch("1", 100, data={"who": "A"})])
    # Older from B: refused, stored row returned regardless of cursor.
    r = sync(client, "B", cursor=1, changes=[ch("1", 50, data={"who": "B"})])
    assert by_id(r)["1"]["data"] == {"who": "A"} and by_id(r)["1"]["seq"] == 1
    assert sync(client, "C")["changes"][0]["data"] == {"who": "A"}
    # Newer from B wins.
    sync(client, "B", changes=[ch("1", 200, data={"who": "B"})])
    assert sync(client, "C")["changes"][0]["data"] == {"who": "B"}
    # Tie: incoming kept, new seq, owner changes to the requester.
    r = sync(client, "A", cursor=0, changes=[ch("1", 200, data={"who": "A2"})])
    assert r["changes"] == []  # A wrote it last: not echoed
    c = sync(client, "C")["changes"][0]
    assert c["data"] == {"who": "A2"} and c["seq"] == 3


def test_tombstones_sync(client):
    sync(client, "A", changes=[ch("1", 1)])
    sync(client, "A", changes=[ch("1", 2, deleted=1, data={})])
    r = sync(client, "B")
    assert r["changes"][0]["deleted"] == 1 and r["changes"][0]["updated_at"] == 2


def test_pull_paging_1250_rows_in_three_pages(client):
    for start in (0, 500, 1000):
        n = min(500, 1250 - start)
        sync(client, "A", changes=[ch(str(start + i), 1) for i in range(n)])
    cursor, pages, total = 0, [], 0
    while True:
        r = sync(client, "B", cursor=cursor)
        pages.append((len(r["changes"]), r["more"], r["cursor"]))
        total += len(r["changes"])
        assert r["cursor"] > cursor or not r["more"]
        cursor = r["cursor"]
        if not r["more"]:
            break
    assert pages == [(500, True, 500), (500, True, 1000), (250, False, 1250)]
    assert total == 1250


def test_echo_suppressed_but_cursor_advances(client):
    r = sync(client, "A", changes=[ch("1", 1), ch("2", 1), ch("3", 1)])
    assert r["changes"] == [] and r["cursor"] == 3
    r = sync(client, "B", changes=[ch("4", 1)])
    assert [c["id"] for c in r["changes"]] == ["1", "2", "3"] and r["cursor"] == 4
    r = sync(client, "A", cursor=3)
    assert [c["id"] for c in r["changes"]] == ["4"] and r["cursor"] == 4


def test_identical_batch_twice_is_harmless(client):
    batch = [ch("1", 5), ch("2", 6, deleted=1)]
    sync(client, "A", changes=batch)
    sync(client, "A", changes=batch)
    r = sync(client, "B")
    assert {c["id"] for c in r["changes"]} == {"1", "2"} and len(r["changes"]) == 2


def test_dropped_response_retry_reaches_same_final_state(client):
    batch = [ch("1", 5, data={"a": 1}), ch("2", 6)]
    sync(client, "A", cursor=0, changes=batch)          # processed, response "lost"
    retry = sync(client, "A", cursor=0, changes=batch)  # client retries from old cursor
    assert retry["changes"] == []  # own rows are not echoed
    final = sync(client, "B")
    assert {c["id"]: c["data"] for c in final["changes"]} == {"1": {"a": 1}, "2": {"v": 6}}


def test_hub_reset_reports_hub_position(client):
    r = sync(client, "A", cursor=999)
    assert r["cursor"] == 0 and r["changes"] == []
    sync(client, "B", changes=[ch("1", 1), ch("2", 1), ch("3", 1)])
    assert sync(client, "A", cursor=10)["cursor"] == 3


def test_two_devices_interleaved_edits_and_deletes_converge(client):
    ca = cb = 0
    state = {"A": {}, "B": {}}

    def go(dev, changes):
        nonlocal ca, cb
        cur = ca if dev == "A" else cb
        while True:
            r = sync(client, dev, cursor=cur, changes=changes)
            changes = []
            for c in r["changes"]:
                if c["id"] not in state[dev] or c["updated_at"] >= state[dev][c["id"]][0]:
                    state[dev][c["id"]] = (c["updated_at"], c["deleted"], c["data"])
            cur = r["cursor"]
            if not r["more"]:
                break
        if dev == "A":
            ca = cur
        else:
            cb = cur

    def local(dev, id, ts, deleted=0):
        state[dev][id] = (ts, deleted, {"by": dev, "ts": ts})
        return ch(id, ts, deleted=deleted, data={"by": dev, "ts": ts})

    go("A", [local("A", "x", 10), local("A", "y", 10)])
    go("B", [])
    go("B", [local("B", "x", 20), local("B", "z", 20)])
    go("A", [local("A", "y", 30, deleted=1), local("A", "x", 15)])  # x@15 loses to B's x@20
    go("B", [local("B", "z", 40, deleted=1)])
    go("A", [])
    go("B", [])
    go("A", [])
    assert state["A"] == state["B"]
    assert state["A"]["x"][2]["by"] == "B" and state["A"]["y"][1] == 1 and state["A"]["z"][1] == 1


def test_500_in_one_request_ok_and_600_split_ok(client):
    r = sync(client, "A", changes=[ch(str(i), 1) for i in range(500)])
    assert r["cursor"] == 500
    sync(client, "A", changes=[ch(f"b{i}" , 1) for i in range(300)])
    sync(client, "A", changes=[ch(f"c{i}", 1) for i in range(300)])
    cursor, seen = 0, 0
    while True:
        r = sync(client, "B", cursor=cursor)
        seen += len(r["changes"])
        cursor = r["cursor"]
        if not r["more"]:
            break
    assert seen == 1100


def test_apps_are_isolated(client):
    sync(client, "A", changes=[ch("1", 1)], app="expenses")
    assert sync(client, "B", app="notes")["changes"] == []
