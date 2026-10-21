from fastapi.testclient import TestClient

from mockingbird.app import create_app
from mockingbird.config import Settings


class FakeStore:
    """The only thing /healthz needs from a store: an async ping()."""

    def __init__(self) -> None:
        self.pings = 0

    async def ping(self) -> None:
        self.pings += 1


def test_healthz_ok(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_healthz_db_without_a_store_skips_the_check(client):
    r = client.get("/healthz?db=1")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_healthz_db_pings_the_store(settings: Settings):
    store = FakeStore()
    with TestClient(create_app(settings, store=store)) as c:
        r = c.get("/healthz?db=1")
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    assert store.pings == 1


def test_healthz_without_the_db_flag_never_touches_the_store(settings: Settings):
    store = FakeStore()
    with TestClient(create_app(settings, store=store)) as c:
        r = c.get("/healthz")
    assert r.json() == {"ok": True}
    assert store.pings == 0
