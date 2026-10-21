import pytest
from fastapi.testclient import TestClient

from mockingbird.app import create_app
from mockingbird.config import Settings


def test_settings() -> Settings:
    """Dummy settings. `_env_file=None` keeps a developer's real .env out of the tests."""
    return Settings(
        _env_file=None,
        anthropic_api_key="test-anthropic-key",
        vapi_api_key="test-vapi-key",
        database_url=None,
        public_base_url="https://mockingbird.test",
        brain_secret="test-brain-secret",
        webhook_secret="test-webhook-secret",
        owner_key="test-owner-key",
        visitor_salt="test-visitor-salt",
    )


@pytest.fixture
def settings() -> Settings:
    return test_settings()


@pytest.fixture
def client(settings: Settings):
    # Task 7 adds MemoryStore here. Until then there is no store.
    with TestClient(create_app(settings, store=None)) as c:
        yield c
