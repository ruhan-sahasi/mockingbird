"""Settings, read from environment variables (or a local .env file)."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # env_ignore_empty: `NAME=` with no value counts as unset, so an unfilled .env.example
    # line fails at startup instead of starting the server with an empty key.
    # hide_input_in_errors: a startup error must not print the keys that were set.
    model_config = SettingsConfigDict(
        env_file=".env", extra="ignore", env_ignore_empty=True, hide_input_in_errors=True
    )

    anthropic_api_key: str
    vapi_api_key: str
    database_url: str | None = None
    public_base_url: str
    brain_secret: str
    webhook_secret: str
    owner_key: str
    visitor_salt: str
    daily_demo_minutes: int = 60
    demo_cap_s: int = 300
    full_cap_s: int = 1200


@lru_cache
def get_settings() -> Settings:
    return Settings()
