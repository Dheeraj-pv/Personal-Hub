import logging

from conftest import TOKEN, auth, ch, sync


def test_health_without_or_with_wrong_token_is_status_only(client):
    sync(client, "A", changes=[ch("1", 1)])
    for headers in ({}, auth("wrong-token"), {"Authorization": TOKEN}):
        r = client.get("/health", headers=headers)
        assert r.status_code == 200 and r.json() == {"status": "ok"}


def test_health_with_token_has_details(client):
    sync(client, "A", changes=[ch("1", 1), ch("2", 2, deleted=1)], app="expenses")
    sync(client, "B", app="expenses")
    body = client.get("/health", headers=auth()).json()
    assert body["status"] == "ok" and body["hub_version"]
    assert isinstance(body["server_time"], int)
    e = body["apps"]["expenses"]
    assert e["row_count"] == 2 and e["devices"] == ["A", "B"]
    assert e["last_sync_at"].endswith("Z")
    assert "notes" not in body["apps"]


def test_401_on_missing_or_wrong_token(client):
    for headers in ({}, auth("nope"), {"Authorization": "Basic abc"}, {"Authorization": "Bearer "}):
        r = client.post("/sync/expenses", headers=headers, json={})
        assert r.status_code == 401 and r.json() == {"error": "unauthorized"}


def test_unknown_app_404_only_after_auth(client):
    assert client.post("/sync/other", json={}).status_code == 401
    r = client.post("/sync/other", headers=auth(), json={})
    assert r.status_code == 404


def test_413_by_content_length_header(client):
    big = b"{" + b" " * (2 * 1024 * 1024 + 10)
    assert client.post("/sync/notes", headers=auth(), content=big).status_code == 413


def test_413_when_streamed_without_content_length(client):
    def gen():
        for _ in range(3):
            yield b" " * (1024 * 1024)
    r = client.post("/sync/notes", headers=auth(), content=gen())
    assert r.status_code == 413


def test_413_when_content_length_lies(client):
    big = b" " * (2 * 1024 * 1024 + 10)
    r = client.post("/sync/notes", headers={**auth(), "content-length": "10"}, content=big)
    assert r.status_code == 413


def test_more_than_500_changes_is_400(client):
    r = client.post("/sync/notes", headers=auth(),
                    json={"device_id": "A", "cursor": 0, "changes": [ch(str(i), 1) for i in range(501)]})
    assert r.status_code == 400


def test_malformed_requests_are_400_and_do_not_echo_input(client):
    secret = "SENTINEL-INPUT"
    good = ch("1", 1)
    bodies = [
        b"not json", b"[]", b'{"device_id":"A","cursor":0,"changes":{}}',
        b'{"device_id":"","cursor":0,"changes":[]}',
        b'{"device_id":"A","cursor":-1,"changes":[]}',
        b'{"device_id":"A","cursor":"0","changes":[]}',
        b'{"device_id":"A","cursor":NaN,"changes":[]}',
        ('{"device_id":"%s","cursor":0,"changes":[{"table":"t","id":"","updated_at":1,"data":{}}]}' % secret).encode(),
        ('{"device_id":"%s","cursor":0,"changes":[{"table":"t","id":"x","updated_at":-1,"data":{}}]}' % secret).encode(),
        ('{"device_id":"%s","cursor":0,"changes":[{"table":"t","id":"x","updated_at":1.5,"data":{}}]}' % secret).encode(),
        ('{"device_id":"%s","cursor":0,"changes":[{"table":"t","id":"x","updated_at":1,"data":[1]}]}' % secret).encode(),
        ('{"device_id":"%s","cursor":0,"changes":[{"table":"t","id":"x","updated_at":1,"deleted":2,"data":{}}]}' % secret).encode(),
        ('{"device_id":"%s","cursor":0,"changes":[{"table":"t","id":"%s","updated_at":1,"data":{}}]}' % (secret, "i" * 300)).encode(),
        b"[" * 100000,
    ]
    for b in bodies:
        r = client.post("/sync/notes", headers=auth(), content=b)
        assert r.status_code == 400, (b[:60], r.text)
        assert secret not in r.text
    assert good  # silence linters; valid shape covered elsewhere


def test_non_finite_numbers_are_400_and_nothing_stored(client):
    def body(literal):
        return ('{"device_id":"A","cursor":0,"changes":[{"table":"t","id":"x","updated_at":1,'
                '"data":{"v":[%s]}}]}' % literal).encode()
    for lit in ("NaN", "Infinity", "-Infinity", "1e999", "-1e999"):
        r = client.post("/sync/notes", headers=auth(), content=body(lit))
        assert r.status_code == 400 and lit not in r.text, lit
    pull = client.post("/sync/notes", headers=auth(),
                       json={"device_id": "B", "cursor": 0, "changes": []})
    assert pull.status_code == 200 and pull.json()["changes"] == []


def test_instance_id_in_sync_and_authenticated_health_only(client):
    first = sync(client, "A")["instance_id"]
    assert sync(client, "B", changes=[ch("1", 1)])["instance_id"] == first
    assert client.get("/health", headers=auth()).json()["instance_id"] == first
    assert "instance_id" not in client.get("/health").json()


def test_unexpected_error_is_500_generic(client, monkeypatch, caplog):
    import hub.main as m

    def boom(*a, **k):
        raise RuntimeError(f"explode {TOKEN}")
    monkeypatch.setattr(m, "run_sync", boom)
    r = client.post("/sync/notes", headers=auth(), json={"device_id": "A", "cursor": 0, "changes": []})
    assert r.status_code == 500 and r.json() == {"error": "internal error"}
    assert TOKEN not in r.text


def test_token_never_in_responses_or_logs(client, caplog):
    caplog.set_level(logging.DEBUG)
    texts = []
    texts.append(client.post("/sync/notes", headers=auth("wrong-" + TOKEN[:5]), json={}).text)
    texts.append(client.post("/sync/zzz", headers=auth(), json={}).text)
    texts.append(client.post("/sync/notes", headers=auth(), content=b"nope").text)
    texts.append(client.get("/health", headers=auth("bad")).text)
    texts.append(client.get("/health", headers=auth()).text)
    texts.append(client.get("/nothing", headers=auth()).text)
    for t in texts:
        assert TOKEN not in t
    assert TOKEN not in caplog.text
    assert "wrong-" not in caplog.text
