"""The routes Vapi calls: the brain (its custom LLM) and the webhook. Each has a secret in its
path, and a wrong secret gets the same 404 as a path that does not exist (Review Focus 5).

The brain route answers every request with a stream Vapi can speak, never an error: a request
for a call that is not live here, or one whose interview has already wrapped up, gets one fixed
line (Review Focus 2).

Vapi sends several brain requests per answer, as its transcript grows, and speaks only the reply
to the last one (Amendment 1). So a new request cancels the reply still streaming for the one
before it, and only then starts the new draft. Both happen in one synchronous step, with no
await between them, so two requests that arrive together cannot both start a draft. The brain
keeps its history valid whatever is cancelled: a draft is real only once Vapi starts speaking
it (routes_ws.py commits it), and a draft that is never spoken leaves no trace.

The draft runs as its own task, feeding a queue that the response streams from, so that the
next request can cancel it. When Vapi drops the stream, the draft is cancelled too.

Each request gets a latency trace from the call's tracer when it arrives. The trace rides on the
draft, and is stamped when the brain yields Claude's first text and when the first text goes out
to Vapi. A reply whose first text is a line the brain says itself (the wrap-up, or the fallback
when Claude failed before writing anything) gets no first-token stamp: that line takes no time
to write, and timing it as Claude's would drag Claude's measured time to first token toward 0.
Measuring must never cut off the interviewer's line, so each stamp is guarded: a tracing bug is
logged by type only, and the reply streams on without that stamp.

The webhook acts only on the end-of-call report. Live events come from the audio socket, so
every other message gets 200 `{}` and nothing else.

The call id is a credential (ruling R19). Nothing here logs it or a request body, and the fixed
line never echoes anything from the request.
"""

import asyncio
import contextlib
import hmac
import logging
import time
import uuid
from collections.abc import AsyncIterator, Callable
from functools import partial

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from mockingbird.brain import Brain, FixedLine
from mockingbird.sessions import LiveSession
from mockingbird.sse import SSE_DONE, sse_chunk
from mockingbird.store import Store
from mockingbird.tracing import CallTracer, TurnTrace
from mockingbird.vapi_types import (
    EndOfCallReport,
    Ignored,
    LlmRequest,
    parse_llm_request,
    parse_server_message,
)

log = logging.getLogger(__name__)

ENDED_LINE = "Sorry, this interview has ended."
# The reasons the relay gives when this server ended the call. Vapi's own label for such a call
# is a fixed string that says nothing about why (vapi-contract.md, question 7), so ours is kept.
# For any other end (Vapi closed the call, or the sweeper found it stale) Vapi's reason says more.
_WE_ENDED_IT = frozenset({"cap", "browser_closed", "plan_done", "error"})
_END = object()  # put on a draft's queue once its task is done, however it ended

router = APIRouter()


@router.post("/vapi/llm/{secret}/chat/completions")
async def brain_reply(secret: str, request: Request) -> StreamingResponse:
    state = request.app.state
    if not _secret_matches(secret, state.settings.brain_secret):
        raise HTTPException(status_code=404)
    req = await _llm_request(request)
    session = state.registry.by_call(req.call_id) if req is not None else None
    if req is None or session is None or session.brain.finished:
        log.info("a brain request for no live interview got the fixed line")
        return _event_stream(_sse(_fixed_line(ENDED_LINE)))

    return _event_stream(start_reply(session, req))


@router.post("/vapi/webhook/{secret}")
async def webhook(secret: str, request: Request) -> dict:
    state = request.app.state
    if not _secret_matches(secret, state.settings.webhook_secret):
        raise HTTPException(status_code=404)
    try:
        body = await request.json()
    except ValueError:
        body = {}
    message = parse_server_message(body)
    if isinstance(message, EndOfCallReport) and message.call_id is not None:
        try:
            await _record_vapi_end(state.store, message)
        except Exception as exc:
            log.error("could not record an end-of-call report: %s", type(exc).__name__)
    else:
        kind = message.type if isinstance(message, Ignored) else type(message).__name__
        log.debug("webhook: no action for a %s message", kind)
    return {}


async def _record_vapi_end(store: Store, report: EndOfCallReport) -> None:
    interview = await store.get_interview_by_call(report.call_id)
    if interview is None or interview.ended_reason in _WE_ENDED_IT:
        return
    await store.update_interview(interview.id, ended_reason=report.ended_reason)


