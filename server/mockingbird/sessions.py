"""The interviews this process is running, held in memory, and the sweeper that tidies up after
the ones that never started or never finished.

The server is one ECS task, so a dict is enough to find a live interview again: by our interview
id when the browser's socket opens, and by Vapi's call id when Vapi asks the brain for a line.

A `LiveSession` starts when POST /api/interviews creates the interview. It carries a one-time
token that opens the interview's socket within TOKEN_TTL_S. That token is the only thing standing
between a stranger and a paid call, so it is single use, and it is compared in constant time.

The call id and the token are credentials (ruling R19 for the call id), so both stay out of a
session's repr, and nothing here logs either of them.

`sweep_once` runs every minute from `run_sweeper`:
- a session whose token ran out unused is removed, and its interview marked `abandoned`, which
  gives the visitor their demo back (abandoned interviews do not count as starts);
- an interview the store still has as `live` well past its cap is marked ended with the reason
  "stale" (ruling R23). That is a call this process lost track of, most likely in a crash, and
  until it is ended the daily budget keeps counting its full cap.
`run_sweeper` also deletes demo data older than RETENTION_DAYS, once an hour.
"""

import asyncio
import hmac
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from mockingbird.brain import Brain
from mockingbird.models import Interview
from mockingbird.relay import Relay
from mockingbird.store import Store

log = logging.getLogger(__name__)

TOKEN_TTL_S = 60.0  # how long the socket token from POST /api/interviews stays good
# A live interview older than its cap plus this is stale. The relay ends every call at its cap,
# so a call still live this long after it was created is one nobody is running.
STALE_AFTER_CAP_S = 60
SWEEP_EVERY_S = 60.0
PURGE_EVERY_S = 3600.0
RETENTION_DAYS = 7  # demo audio and transcripts are deleted after this many days (the spec)


@dataclass
class LiveSession:
    """One interview this process is running, from its start request until its call ends."""

    interview: Interview  # as it was created; the store has its current state
    brain: Brain
    token: str = field(repr=False)  # "" once it has been used
    token_expires_at: float  # by the registry's clock
    relay: Relay | None = None
    call_id: str | None = field(default=None, repr=False)
    reply_task: asyncio.Task | None = None  # the brain reply now streaming to Vapi, if any


class SessionRegistry:
    """The live sessions, by interview id and by Vapi call id. `clock` is in seconds since the
    epoch, the same clock the sweeper's `now` uses."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self.clock = clock
        self._by_interview: dict[str, LiveSession] = {}
        self._by_call: dict[str, LiveSession] = {}

    def add(self, s: LiveSession) -> None:
        self._by_interview[s.interview.id] = s
        if s.call_id is not None:
            self._by_call[s.call_id] = s

    def by_interview(self, id: str) -> LiveSession | None:
        return self._by_interview.get(id)

    def by_call(self, call_id: str) -> LiveSession | None:
        return self._by_call.get(call_id)

    def bind_call(self, interview_id: str, call_id: str) -> None:
        """Record the Vapi call an interview's session is running."""
        s = self._by_interview[interview_id]
        s.call_id = call_id
        self._by_call[call_id] = s

    def consume_token(self, interview_id: str, token: str) -> LiveSession | None:
        """The session, if `token` is its token and is still good. A token works once."""
        s = self._by_interview.get(interview_id)
        if s is None or not s.token or not token:
            return None
        if self.clock() > s.token_expires_at:
            return None
        # bytes, because compare_digest refuses a str with characters outside ASCII
        if not hmac.compare_digest(s.token.encode(), token.encode()):
            return None
        s.token = ""
        return s

    def remove(self, id: str) -> None:
        s = self._by_interview.pop(id, None)
        if s is not None and s.call_id is not None:
            self._by_call.pop(s.call_id, None)

    def sessions(self) -> list[LiveSession]:
        return list(self._by_interview.values())


async def sweep_once(registry: SessionRegistry, store: Store, now: float) -> None:
    """Abandon the interviews whose token ran out unused, and end the stale live ones.

    `now` is in seconds since the epoch. Each write is tried on its own, so one that fails does
    not stop the rest; it is tried again on the next sweep.
    """
    for s in registry.sessions():
        if s.token and now > s.token_expires_at:
            # Removed first: once the token is gone from the registry nothing can open it.
            registry.remove(s.interview.id)
            await _attempt(
                "abandon an unused interview",
                store.update_interview(s.interview.id, status="abandoned"),
            )

    for i in await store.list_live_interviews():
        # A session in the registry is a call this process is running. Its route ends it.
        if registry.by_interview(i.id) is not None:
            continue
        if i.created_at.timestamp() + i.cap_s + STALE_AFTER_CAP_S < now:
            await _attempt(
                "end a stale interview",
                store.update_interview(
                    i.id, status="ended", ended_reason="stale", duration_s=i.cap_s
                ),
            )


async def run_sweeper(
    registry: SessionRegistry,
    store: Store,
    *,
    clock: Callable[[], float] = time.time,
    every_s: float = SWEEP_EVERY_S,
    purge_every_s: float = PURGE_EVERY_S,
) -> None:
    """Sweep every `every_s`, and delete old demo data at start and then every `purge_every_s`.
    Runs until cancelled. A failure is logged and the loop goes on."""
    last_purge: float | None = None
    while True:
        now = clock()
        await _attempt("sweep the sessions", sweep_once(registry, store, now))
        if last_purge is None or now - last_purge >= purge_every_s:
            last_purge = now
            await _attempt("delete old demo data", _purge(store))
        await asyncio.sleep(every_s)


async def _purge(store: Store) -> None:
    deleted = await store.delete_demo_data_older_than(RETENTION_DAYS)
    if deleted:
        log.info("deleted %d demo interview(s) older than %d days", deleted, RETENTION_DAYS)


async def _attempt(what: str, step: Awaitable[object]) -> None:
    try:
        await step
    except Exception as exc:
        # The type only: the store's messages are safe, but a driver error is not (ruling R19).
        log.error("could not %s: %s", what, type(exc).__name__)
