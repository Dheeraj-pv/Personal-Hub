import json
import os
import stat

import pytest
from conftest import TOKEN, auth, ch, sync
from fastapi.testclient import TestClient

from hub import notes_export
from hub.config import Settings
from hub.main import create_app

ID1 = "11111111-aaaa-bbbb-cccc-000000000001"


@pytest.fixture
def notes_dir(tmp_path):
    return tmp_path / "notes"


@pytest.fixture
def client(tmp_path, notes_dir):
    s = Settings(token=TOKEN, db_path=tmp_path / "data" / "hub.db", notes_dir=notes_dir)
    return TestClient(create_app(s), raise_server_exceptions=False)


def note(id=ID1, ts=1000, deleted=0, **data):
    base = {"id": id, "kind": "idea", "title": "Hello World", "body": "text", "checklist": [],
            "url": "", "tag": None, "pinned": False, "archived": False,
            "created_at_ms": 1_700_000_000_000}
    return ch(id, ts, table="notes", deleted=deleted, data={**base, **data})


def push(client, *changes, device="A"):
    return sync(client, device, changes=changes, app="notes")


def files(d):
    return sorted(p.name for p in d.iterdir() if p.is_file())


def test_idea_file_content_and_modes(client, notes_dir):
    push(client, note(tag="work", pinned=True))
    assert files(notes_dir) == ["hello-world-11111111.md"]
    p = notes_dir / "hello-world-11111111.md"
    assert p.read_text() == (
        "---\n"
        f'id: "{ID1}"\n'
        "kind: idea\n"
        'tag: "work"\n'
        "pinned: true\n"
        "archived: false\n"
        "created: 2023-11-14T22:13:20.000Z\n"
        "updated: 1970-01-01T00:00:01.000Z\n"
        "---\n\n# Hello World\n\ntext\n"
    )
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert stat.S_IMODE(notes_dir.stat().st_mode) == 0o700


def test_todo_and_link(client, notes_dir):
    push(client,
         note(id="22222222-x", kind="todo", title="Shop",
              checklist=[{"t": "milk", "d": True}, {"t": "eggs", "d": False}, "junk", {"d": 1}],
              body=""),
         note(id="33333333-x", kind="link", title="Site", url="https://e.com/a", body="why"))
    todo = (notes_dir / "shop-22222222.md").read_text()
    assert "# Shop\n\n- [x] milk\n- [ ] eggs\n" in todo
    link = (notes_dir / "site-33333333.md").read_text()
    assert "# Site\n\nhttps://e.com/a\n\nwhy\n" in link


def test_archived_is_exported(client, notes_dir):
    push(client, note(archived=True))
    assert "archived: true" in (notes_dir / "hello-world-11111111.md").read_text()


def test_title_change_renames(client, notes_dir):
    push(client, note(ts=1000))
    push(client, note(ts=2000, title="New Title"))
    assert files(notes_dir) == ["new-title-11111111.md"]


def test_tombstone_moves_to_trash_and_overwrites(client, notes_dir):
    push(client, note(ts=1000))
    push(client, note(ts=2000, deleted=1))
    assert files(notes_dir) == []
    assert files(notes_dir / ".trash") == ["hello-world-11111111.md"]
    # restore then delete again: same trash name is overwritten, no error
    push(client, note(ts=3000))
    push(client, note(ts=4000, deleted=1))
    assert files(notes_dir) == [] and files(notes_dir / ".trash") == ["hello-world-11111111.md"]


def test_tombstone_without_file_creates_nothing(client, notes_dir):
    push(client, note(deleted=1))
    assert files(notes_dir) == [] and not (notes_dir / ".trash").exists()


