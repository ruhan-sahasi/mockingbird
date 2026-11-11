"""Settings, read from environment variables (or a local .env file)."""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # env_ignore_empty: `NAME=` with no value counts as unset, so an unfilled .env.example
    # line fails at startup instead of starting the server with an empty key.
    # hide_input_in_errors: a startup error must not print the keys that were set.
    model_config = SettingsConfigDict(
        env_file=".env", extra="ignore", env_ignore_empty=True, hide_input_in_errors=True
    )

    # repr=False on every secret keeps it out of repr(settings), so a logged Settings is safe.
    anthropic_api_key: str = Field(repr=False)
    vapi_api_key: str = Field(repr=False)
    database_url: str | None = Field(default=None, repr=False)
    public_base_url: str
    brain_secret: str = Field(repr=False)
    webhook_secret: str = Field(repr=False)
    owner_key: str = Field(repr=False)
    visitor_salt: str = Field(repr=False)
    daily_demo_minutes: int = 60
    demo_cap_s: int = 300
    full_cap_s: int = 1200
    # Recordings go to a private Supabase Storage bucket when both of the first two are set,
    # and to a local folder otherwise.
    supabase_url: str | None = None
    supabase_service_key: str | None = Field(default=None, repr=False)
    recordings_bucket: str = "recordings"
    recordings_dir: str = "recordings"


@lru_cache
def get_settings() -> Settings:
    return Settings()
