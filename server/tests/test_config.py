import pytest
from pydantic import ValidationError

from mockingbird.config import Settings

REQUIRED = {
    "ANTHROPIC_API_KEY": "a-key",
    "VAPI_API_KEY": "v-key",
    "PUBLIC_BASE_URL": "https://mockingbird.test",
    "BRAIN_SECRET": "brain",
    "WEBHOOK_SECRET": "hook",
    "OWNER_KEY": "owner",
    "VISITOR_SALT": "salt",
}
OPTIONAL = ["DATABASE_URL", "DAILY_DEMO_MINUTES", "DEMO_CAP_S", "FULL_CAP_S"]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Start every test with none of the settings in the environment."""
    for name in [*REQUIRED, *OPTIONAL]:
        monkeypatch.delenv(name, raising=False)


def test_optional_settings_have_defaults(monkeypatch):
    for name, value in REQUIRED.items():
        monkeypatch.setenv(name, value)
    s = Settings(_env_file=None)
    assert s.anthropic_api_key == "a-key"
    assert s.database_url is None
    assert s.daily_demo_minutes == 60
    assert s.demo_cap_s == 300
    assert s.full_cap_s == 1200


def test_every_setting_comes_from_the_environment(monkeypatch):
    for name, value in REQUIRED.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("DATABASE_URL", "postgresql://db.test/mb")
    monkeypatch.setenv("DAILY_DEMO_MINUTES", "15")
    monkeypatch.setenv("DEMO_CAP_S", "120")
    monkeypatch.setenv("FULL_CAP_S", "600")
    s = Settings(_env_file=None)
    assert s.vapi_api_key == "v-key"
    assert s.public_base_url == "https://mockingbird.test"
    assert s.brain_secret == "brain"
    assert s.webhook_secret == "hook"
    assert s.owner_key == "owner"
    assert s.visitor_salt == "salt"
    assert s.database_url == "postgresql://db.test/mb"
    assert (s.daily_demo_minutes, s.demo_cap_s, s.full_cap_s) == (15, 120, 600)


def test_an_empty_required_value_counts_as_missing(monkeypatch):
    # .env.example ships `NAME=` lines. An unfilled one must fail at startup, not
    # start the server with an empty key.
    for name, value in REQUIRED.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    with pytest.raises(ValidationError) as err:
        Settings(_env_file=None)
    assert [e["loc"] for e in err.value.errors()] == [("anthropic_api_key",)]


def test_a_startup_error_does_not_echo_secret_values(monkeypatch):
    # The error is logged at startup. It must not print the keys that were set.
    for name, value in REQUIRED.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("VAPI_API_KEY", "leakcheck-0123456789")
    with pytest.raises(ValidationError) as err:
        Settings(_env_file=None)
    # pydantic shortens long values, so look for the start of the key, not all of it.
    assert "leakc" not in str(err.value)
