import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mockingbird.app import app
from mockingbird.config import get_settings

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