@pytest.mark.parametrize("title,expected_prefix", [
    ("../../etc/passwd", "etc-passwd-"),
    ("a/b", "a-b-"),
    ("\x00", "text-"),          # falls back to first body line
    ("---\nevil: yes", "evil-yes-"),
    ("x" * 500, "x" * 50 + "-"),
    ("\U0001F600\U0001F600", "text-"),
    ("", "text-"),
    (".hidden", "hidden-"),
    ("Café Über", "cafe-uber-"),
])
def test_hostile_titles(client, notes_dir, title, expected_prefix):
    push(client, note(title=title))
    (name,) = files(notes_dir)
    assert name.startswith(expected_prefix) and name.endswith("-11111111.md")
    assert "/" not in name and ".." not in name and not name.startswith(".")
    assert [p.name for p in notes_dir.iterdir() if p.name != name] == []


def test_untitled_when_no_title_or_body(client, notes_dir):
    push(client, note(title="", body=""))
    assert files(notes_dir) == ["untitled-11111111.md"]


def test_front_matter_cannot_be_injected(client, notes_dir):
    push(client, note(title="---\nevil: yes", tag='x"\nowned: true\n---\n', body="b"))
    text = (notes_dir / files(notes_dir)[0]).read_text()
    head = text.split("---\n")[1]
    keys = [ln.split(":")[0] for ln in head.splitlines()]
    assert keys == ["id", "kind", "tag", "pinned", "archived", "created", "updated"]
    assert text.count("\n---\n") == 1 or text.startswith("---\n")
    assert "\n# --- evil: yes\n" in text


def test_nul_and_control_chars_stripped(client, notes_dir):
    push(client, note(title="a\x00b\x07c", body="x\x00y"))
    text = (notes_dir / files(notes_dir)[0]).read_text()
    assert "\x00" not in text and "\x07" not in text and "# abc" in text


def test_malformed_payloads(client, notes_dir):
    bad = [
        ("aaaaaaaa-1", {}),
        ("bbbbbbbb-1", {"title": 5, "body": ["x"], "kind": {"a": 1}, "checklist": "no",
                       "tag": 7, "pinned": "yes", "created_at_ms": "soon", "url": None}),
        ("cccccccc-1", {"kind": "todo", "checklist": [None, 3, {"t": 5}, {"t": "ok", "d": "x"}]}),
        ("dddddddd-1", {"created_at_ms": 10**30, "title": "ok"}),
        ("eeeeeeee-1", {"title": "t", "created_at_ms": True}),
    ]
    for id_, data in bad:
        push(client, ch(id_, 1, table="notes", data=data))
    # a lone surrogate can only arrive as a JSON escape; the file must still be valid UTF-8
    raw = ('{"device_id":"A","cursor":0,"changes":[{"table":"notes","id":"ffffffff-1",'
           '"updated_at":1,"data":{"title":"lone\\ud800 x"}}]}')
    r = client.post("/sync/notes", headers=auth(), content=raw)
    assert r.status_code == 200
    assert (notes_dir / "lone-x-ffffffff.md").read_text().count("# lone x") == 1
    assert len(files(notes_dir)) == 6
    assert "- [ ] ok" in (notes_dir / "untitled-cccccccc.md").read_text()
    assert "created: null" in (notes_dir / "ok-dddddddd.md").read_text()


def test_odd_ids_do_not_escape(client, notes_dir):
    push(client, ch("../../x", 1, table="notes", data={"title": "t"}),
         ch("....", 1, table="notes", data={"title": "t"}))
    names = files(notes_dir)
    assert len(names) == 2 and all(n.startswith("t-") for n in names)
    assert not (notes_dir.parent / "x").exists()


def test_symlink_not_followed(client, notes_dir, tmp_path):
    outside = tmp_path / "outside.md"
    outside.write_text("keep")
    notes_dir.mkdir(mode=0o700)
    (notes_dir / "hello-world-11111111.md").symlink_to(outside)
    push(client, note())
    assert outside.read_text() == "keep"
    assert not (notes_dir / "hello-world-11111111.md").is_symlink()
    assert "# Hello World" in (notes_dir / "hello-world-11111111.md").read_text()


