"""The FastAPI app. `create_app` is the one place that wires routes to dependencies."""

from typing import Any, Protocol

from fastapi import FastAPI

from mockingbird.config import Settings, get_settings


class Pingable(Protocol):
    """All /healthz needs from a store. Task 7 adds the real Store, which has ping()."""

    async def ping(self) -> None: ...


def create_app(
    settings: Settings,
    *,
    store: Pingable | None = None,
    vapi: Any | None = None,
    streamer_factory: Any | None = None,
    grader: Any | None = None,
) -> FastAPI:
    """Build the app. The keyword arguments are injection points: tests pass fakes. Only
    `store` is used so far; the rest are kept on `app.state` for the routes later tasks add."""
    api = FastAPI(title="Mockingbird")
    api.state.settings = settings
    api.state.store = store
    api.state.vapi = vapi
    api.state.streamer_factory = streamer_factory
    api.state.grader = grader

    @api.get("/healthz")
    async def healthz(db: bool = False) -> dict[str, bool]:
        # `?db=1` also checks the database. That is the Supabase keep-alive. Plain
        # /healthz is what the load balancer polls, so it must never touch the database.
        if db and store is not None:
            await store.ping()
        return {"ok": True}

    return api


def app() -> FastAPI:
    """Zero-argument factory: `uvicorn mockingbird.app:app --factory`."""
    return create_app(get_settings())
