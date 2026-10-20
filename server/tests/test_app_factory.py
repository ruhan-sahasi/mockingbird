import asyncio
from types import SimpleNamespace

import anthropic
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mockingbird import app as app_module
from mockingbird.app import app, create_app
from mockingbird.claude import AnthropicStreamer
from mockingbird.config import get_settings
from mockingbird.report import Grader
from mockingbird.store import MemoryStore, PostgresStore
from mockingbird.vapi_client import VapiClient

REQUIRED = {
    "ANTHROPIC_API_KEY": "a-key",
    "VAPI_API_KEY": "v-key",
    "PUBLIC_BASE_URL": "https://mockingbird.test",
    "BRAIN_SECRET": "brain",
    "WEBHOOK_SECRET": "hook",
    "OWNER_KEY": "owner-from-env",
    "VISITOR_SALT": "salt",
}
OPTIONAL = ["DATABASE_URL", "DAILY_DEMO_MINUTES", "DEMO_CAP_S", "FULL_CAP_S"]


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    """The seven required settings in the environment, an empty working directory so no
    real .env is read, and a fresh get_settings() cache before and after each test."""
    for name in OPTIONAL:
        monkeypatch.delenv(name, raising=False)
    for name, value in REQUIRED.items():
        monkeypatch.setenv(name, value)
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_app_is_a_zero_argument_factory_that_builds_the_app_from_the_environment():
    # `uvicorn mockingbird.app:app --factory` (Task 14's Dockerfile) calls app() with no
    # arguments. If app becomes a module-level instance, this call fails.
    a = app()
    assert isinstance(a, FastAPI)
    assert a.state.settings.owner_key == "owner-from-env"
    with TestClient(a) as c:
        assert c.get("/healthz").json() == {"ok": True}


def test_app_reads_the_settings_when_called_not_when_imported(monkeypatch):
    monkeypatch.setenv("DEMO_CAP_S", "42")
    assert app().state.settings.demo_cap_s == 42


def test_app_uses_the_cached_get_settings():
    assert app().state.settings is get_settings()
    assert get_settings() is get_settings()


# --- the real dependencies create_app builds ------------------------------------------------


class FakePool:
    """Stands in for the asyncpg pool. Nothing in these tests queries it."""

    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def idle_sweeper(monkeypatch):
    """The sweeper as a stand-in that records the store it was given and then waits, so these
    tests see what the app built without a sweep running against it."""
    seen: list = []

    async def run_sweeper(registry, store) -> None:
        seen.append(store)
        await asyncio.Event().wait()

    monkeypatch.setattr(app_module, "run_sweeper", run_sweeper)
    return seen


def test_create_app_builds_the_grader_its_own_slow_client_and_one_live_client(settings):
    with TestClient(create_app(settings)) as c:
        state = c.app.state
        assert isinstance(state.grader, Grader)
        grading = state.grader._client
        # Grading with adaptive thinking takes a while, and nobody is waiting on the line.
        assert grading.timeout == anthropic.Timeout(120.0, connect=5.0)
        assert grading.max_retries == 2
        assert grading.api_key == settings.anthropic_api_key

        one, two = state.streamer_factory(), state.streamer_factory()
        assert isinstance(one, AnthropicStreamer)
        assert one is not two  # one streamer per interview
        live = one._client
        assert two._client is live  # over one client, which keeps one connection pool
        assert live.timeout == anthropic.Timeout(10.0, connect=2.0)
        assert live.max_retries == 1
        assert grading is not live

        assert isinstance(state.vapi, VapiClient)
        vapi_http = state.vapi._http
        assert not vapi_http.is_closed

    # Shutting down closes what was built.
    assert grading.is_closed()
    assert live.is_closed()
    assert vapi_http.is_closed


def test_without_a_database_url_the_store_is_in_memory_with_a_warning(settings, caplog):
    with TestClient(create_app(settings)) as c:
        assert isinstance(c.app.state.store, MemoryStore)
    assert "DATABASE_URL is not set" in caplog.text


def test_with_a_database_url_the_store_is_postgres_and_its_pool_closes_on_shutdown(
    settings, monkeypatch, idle_sweeper
):
    pool = FakePool()
    dsns: list[str] = []

    async def create_pool(dsn: str) -> FakePool:
        dsns.append(dsn)
        return pool

    monkeypatch.setattr(app_module, "create_pool", create_pool)
    dsn = "postgresql://mockingbird@db.example/postgres"

    with TestClient(create_app(settings.model_copy(update={"database_url": dsn}))) as c:
        store = c.app.state.store
        assert isinstance(store, PostgresStore)
        assert store._pool is pool
        assert idle_sweeper == [store]  # the sweeper works on the store that was built
        assert not pool.closed

    assert dsns == [dsn]
    assert pool.closed


def test_injected_dependencies_are_used_as_given_and_nothing_real_is_built(
    settings, monkeypatch, idle_sweeper
):
    built: list[str] = []

    def builder(name: str):
        def build(*args, **kwargs):
            built.append(name)
            raise AssertionError(f"{name} was built although a fake was injected")

        return build

    monkeypatch.setattr(app_module, "create_pool", builder("create_pool"))
    monkeypatch.setattr(app_module, "MemoryStore", builder("MemoryStore"))
    monkeypatch.setattr(app_module, "make_anthropic_client", builder("make_anthropic_client"))
    monkeypatch.setattr(
        app_module, "anthropic", SimpleNamespace(AsyncAnthropic=builder("AsyncAnthropic"))
    )
    monkeypatch.setattr(app_module, "httpx", SimpleNamespace(AsyncClient=builder("AsyncClient")))
    store, vapi, grader = MemoryStore(), object(), object()

    def streamer_factory():
        return object()

    with_db = settings.model_copy(update={"database_url": "postgresql://unused/db"})
    a = create_app(
        with_db, store=store, vapi=vapi, streamer_factory=streamer_factory, grader=grader
    )
    with TestClient(a) as c:
        state = c.app.state
        assert state.store is store
        assert state.vapi is vapi
        assert state.streamer_factory is streamer_factory
        assert state.grader is grader
        assert idle_sweeper == [store]

    assert built == []
