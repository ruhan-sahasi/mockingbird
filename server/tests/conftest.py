import pytest
from fastapi.testclient import TestClient

from mockingbird.app import create_app
from mockingbird.config import Settings
from mockingbird.store import MemoryStore


def test_settings() -> Settings:
    """Dummy settings. `_env_file=None` keeps a developer's real .env out of the tests."""
    return Settings(
        _env_file=None,
        anthropic_api_key="test-anthropic-key",
        vapi_api_key="test-vapi-key",
        database_url=None,
        supabase_url=None,
        supabase_service_key=None,
        public_base_url="https://mockingbird.test",
        brain_secret="test-brain-secret",
        webhook_secret="test-webhook-secret",
        owner_key="test-owner-key",
        visitor_salt="test-visitor-salt",
    )


@pytest.fixture
def settings(tmp_path) -> Settings:
    """test_settings() with RECORDINGS_DIR in this test's tmp folder. An app built without an
    injected audio store keeps recordings in a local folder, and the default, "recordings", is
    relative to the working directory, which is the repo. Every test that starts the app gets
    these settings (or a copy of them), so a test call never writes WAV files into the repo."""
    return test_settings().model_copy(update={"recordings_dir": str(tmp_path / "recordings")})


@pytest.fixture
def client(settings: Settings):
    with TestClient(create_app(settings, store=MemoryStore())) as c:
        yield c
