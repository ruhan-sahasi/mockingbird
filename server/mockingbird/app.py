"""The FastAPI app. `create_app` is the one place that wires routes to dependencies."""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

import anthropic
import httpx
from fastapi import FastAPI

from mockingbird import routes_api, routes_vapi, routes_ws
from mockingbird.claude import AnthropicStreamer, make_anthropic_client
from mockingbird.config import Settings, get_settings
from mockingbird.report import Grader, load_rubric
from mockingbird.sessions import SessionRegistry, run_sweeper
from mockingbird.store import MemoryStore, PostgresStore, Store, create_pool
from mockingbird.vapi_client import VapiClient

log = logging.getLogger(__name__)

# Grading one interview takes Opus a while with adaptive thinking on, and nobody is waiting on
# the line, so the grader gets its own client with a long read timeout. The live client's 10 s
# would cut most grades off.
GRADER_TIMEOUT = anthropic.Timeout(120.0, connect=5.0)
GRADER_MAX_RETRIES = 2


def create_app(
    settings: Settings,
    *,
    store: Store | None = None,
    vapi: Any | None = None,
    streamer_factory: Any | None = None,
    grader: Any | None = None,
) -> FastAPI:
    """Build the app. The keyword arguments are injection points, and tests pass fakes. Whatever
    is not passed is built from `settings` when the app starts: PostgresStore when DATABASE_URL
    is set (MemoryStore when it is not), the Vapi client, the live Claude streamer and the
    grader. Starting also starts the sweeper (sessions.py); stopping closes what was built."""

    @asynccontextmanager
    async def lifespan(api: FastAPI) -> AsyncIterator[None]:
        state = api.state
        async with AsyncExitStack() as built:
            await _build_missing(state, settings, built)
            sweeper = asyncio.create_task(run_sweeper(state.registry, state.store))
            state.sweeper = sweeper
            try:
                yield
            finally:
                # Reports still being graded are lost. Their interviews stay "ended".
                tasks = [sweeper, *state.background]
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    api = FastAPI(title="Mockingbird", lifespan=lifespan)
    api.state.settings = settings
    api.state.store = store
    api.state.vapi = vapi
    api.state.streamer_factory = streamer_factory
    api.state.grader = grader
    api.state.registry = SessionRegistry()
    api.state.start_lock = asyncio.Lock()
    api.state.background = set()  # report grading tasks, held until they finish
    api.include_router(routes_api.router)
    api.include_router(routes_ws.router)
    api.include_router(routes_vapi.router)
    return api


async def _build_missing(state: Any, settings: Settings, built: AsyncExitStack) -> None:
    """Build each real dependency that was not injected, and register how to close it."""
    if state.store is None:
        if settings.database_url:
            pool = await create_pool(settings.database_url)
            built.push_async_callback(pool.close)
            state.store = PostgresStore(pool)
        else:
            log.warning("DATABASE_URL is not set: interviews are kept in memory only")
            state.store = MemoryStore()
    if state.vapi is None:
        http = httpx.AsyncClient()
        built.push_async_callback(http.aclose)
        state.vapi = VapiClient(settings.vapi_api_key, http)
    if state.streamer_factory is None:
        # One client for every interview, so they share its connection pool to the API.
        live = make_anthropic_client(settings.anthropic_api_key)
        built.push_async_callback(live.close)
        state.streamer_factory = lambda: AnthropicStreamer(live)
    if state.grader is None:
        grading = anthropic.AsyncAnthropic(
            api_key=settings.anthropic_api_key,
            timeout=GRADER_TIMEOUT,
            max_retries=GRADER_MAX_RETRIES,
        )
        built.push_async_callback(grading.close)
        state.grader = Grader(grading, load_rubric())


def app() -> FastAPI:
    """Zero-argument factory: `uvicorn mockingbird.app:app --factory`."""
    return create_app(get_settings())
