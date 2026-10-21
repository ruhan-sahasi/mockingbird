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
- any interview the store still has as `created`, older than a token's life, that no session here
  holds is marked `abandoned` too. Those are the starts a restart (every deploy is one) left
  behind: their tokens were in the old process's memory, so they can never start, and as
  `created` each would hold its visitor's demo and a cap of the day's budget until midnight;
- any interview the store still has as `live` well past its cap is marked ended with the reason
  "stale" and its cap as its duration (ruling R23). That is mostly a call this process lost
  track of in a crash, and until it is ended the daily budget keeps counting its full cap. It
  can also be a call this process is still running: one whose socket opened near the end of
  its token's minute goes live a little over a minute after it was created, so its last few
  seconds can pass the stale line. That call's route marks it ended again when it does end,
  with the real reason and duration, so the stale mark only stands for a call nobody ends.
`run_sweeper` also deletes demo data older than RETENTION_DAYS, once an hour (`purge_once`). That
covers Vapi's copy of each call too, the transcript, messages, logs and analysis it keeps: each
old demo's call is deleted at Vapi first, while its row still holds the call id, and a row whose
call Vapi would not delete is kept, call id and all, for the next purge to try again.
"""

import asyncio
import hmac
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from mockingbird.brain import Brain
from mockingbird.failures import describe
from mockingbird.models import Interview
from mockingbird.relay import Relay
from mockingbird.store import Store

log = logging.getLogger(__name__)

TOKEN_TTL_S = 60.0  # how long the socket token from POST /api/interviews stays good
# A live interview older than its cap plus this is stale (ruling R23). The relay ends every call
# at its cap, and a call goes live at most about TOKEN_TTL_S after it was created.
STALE_AFTER_CAP_S = 60
SWEEP_EVERY_S = 60.0
PURGE_EVERY_S = 3600.0
RETENTION_DAYS = 7  # demo audio and transcripts are deleted after this many days (the spec)


class CallDeleter(Protocol):
    """The part of VapiClient the purge uses."""

    async def delete_call(self, call_id: str) -> None: ...


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
    """Abandon the interviews whose token ran out unused or that nothing holds, and end the
    stale live ones.

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

    # A start younger than a token's life may be one whose session is about to be added.
    await _attempt(
        "abandon the starts nothing holds",
        store.abandon_unstarted(
            created_before=datetime.fromtimestamp(now - TOKEN_TTL_S, UTC),
            keep=[s.interview.id for s in registry.sessions()],
        ),
    )

    for i in await store.list_live_interviews():
        if i.created_at.timestamp() + i.cap_s + STALE_AFTER_CAP_S < now:
            await _attempt("end a stale interview", _end_stale(store, i))


async def _end_stale(store: Store, i: Interview) -> None:
    # Read again first: its route may have ended it since the list was read, and its reason and
    # duration are the real ones.
    current = await store.get_interview(i.id)
    if current is None or current.status != "live":
        return
    await store.update_interview(i.id, status="ended", ended_reason="stale", duration_s=i.cap_s)


async def run_sweeper(
    registry: SessionRegistry,
    store: Store,
    vapi: CallDeleter,
    *,
    clock: Callable[[], float] = time.time,
    every_s: float = SWEEP_EVERY_S,
    purge_every_s: float = PURGE_EVERY_S,
    sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
) -> None:
    """Sweep every `every_s`, and delete old demo data at start and then every `purge_every_s`.
    Runs until cancelled. A failure is logged and the loop goes on. `clock` and `sleep` are
    there for tests."""
    last_purge: float | None = None
    while True:
        now = clock()
        await _attempt("sweep the sessions", sweep_once(registry, store, now))
        if last_purge is None or now - last_purge >= purge_every_s:
            last_purge = now
            await _attempt("delete old demo data", purge_once(store, vapi))
        await sleep(every_s)


async def purge_once(store: Store, vapi: CallDeleter) -> None:
    """Delete the demo data older than RETENTION_DAYS: each call's copy at Vapi first, then our
    rows. A row whose call Vapi would not delete is kept for the next purge."""
    deleted_at_vapi = []
    for call_id in await store.demo_calls_older_than(RETENTION_DAYS):
        try:
            await vapi.delete_call(call_id)
        except Exception as exc:
            # A VapiError's message holds no call id, URL or key; anything else gives its type.
            msg = "could not delete an old call at Vapi, kept for the next purge: %s"
            log.warning(msg, describe(exc))
        else:
            deleted_at_vapi.append(call_id)
    deleted = await store.delete_demo_data_older_than(RETENTION_DAYS, vapi_deleted=deleted_at_vapi)
    if deleted:
        log.info("deleted %d demo interview(s) older than %d days", deleted, RETENTION_DAYS)


async def _attempt(what: str, step: Awaitable[object]) -> None:
    try:
        await step
    except Exception as exc:
        # The type only: the store's messages are safe, but a driver error is not (ruling R19).
        log.error("could not %s: %s", what, type(exc).__name__)
