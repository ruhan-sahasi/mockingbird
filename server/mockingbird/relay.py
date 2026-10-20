"""The audio relay: the browser's socket on one side, the Vapi call's socket on the other.

Audio passes through unchanged both ways (16 kHz s16le mono, 640-byte frames). Vapi streams
continuously, silence included, and puts its live events on the same socket as text frames, in
order with the audio. The relay reads those events itself:
- `user-interrupted`: the browser is told to flush the reply it is still playing, before any
  later audio goes out, and then the route hears about it through `on_event`;
- `hangup`, or `status-update` with status "ended": the call is over, so the relay ends;
- `speech-update` and other `status-update` frames go to `on_event`;
- every other type (transcripts, model output and so on) is logged by type and dropped.
Nothing the browser sends as text ever reaches Vapi, because Vapi takes control messages on this
socket. The only text the browser may send is `{"type": "end"}`.

`run()` holds two pumps (one per direction), a cap timer and a browser ping under one
TaskGroup. `end(reason)` starts the hang-up and stops that group. The hang-up runs as its own
task, so cancelling whoever called `end()` cannot stop it halfway. It sends Vapi `end-call` first,
because that is what stops the call and its bill, then tells the browser, then closes both. When
`run()` exits without anyone having called `end()` (a task failed, or `run()` itself was
cancelled by a server shutdown), it hangs up anyway with reason "error" before raising, so no
path out of `run()` leaves a Vapi call running until its own time limit. `run()` only ever
returns "cap", "browser_closed", "vapi_closed" or "plan_done".

The relay never sees the call id or the websocket URL, which are credentials while a call is live
(ruling R19). It logs frame types, exception type names and where an error was raised, never a
frame's contents or an exception's message.
"""

import asyncio
import json
import logging
import traceback
from collections.abc import Awaitable, Callable
from typing import Protocol

from starlette.websockets import (
    WebSocket,
    WebSocketDisconnect,
    WebSocketDisconnected,
    WebSocketState,
)
from websockets.asyncio.client import ClientConnection
from websockets.exceptions import ConnectionClosed

from mockingbird.vapi_types import (
    Hangup,
    Ignored,
    SpeechUpdate,
    StatusUpdate,
    UserInterrupted,
    parse_ws_event,
)

log = logging.getLogger(__name__)

END_CALL = json.dumps({"type": "end-call"})
FLUSH = json.dumps({"type": "flush"})
PING = json.dumps({"type": "ping"})

# The browser hears this reason only when run() is about to raise. run() never returns it.
_ERROR = "error"

# What the route's `on_event` is given. Ignored frames and the ones that end the call are not.
LiveEvent = SpeechUpdate | UserInterrupted | StatusUpdate
OnEvent = Callable[[LiveEvent], Awaitable[None]]


class SocketClosed(Exception):
    """A send went to a socket that has closed or lost its connection. It has no message."""


class Socket(Protocol):
    """One side of the relay, whatever library sits underneath."""

    async def recv(self) -> bytes | str | None:
        """The next frame: bytes for a binary frame, str for text, None once the socket closed."""

    async def send_bytes(self, b: bytes) -> None:
        """Send a binary frame. Raises SocketClosed if the socket has closed."""

    async def send_text(self, s: str) -> None:
        """Send a text frame. Raises SocketClosed if the socket has closed."""

    async def close(self, code: int = 1000) -> None:
        """Close the socket. Closing one that is already closed does nothing."""


