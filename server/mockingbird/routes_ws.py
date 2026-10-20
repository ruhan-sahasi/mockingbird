"""The interview socket: /ws/interview/{id}?token=...

Opening it with the token from POST /api/interviews starts the call, in this order: the brain
gives the opening line, Vapi creates a call that speaks it, the relay connects to the call's
audio socket, the interview is marked live, and the relay runs until the call ends. A token that
is wrong, used already or expired gets the socket closed with code 4401 (Review Focus 5).

While the call runs, the relay hands the route Vapi's live events (Amendment 1):
- the interviewer started speaking: the brain commits the reply Vapi is speaking;
- the interviewer stopped speaking after the wrap-up was committed: the call ends, "plan_done".

When the call ends, however it ends, the clean-up runs in a `finally`: `run()` can also raise
(an ExceptionGroup when a relay task failed, CancelledError on shutdown), and the interview must
not be left live. The interview is marked ended with how long it ran and why, its turns are
saved, the session is removed and the report is graded in the background.

A call that never went live (Vapi refused it, or its socket would not connect) is marked
`abandoned` instead and gets no report: nobody was interviewed, so the visitor's demo is not
spent. The browser's socket is closed with 1011.

The Vapi call id and its socket URL are credentials (ruling R19). They go to Vapi, the registry
and the store, never to the browser, and the logs here name only exception types.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Coroutine, Sequence
from typing import Any

import websockets
from fastapi import APIRouter, WebSocket

from mockingbird.models import Turn
from mockingbird.relay import LiveEvent, OnEvent, Relay, StarletteSocket, WebsocketsSocket
from mockingbird.report import build_report
from mockingbird.sessions import LiveSession
from mockingbird.store import Store
from mockingbird.vapi_client import build_assistant
from mockingbird.vapi_types import SpeechUpdate

log = logging.getLogger(__name__)

TOKEN_REJECTED = 4401
INTERNAL_ERROR = 1011
# The relay closes Vapi's socket within its own hang-up limit. websockets' own wait for the
# closing handshake is kept short too, so a dead connection is dropped and not held for 10 s.
VAPI_CLOSE_TIMEOUT_S = 2.0
# How long the clean-up waits for a cancelled brain reply to stop before writing the turns.
REPLY_STOP_TIMEOUT_S = 1.0
ERROR = "error"  # the ended_reason when the call failed rather than ended

router = APIRouter()


@router.websocket("/ws/interview/{interview_id}")
async def interview_socket(websocket: WebSocket, interview_id: str, token: str = "") -> None:
    # Accept before checking, so the browser sees the 4401 close code. A socket refused before
    # it is accepted reaches the browser as a failed handshake with no code.
    await websocket.accept()
    session = websocket.app.state.registry.consume_token(interview_id, token)
    if session is None:
        await websocket.close(code=TOKEN_REJECTED)
        return
    await _run_call(websocket, session)


async def _run_call(websocket: WebSocket, session: LiveSession) -> None:
    state = websocket.app.state
    settings = state.settings
    interview = session.interview
    reason = ERROR
    live_since: float | None = None
    try:
        opening = session.brain.start()
        base = settings.public_base_url.rstrip("/")
        assistant = build_assistant(
            opening,
            llm_url=f"{base}/vapi/llm/{settings.brain_secret}",
            webhook_url=f"{base}/vapi/webhook/{settings.webhook_secret}",
            cap_s=interview.cap_s,
        )
        call = await state.vapi.create_call(assistant)
        state.registry.bind_call(interview.id, call.id)
        # Kept so the end-of-call webhook, which names only the call, can find the interview.
        await state.store.update_interview(interview.id, vapi_call_id=call.id)
        connection = await websockets.connect(
            call.websocket_url, close_timeout=VAPI_CLOSE_TIMEOUT_S
        )
        session.relay = Relay(
            StarletteSocket(websocket),
            WebsocketsSocket(connection),
            cap_s=interview.cap_s,
            on_event=_live_events(session),
        )
        await state.store.update_interview(interview.id, status="live")
        live_since = time.monotonic()
        reason = await session.relay.run()
    except Exception as exc:
        # The type only: a library's message can hold the socket URL, which holds the call id.
        log.error("the call for interview %s failed: %s", interview.id, type(exc).__name__)
    finally:
        await _clean_up(websocket, session, reason=reason, live_since=live_since)


def _live_events(session: LiveSession) -> OnEvent:
    async def on_event(event: LiveEvent) -> None:
        match event:
            case SpeechUpdate(role="assistant", status="started"):
                # Vapi speaks the reply to its latest request, which is the brain's pending draft.
                session.brain.commit_spoken()
            case SpeechUpdate(role="assistant", status="stopped") if session.brain.finished:
                # The wrap-up line has been said (or cut off): the interview is over.
                await session.relay.end("plan_done")
            case _:
                log.debug("no action for a %s event", type(event).__name__)

    return on_event


async def _clean_up(
    websocket: WebSocket, session: LiveSession, *, reason: str, live_since: float | None
) -> None:
    state = websocket.app.state
    store = state.store
    interview_id = session.interview.id

    if session.relay is not None:
        # Normally run() has hung up already and this returns at once. If run() never ran,
        # this ends the Vapi call now, so it cannot run on until its own time limit.
        await session.relay.end(reason)
    else:
        await StarletteSocket(websocket).close(INTERNAL_ERROR)
    await _stop_reply(session)
    session.brain.finalize()

    if live_since is None:
        await _attempt(
            "mark the interview abandoned",
            store.update_interview(interview_id, status="abandoned", ended_reason=ERROR),
        )
        state.registry.remove(interview_id)
        return

    await _attempt(
        "mark the interview ended",
        store.update_interview(
            interview_id,
            status="ended",
            duration_s=round(time.monotonic() - live_since),
            ended_reason=reason,
        ),
    )
    await _attempt("save the turns", _save_turns(store, interview_id, session.brain.turns))
    state.registry.remove(interview_id)
    _in_background(state, build_report(store, state.grader, interview_id))


async def _stop_reply(session: LiveSession) -> None:
    """Cancel a brain reply still streaming to Vapi: nobody will hear it now. A reply that was
    being spoken is kept by the brain, so the clean-up waits for the cancel to land first."""
    task = session.reply_task
    if task is not None and not task.done():
        task.cancel()
        await asyncio.wait({task}, timeout=REPLY_STOP_TIMEOUT_S)


async def _save_turns(store: Store, interview_id: str, turns: Sequence[Turn]) -> None:
    for turn in turns:
        await store.add_turn(interview_id, turn)


def _in_background(state: Any, step: Coroutine[Any, Any, None]) -> None:
    # The event loop keeps only a weak reference to a task, so hold one until it is done.
    task = asyncio.create_task(step)
    state.background.add(task)
    task.add_done_callback(state.background.discard)


async def _attempt(what: str, step: Awaitable[None]) -> None:
    """One clean-up step. A failure is logged and the clean-up goes on to the next step."""
    try:
        await step
    except Exception as exc:
        log.error("could not %s: %s", what, type(exc).__name__)