async def _llm_request(request: Request) -> LlmRequest | None:
    """The request, or None when the body is not JSON or names no call."""
    try:
        return parse_llm_request(await request.json())
    except ValueError:  # json.JSONDecodeError and UnicodeDecodeError are ValueErrors too
        return None


def start_reply(session: LiveSession, req: LlmRequest) -> AsyncIterator[str]:
    """Cancel the reply still streaming for the session, start drafting this one, and return its
    SSE events. There is no await in here, so nothing can run between the cancel and the start.
    """
    tracer = session.tracer
    trace = _stamp(tracer.new_trace) if tracer is not None else None
    previous = session.reply_task
    if previous is not None and not previous.done():
        previous.cancel()
    chunks: asyncio.Queue = asyncio.Queue()
    task = asyncio.create_task(_draft(session.brain, req, chunks, tracer, trace))
    # The end marker goes on from a done callback, not a `finally` in _draft: a task cancelled
    # before its first step never runs its body at all, and its stream would wait forever.
    task.add_done_callback(lambda _: chunks.put_nowait(_END))
    session.reply_task = task
    on_first = partial(_stamp, tracer.first_chunk, trace) if trace is not None else None
    return _stream_draft(task, chunks, on_first)


def _stamp[T](hop: Callable[..., T], *args: TurnTrace) -> T | None:
    """Make one tracer stamp and return what it gives. Measuring a reply must never cut it off,
    so a tracing bug is logged by its exception type only (ruling R19), the stamp gives None,
    and the reply goes on without it."""
    try:
        return hop(*args)
    except Exception as exc:
        log.error("tracing a reply failed: %s", type(exc).__name__)
        return None


async def _draft(
    brain: Brain,
    req: LlmRequest,
    chunks: asyncio.Queue,
    tracer: CallTracer | None = None,
    trace: TurnTrace | None = None,
) -> None:
    """Draft the reply into `chunks`. Cancelling this task stops the draft, and aclosing()
    makes sure the brain's generator, and with it the Claude stream, is shut. `trace` is from
    `tracer`. It is None when `tracer` is, or when making it failed, and then nothing is
    stamped."""
    try:
        draft = brain.draft(req.user_text, req.spoken_assistant_text, trace=trace)
        async with contextlib.aclosing(draft) as reply:
            first = True
            async for chunk in reply:
                claude_wrote_it = not isinstance(chunk, FixedLine)
                if first and claude_wrote_it and tracer is not None and trace is not None:
                    _stamp(tracer.first_token, trace)
                first = False
                chunks.put_nowait(chunk)
    except Exception as exc:
        # The brain answers Claude's failures itself. This is a bug, so the stream just ends.
        log.error("drafting a reply failed: %s", type(exc).__name__)


async def _stream_draft(
    task: asyncio.Task, chunks: asyncio.Queue, on_first: Callable[[], None] | None = None
) -> AsyncIterator[str]:
    try:
        async for event in _sse(_until_end(chunks), on_first):
            yield event
    finally:
        # Vapi dropped this stream, or it is done: either way nothing more of it will be heard.
        task.cancel()


async def _until_end(chunks: asyncio.Queue) -> AsyncIterator[str]:
    while (chunk := await chunks.get()) is not _END:
        yield chunk


async def _fixed_line(text: str) -> AsyncIterator[str]:
    yield text


async def _sse(
    texts: AsyncIterator[str], on_first: Callable[[], None] | None = None
) -> AsyncIterator[str]:
    """OpenAI chat-completion chunks for each text, then the finish chunk and [DONE].
    `on_first` is called just before the first text's chunk goes out."""
    chunk_id = f"chatcmpl-{uuid.uuid4().hex}"
    created = int(time.time())
    first = True
    async for text in texts:
        if first and on_first is not None:
            on_first()
        yield sse_chunk(text, chunk_id=chunk_id, created=created, first=first)
        first = False
    yield sse_chunk("", chunk_id=chunk_id, created=created, finish=True)
    yield SSE_DONE


def _event_stream(events: AsyncIterator[str]) -> StreamingResponse:
    return StreamingResponse(
        events, media_type="text/event-stream", headers={"Cache-Control": "no-cache"}
    )


def _secret_matches(given: str, expected: str) -> bool:
    # bytes, because compare_digest refuses a str with characters outside ASCII
    return hmac.compare_digest(given.encode(), expected.encode())