class StarletteSocket:
    """The browser's side: a Starlette WebSocket that the route has already accepted."""

    def __init__(self, ws: WebSocket) -> None:
        self._ws = ws

    async def recv(self) -> bytes | str | None:
        # Starlette raises if receive() is called again after the disconnect, so check first.
        if self._ws.client_state == WebSocketState.DISCONNECTED:
            return None
        message = await self._ws.receive()
        if message["type"] == "websocket.disconnect":
            return None
        # An ASGI receive message holds exactly one of the two, and may set the other to None.
        if message.get("bytes") is not None:
            return message["bytes"]
        return message.get("text") or ""

    async def send_bytes(self, b: bytes) -> None:
        await self._send({"type": "websocket.send", "bytes": b})

    async def send_text(self, s: str) -> None:
        await self._send({"type": "websocket.send", "text": s})

    async def _send(self, message: dict) -> None:
        try:
            await self._ws.send(message)
        # WebSocketDisconnect: the server lost the connection mid-send.
        # WebSocketDisconnected: the socket was already closed or lost before this send.
        except (WebSocketDisconnect, WebSocketDisconnected):
            raise SocketClosed from None

    async def close(self, code: int = 1000) -> None:
        try:
            await self._ws.close(code)
        except (WebSocketDisconnect, WebSocketDisconnected):
            pass  # already closed, or the browser has gone: either way it is closed


class WebsocketsSocket:
    """Vapi's side: a connection from `websockets.asyncio.client.connect`."""

    def __init__(self, conn: ClientConnection) -> None:
        self._conn = conn

    async def recv(self) -> bytes | str | None:
        try:
            return await self._conn.recv()
        except ConnectionClosed:
            return None

    # websockets picks the frame type from the Python type: bytes go as binary, str as text.
    async def send_bytes(self, b: bytes) -> None:
        await self._send(b)

    async def send_text(self, s: str) -> None:
        await self._send(s)

    async def _send(self, frame: bytes | str) -> None:
        try:
            await self._conn.send(frame)
        except ConnectionClosed:
            raise SocketClosed from None

    async def close(self, code: int = 1000) -> None:
        await self._conn.close(code)  # does nothing on a connection that is already closed


class _Stopped(Exception):
    """Raised inside run()'s TaskGroup to stop it once end() has started."""


