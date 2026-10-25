import pytest
from fastapi.testclient import TestClient

from hub.config import Settings
from hub.main import create_app

TOKEN = "unit-test-token-" + "a1b2c3d4" * 4


@pytest.fixture
def settings(tmp_path):
    return Settings(token=TOKEN, db_path=tmp_path / "data" / "hub.db")


@pytest.fixture
def app(settings):
    return create_app(settings)


@pytest.fixture
def client(app):
    # raise_server_exceptions=False: we want to see the real 500 response a client would get.
    return TestClient(app, raise_server_exceptions=False)


def auth(token=TOKEN):
    return {"Authorization": f"Bearer {token}"}


def ch(id, ts, *, table="t", deleted=0, data=None):
    return {"table": table, "id": id, "updated_at": ts, "deleted": deleted,
            "data": {"v": ts} if data is None else data}


def sync(client, device, cursor=0, changes=(), app="expenses"):
    r = client.post(f"/sync/{app}", headers=auth(),
                    json={"device_id": device, "cursor": cursor, "changes": list(changes)})
    assert r.status_code == 200, r.text
    return r.json()