def test_symlinked_trash_is_refused(client, notes_dir, tmp_path):
    push(client, note(ts=1))
    target = tmp_path / "elsewhere"
    target.mkdir()
    (notes_dir / ".trash").symlink_to(target)
    r = client.post("/sync/notes", headers=auth(),
                    json={"device_id": "A", "cursor": 0, "changes": [note(ts=2, deleted=1)]})
    assert r.status_code == 200
    assert list(target.iterdir()) == []


def test_unchanged_rows_not_rewritten(client, notes_dir):
    push(client, note(), note(id="22222222-x", title="Other"))
    paths = [notes_dir / n for n in files(notes_dir)]
    before = {p: (p.stat().st_ino, p.stat().st_mtime_ns) for p in paths}
    push(client, note())                    # same row again
    push(client, note(ts=500), device="B")  # older edit is refused; stored row unchanged
    assert {p: (p.stat().st_ino, p.stat().st_mtime_ns) for p in paths} == before
    assert notes_export.export_all(client.app.state.settings.db_path, notes_dir) == (0, 0)


def test_full_reexport_rebuilds(client, notes_dir):
    push(client, note(), note(id="22222222-x", title="Gone"))
    push(client, note(id="22222222-x", ts=2000, deleted=1, title="Gone"))
    for p in notes_dir.rglob("*.md"):
        p.unlink()
    changed, failed = notes_export.export_all(client.app.state.settings.db_path, notes_dir)
    assert (changed, failed) == (1, 0)
    assert files(notes_dir) == ["hello-world-11111111.md"]
    assert notes_export.export_all(client.app.state.settings.db_path, notes_dir) == (0, 0)


def test_expenses_app_produces_no_files(client, notes_dir):
    sync(client, "A", changes=[note()], app="expenses")
    assert not notes_dir.exists()


def test_notes_table_other_than_notes_ignored(client, notes_dir):
    push(client, ch("x", 1, table="other", data={"title": "t"}))
    assert not notes_dir.exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_export_failure_does_not_fail_sync(client, notes_dir, caplog):
    notes_dir.mkdir(mode=0o700)
    notes_dir.chmod(0o500)
    try:
        r = client.post("/sync/notes", headers=auth(), json={
            "device_id": "A", "cursor": 0,
            "changes": [note(title="SECRET-TITLE")]})
        assert r.status_code == 200 and r.json()["cursor"] == 1
    finally:
        notes_dir.chmod(0o700)
    assert "SECRET-TITLE" not in caplog.text
    assert "notes export failed" in caplog.text
    # the data is in the DB, so a later full export recovers
    assert notes_export.export_all(client.app.state.settings.db_path, notes_dir) == (1, 0)


def test_export_unexpected_error_is_swallowed(client, monkeypatch, caplog):
    def boom(*a, **k):
        raise RuntimeError("SECRET-BODY")
    monkeypatch.setattr(notes_export, "_export", boom)
    r = client.post("/sync/notes", headers=auth(),
                    json={"device_id": "A", "cursor": 0, "changes": [note()]})
    assert r.status_code == 200
    assert "SECRET-BODY" not in caplog.text and "RuntimeError" in caplog.text


def test_no_notes_dir_setting_disables_export(tmp_path):
    s = Settings(token=TOKEN, db_path=tmp_path / "d" / "hub.db")
    c = TestClient(create_app(s))
    assert sync(c, "A", changes=[note()], app="notes")["cursor"] == 1


def test_default_notes_dir():
    from hub.config import load_settings
    s = load_settings({"HUB_TOKEN": TOKEN, "HOME": "/h"})
    assert s.notes_dir is not None and s.notes_dir.parts[-2:] == ("PersonalHub", "notes")
    s = load_settings({"HUB_TOKEN": TOKEN, "HUB_NOTES_DIR": "/x/n"})
    assert str(s.notes_dir) == "/x/n"