class Relay:
    def __init__(
        self,
        browser: Socket,
        vapi: Socket,
        *,
        cap_s: float,
        on_event: OnEvent | None = None,
        ping_every_s: float = 20.0,
    ) -> None:
        self._browser = browser
        self._vapi = vapi
        self._cap_s = cap_s
        self._on_event = on_event
        self._ping_every_s = ping_every_s
        self._reason: str | None = None
        self._stopping = asyncio.Event()
        self._hang_up_task: asyncio.Task | None = None

    async def run(self) -> str:
        """Relay until the call ends, and return why: "cap", "browser_closed", "vapi_closed" or
        "plan_done". Raises if a task failed or run() was cancelled, after hanging up."""
        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(self._browser_to_vapi())
                tg.create_task(self._vapi_to_browser())
                tg.create_task(self._end_at_cap())
                tg.create_task(self._ping_browser())
                tg.create_task(self._stop_when_ending())
        except* _Stopped:
            pass
        finally:
            # Normally end() has already started the hang-up, and this waits for it to finish.
            # Otherwise a task failed or run() was cancelled, and this hangs up now.
            await self.end(_ERROR)
        return self._reason

    async def end(self, reason: str) -> None:
        """End the call. Only the first call counts. Every call waits until the hang-up is done.

        Safe to call from `on_event`, from the cap timer or from outside, before, during or after
        `run()`.
        """
        if self._hang_up_task is None:
            self._reason = reason
            self._stopping.set()
            self._hang_up_task = asyncio.create_task(self._hang_up(reason))
        # A caller inside run()'s TaskGroup is cancelled here as the group stops. The shield keeps
        # that from cancelling the hang-up with it.
        await asyncio.shield(self._hang_up_task)

    async def flush_browser(self) -> None:
        """Tell the browser to drop the interviewer audio it has queued but not yet played."""
        await self._send_browser(FLUSH)

    async def _hang_up(self, reason: str) -> None:
        # Vapi first: end-call is what stops the call, and the bill.
        await _attempt("send end-call to Vapi", self._vapi.send_text(END_CALL))
        ended = json.dumps({"type": "ended", "reason": reason})
        await _attempt("tell the browser the call ended", self._browser.send_text(ended))
        # Both at once, so a slow closing handshake on one side does not hold up the other.
        await asyncio.gather(
            _attempt("close the Vapi socket", self._vapi.close()),
            _attempt("close the browser socket", self._browser.close()),
        )
        log.info("relay ended: %s", reason)

    async def _stop_when_ending(self) -> None:
        await self._stopping.wait()
        raise _Stopped

    async def _end_at_cap(self) -> None:
        await asyncio.sleep(self._cap_s)
        await self.end("cap")

    async def _ping_browser(self) -> None:
        while True:
            await asyncio.sleep(self._ping_every_s)
            if not await self._send_browser(PING):
                return

    async def _browser_to_vapi(self) -> None:
        while (frame := await self._browser.recv()) is not None:
            if isinstance(frame, bytes):
                try:
                    await self._vapi.send_bytes(frame)
                except SocketClosed:
                    await self.end("vapi_closed")
                    return
            elif _is_end_request(frame):
                await self.end("browser_closed")
                return
            else:
                log.debug("dropped a browser text frame that is not an end request")
        await self.end("browser_closed")

    async def _vapi_to_browser(self) -> None:
        while (frame := await self._vapi.recv()) is not None:
            if isinstance(frame, bytes):
                if not await self._send_browser(frame):
                    return
            elif not await self._on_vapi_text(frame):
                return
        await self.end("vapi_closed")

    async def _on_vapi_text(self, text: str) -> bool:
        """Act on one of Vapi's text frames. Returns False once the relay is ending."""
        try:
            payload = json.loads(text)
        except ValueError:
            log.debug("dropped a Vapi text frame that is not JSON")
            return True
        event = parse_ws_event(payload)
        match event:
            case Hangup() | StatusUpdate(status="ended"):
                await self.end("vapi_closed")
                return False
            case Ignored():
                log.debug("dropped a Vapi %r frame", event.type)
            case UserInterrupted():
                # The flush goes out before the next audio frame is read, so the browser drops the
                # cut-off reply before it can play anything newer.
                if not await self._send_browser(FLUSH):
                    return False
                await self._emit(event)
            case _:
                await self._emit(event)
        return True

    async def _emit(self, event: LiveEvent) -> None:
        if self._on_event is None:
            return
        try:
            await self._on_event(event)
        except Exception as exc:
            # A failing handler must not cut the interview off, so relaying goes on. CancelledError
            # is not an Exception, so a relay that is stopping still stops here. The log names the
            # error and where it was raised, but not its message: a lookup such as
            # `sessions[call_id]` puts the call id in a KeyError's message (ruling R19).
            where = traceback.extract_tb(exc.__traceback__)[-1]
            log.error(
                "on_event failed for a %s event: %s at %s:%s; relaying goes on",
                type(event).__name__,
                type(exc).__name__,
                where.filename,
                where.lineno,
            )

    async def _send_browser(self, frame: bytes | str) -> bool:
        """Send one frame to the browser. If it has gone, end the relay and return False."""
        try:
            if isinstance(frame, bytes):
                await self._browser.send_bytes(frame)
            else:
                await self._browser.send_text(frame)
        except SocketClosed:
            await self.end("browser_closed")
            return False
        return True


def _is_end_request(text: str) -> bool:
    try:
        message = json.loads(text)
    except ValueError:
        return False
    return isinstance(message, dict) and message.get("type") == "end"


async def _attempt(what: str, step: Awaitable[None]) -> None:
    """Await one hang-up step. A failure is logged and the hang-up goes on to the next step."""
    try:
        await step
    except Exception as exc:
        # Only the type: a library's message could hold a URL (ruling R19).
        log.info("could not %s: %s", what, type(exc).__name__)
