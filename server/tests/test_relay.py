"""The relay between the browser's socket and Vapi's, tested with in-memory sockets.

`FakeSocket` stands in for either side. The test puts what the far end sends into `incoming` and
reads back what the relay sent from `sent`. The adapters are tested at the bottom against small
fakes of what they wrap: Starlette's real `WebSocket` over fake ASGI channels, and a stand-in for
a `websockets` connection that raises the library's real exceptions.

Vapi's text frames here have the shapes recorded in fixtures/vapi/ws_text_frames.jsonl.
"""

import asyncio
import json
import logging

import pytest
from starlette.websockets import WebSocket
from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK
from websockets.frames import Close

from mockingbird.relay import Relay, SocketClosed, StarletteSocket, WebsocketsSocket
from mockingbird.vapi_types import SpeechUpdate, StatusUpdate, UserInterrupted

# One 20 ms frame each way: 320 samples of s16le, 640 bytes. Different bytes, so a frame sent the
# wrong way, or changed on the way, cannot pass for the right one.
MIC_1, MIC_2, MIC_3 = (bytes([n]) * 640 for n in (1, 2, 3))
VOICE_1, VOICE_2, VOICE_3 = (bytes([n]) * 640 for n in (101, 102, 103))

# The recorded id of an ended call (fixtures/vapi/create_call_response.json). Used only to show
# that the relay's logs never carry what was inside a frame or a handler's error message.
CALL_ID = "01a15690-48f3-7331-a348-64c2e5c04886"


def frame(**fields) -> str:
    return json.dumps(fields)


ASSISTANT_STARTED = frame(type="speech-update", status="started", role="assistant", turn=1)
ASSISTANT_STOPPED = frame(type="speech-update", status="stopped", role="assistant", turn=1)
USER_STARTED = frame(type="speech-update", status="started", role="user", turn=2)
USER_STOPPED = frame(type="speech-update", status="stopped", role="user", turn=2)
USER_INTERRUPTED = frame(type="user-interrupted", turnId="turn-2")
IN_PROGRESS = frame(type="status-update", status="in-progress")
ENDED = frame(type="status-update", status="ended", endedReason="customer-ended-call")
HANGUP = frame(type="hangup")
TRANSCRIPT = frame(type="transcript", role="user", transcriptType="final", transcript="I led it.")
MODEL_OUTPUT = frame(type="model-output", output="Tell", turnId="turn-2")


def decode(sent: bytes | str):
    """A text frame as the dict it holds, so key order and spacing do not matter. Audio as is."""
    return json.loads(sent) if isinstance(sent, str) else sent


class FakeSocket:
    """One side of the relay, backed by two queues.

    Put frames into `incoming` for the relay to receive. None means the far end closed, and an
    exception is raised from `recv()`. What the relay sends lands in `sent`, and `actions` keeps
    every send and close in order. `log` is shared by the two sockets of one relay, so it keeps
    the order across both, each entry tagged with the socket's name.

    `dropped = True` makes sends fail the way a lost connection does, without the relay having
    seen a close yet. `stuck = True` makes every send and close wait until `unstick()`, the way
    they do to a peer whose network dropped with the send buffer full: uvicorn's send waits for
    the buffer to drain, which only happens once the kernel gives up on the connection.
    """

    def __init__(self, name: str = "", log: list | None = None) -> None:
        self.name = name
        self.log: list[tuple[str, str, object]] = [] if log is None else log
        self.incoming: asyncio.Queue[bytes | str | BaseException | None] = asyncio.Queue()
        self.sent: asyncio.Queue[bytes | str] = asyncio.Queue()
        self.actions: list[tuple[str, object]] = []
        self.closed = asyncio.Event()
        self.dropped = False
        self.stuck = False
        self._unstuck = asyncio.Event()

    async def recv(self) -> bytes | str | None:
        if self.closed.is_set():
            return None
        item = await self.incoming.get()
        if isinstance(item, BaseException):
            raise item
        return item

    async def send_bytes(self, b: bytes) -> None:
        await self._send(b)

    async def send_text(self, s: str) -> None:
        await self._send(s)

    async def _send(self, frame: bytes | str) -> None:
        await self._wait_while_stuck()
        if self.dropped or self.closed.is_set():
            raise SocketClosed
        self._record("send", decode(frame))
        self.sent.put_nowait(frame)

    async def close(self, code: int = 1000) -> None:
        await self._wait_while_stuck()
        self._record("close", code)
        self.closed.set()
        self.incoming.put_nowait(None)  # wakes a pending recv(), as closing a real socket does

    def unstick(self) -> None:
        self.stuck = False
        self._unstuck.set()

    async def _wait_while_stuck(self) -> None:
        if self.stuck:
            await self._unstuck.wait()

    def _record(self, kind: str, what: object) -> None:
        self.actions.append((kind, what))
        self.log.append((self.name, kind, what))

    async def next_sent(self):
        return decode(await asyncio.wait_for(self.sent.get(), 1.0))

    def frames(self) -> list:
        return [what for kind, what in self.actions if kind == "send"]


class EventLog:
    """An `on_event` callback that keeps every event it is given."""

    def __init__(self) -> None:
        self.seen: list = []
        self._queue: asyncio.Queue = asyncio.Queue()

    async def __call__(self, event) -> None:
        self.seen.append(event)
        self._queue.put_nowait(event)

    async def next(self):
        return await asyncio.wait_for(self._queue.get(), 1.0)


def make_relay(*, cap_s: float = 60.0, **kwargs) -> tuple[Relay, FakeSocket, FakeSocket]:
    log: list = []
    browser, vapi = FakeSocket("browser", log), FakeSocket("vapi", log)
    return Relay(browser, vapi, cap_s=cap_s, **kwargs), browser, vapi


async def start(relay: Relay) -> asyncio.Task:
    task = asyncio.create_task(relay.run())
    await asyncio.sleep(0)  # let run() start its tasks
    return task


async def finished(task: asyncio.Task) -> str:
    return await asyncio.wait_for(task, 1.0)


END_CALL = {"type": "end-call"}


def ended(reason: str) -> dict:
    return {"type": "ended", "reason": reason}


def assert_hung_up_in_order(log: list, reason: str) -> None:
    """The whole hang-up, across both sockets: Vapi hears `end-call` before the browser hears
    anything, because that is what stops the bill. Then the browser hears why, and both sockets
    close. The two closes run side by side, so their order is not pinned."""
    assert log[:2] == [("vapi", "send", END_CALL), ("browser", "send", ended(reason))]
    assert sorted(log[2:]) == [("browser", "close", 1000), ("vapi", "close", 1000)]


# --- Relaying ---


async def test_frames_flow_both_ways():
    relay, browser, vapi = make_relay()
    task = await start(relay)

    for mic, voice in [(MIC_1, VOICE_1), (MIC_2, VOICE_2), (MIC_3, VOICE_3)]:
        browser.incoming.put_nowait(mic)
        vapi.incoming.put_nowait(voice)

    assert [await vapi.next_sent() for _ in range(3)] == [MIC_1, MIC_2, MIC_3]
    assert [await browser.next_sent() for _ in range(3)] == [VOICE_1, VOICE_2, VOICE_3]
    await relay.end("plan_done")
    assert await finished(task) == "plan_done"


async def test_vapi_text_frames_not_forwarded():
    relay, browser, vapi = make_relay(on_event=EventLog())
    task = await start(relay)

    for text in [TRANSCRIPT, MODEL_OUTPUT, USER_STARTED, IN_PROGRESS, "not json", "[1, 2]"]:
        vapi.incoming.put_nowait(text)
    vapi.incoming.put_nowait(VOICE_1)

    assert await browser.next_sent() == VOICE_1
    await relay.end("plan_done")
    await finished(task)
    assert browser.frames() == [VOICE_1, ended("plan_done")]


async def test_browser_text_frames_never_reach_vapi():
    # Vapi takes control messages on this socket, so a page must not be able to send it any.
    relay, browser, vapi = make_relay()
    task = await start(relay)

    for text in [frame(type="end-call"), frame(type="add-message"), "not json", frame(kind="end")]:
        browser.incoming.put_nowait(text)
    browser.incoming.put_nowait(MIC_1)

    assert await vapi.next_sent() == MIC_1
    await relay.end("plan_done")
    await finished(task)
    assert vapi.frames() == [MIC_1, END_CALL]


async def test_browser_end_message_ends_with_browser_closed():
    relay, browser, vapi = make_relay()
    task = await start(relay)

    browser.incoming.put_nowait(frame(type="end"))

    assert await finished(task) == "browser_closed"
    assert_hung_up_in_order(vapi.log, "browser_closed")


async def test_browser_gets_a_ping_every_ping_every_s():
    relay, browser, _ = make_relay(ping_every_s=0.05)
    loop = asyncio.get_running_loop()
    began = loop.time()
    task = await start(relay)

    assert await browser.next_sent() == {"type": "ping"}
    assert await browser.next_sent() == {"type": "ping"}
    assert loop.time() - began >= 0.09  # two 0.05 s waits, with room for float rounding
    await relay.end("plan_done")
    await finished(task)


# --- Ending ---


async def test_cap_ends_call_and_tells_both_sides():
    relay, browser, vapi = make_relay(cap_s=0.2)
    loop = asyncio.get_running_loop()
    began = loop.time()

    reason = await asyncio.wait_for(relay.run(), 1.0)

    assert reason == "cap"
    assert loop.time() - began >= 0.19  # not before the cap, with room for float rounding
    assert_hung_up_in_order(vapi.log, "cap")


async def test_browser_disconnect_ends_vapi_call_within_one_second():
    # Review Focus 1: a closed tab must not leave the Vapi call running until its own limit.
    relay, browser, vapi = make_relay()
    task = await start(relay)
    loop = asyncio.get_running_loop()
    began = loop.time()

    browser.incoming.put_nowait(None)

    await asyncio.wait_for(vapi.closed.wait(), 1.0)
    assert loop.time() - began < 1.0
    assert vapi.actions == [("send", END_CALL), ("close", 1000)]
    assert await finished(task) == "browser_closed"
    assert_hung_up_in_order(vapi.log, "browser_closed")


async def test_browser_disconnect_ends_vapi_call_within_one_second_while_browser_sends_hang():
    # Review Focus 1 when the network has gone with the browser's send buffer full: a send to the
    # browser then waits until the kernel gives up on the connection, which takes minutes. The
    # Vapi side must not wait with it, however long each browser step is allowed to take.
    relay, browser, vapi = make_relay(hang_up_timeout_s=30.0)
    task = await start(relay)
    loop = asyncio.get_running_loop()
    began = loop.time()

    browser.stuck = True
    browser.incoming.put_nowait(None)

    await asyncio.wait_for(vapi.closed.wait(), 1.0)
    assert loop.time() - began < 1.0
    assert vapi.actions == [("send", END_CALL), ("close", 1000)]
    assert browser.actions == []  # the `ended` send is still waiting
    browser.unstick()
    assert await finished(task) == "browser_closed"
    assert browser.actions == [("send", ended("browser_closed")), ("close", 1000)]


async def test_browser_that_takes_nothing_cannot_hold_up_the_end(caplog):
    # The same full buffer, but it never drains. Each browser step gives up after
    # hang_up_timeout_s, so run() and every end() caller still return.
    caplog.set_level(logging.INFO, logger="mockingbird.relay")
    relay, browser, vapi = make_relay(hang_up_timeout_s=0.1)
    task = await start(relay)
    browser.stuck = True

    # The route's own end(), as once the wrap-up line has been spoken.
    await asyncio.wait_for(relay.end("plan_done"), 1.0)

    assert await finished(task) == "plan_done"
    assert vapi.actions == [("send", END_CALL), ("close", 1000)]
    assert browser.actions == []
    logged = [r.getMessage() for r in caplog.records]
    assert "could not tell the browser the call ended: TimeoutError" in logged
    assert "could not close the browser socket: TimeoutError" in logged


@pytest.mark.parametrize("before", [[], [MIC_1, MIC_2]], ids=["from-the-start", "after-audio"])
async def test_browser_going_quiet_ends_vapi_call(before):
    # Review Focus 1 when the network drops with no FIN or RST: no close ever reaches the relay,
    # and its sends to the browser only queue. But the browser streams mic frames every 20 ms
    # from the moment its socket opens, silence included, so frames stopping is the signal.
    relay, browser, vapi = make_relay(browser_timeout_s=0.2)
    task = await start(relay)
    for mic in before:
        browser.incoming.put_nowait(mic)

    await asyncio.wait_for(vapi.closed.wait(), 1.0)

    assert vapi.actions == [*(("send", mic) for mic in before), ("send", END_CALL), ("close", 1000)]
    assert await finished(task) == "browser_closed"


async def test_steady_mic_frames_keep_the_call_up():
    # The browser counts as gone only after browser_timeout_s with no frame at all, measured
    # from its last frame, not from the start of the call.
    relay, browser, vapi = make_relay(browser_timeout_s=0.2)
    task = await start(relay)

    for _ in range(8):  # 0.4 s in all, twice browser_timeout_s
        browser.incoming.put_nowait(MIC_1)
        await asyncio.sleep(0.05)

    assert END_CALL not in vapi.frames()
    await relay.end("plan_done")
    assert await finished(task) == "plan_done"


async def test_browser_that_stops_taking_frames_ends_vapi_call():
    # The other half of a dead connection: the relay's sends to the browser stop completing.
    # Mic frames keep arriving here, so the stuck send is the only thing that can end the call.
    relay, browser, vapi = make_relay(browser_timeout_s=0.2, hang_up_timeout_s=0.1)
    task = await start(relay)
    browser.stuck = True

    async def keep_talking() -> None:
        while True:
            browser.incoming.put_nowait(MIC_1)
            await asyncio.sleep(0.02)

    talking = asyncio.create_task(keep_talking())
    vapi.incoming.put_nowait(VOICE_1)
    try:
        await asyncio.wait_for(vapi.closed.wait(), 1.0)
    finally:
        talking.cancel()

    assert END_CALL in vapi.frames()
    assert await finished(task) == "browser_closed"
    assert browser.actions == []


async def test_browser_dropping_mid_send_ends_vapi_call():
    # The network drops: forwarding Vapi's audio fails before the browser's close is seen.
    relay, browser, vapi = make_relay()
    task = await start(relay)

    browser.dropped = True
    vapi.incoming.put_nowait(VOICE_1)

    await asyncio.wait_for(vapi.closed.wait(), 1.0)
    assert vapi.actions == [("send", END_CALL), ("close", 1000)]
    assert await finished(task) == "browser_closed"


async def test_vapi_socket_closing_ends_with_vapi_closed():
    relay, browser, vapi = make_relay()
    task = await start(relay)

    vapi.incoming.put_nowait(None)

    assert await finished(task) == "vapi_closed"
    assert browser.actions == [("send", ended("vapi_closed")), ("close", 1000)]
    assert vapi.closed.is_set()


async def test_end_is_idempotent():
    relay, browser, vapi = make_relay()
    task = await start(relay)

    await asyncio.gather(relay.end("plan_done"), relay.end("cap"))
    assert await finished(task) == "plan_done"
    await relay.end("vapi_closed")

    assert_hung_up_in_order(vapi.log, "plan_done")


async def test_end_from_on_event_stops_the_relay():
    # The route ends the call this way once the wrap-up line has been spoken.
    async def on_event(event) -> None:
        if event == SpeechUpdate(None, "assistant", "stopped"):
            await relay.end("plan_done")

    relay, browser, vapi = make_relay(on_event=on_event)
    task = await start(relay)

    vapi.incoming.put_nowait(ASSISTANT_STARTED)
    vapi.incoming.put_nowait(ASSISTANT_STOPPED)

    assert await finished(task) == "plan_done"
    assert_hung_up_in_order(vapi.log, "plan_done")


async def test_end_cancels_an_on_event_that_is_stuck(caplog):
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def on_event(event) -> None:
        started.set()
        try:
            await asyncio.Event().wait()  # never set, like a handler that hangs
        except asyncio.CancelledError:
            cancelled.set()
            raise

    relay, _, vapi = make_relay(on_event=on_event)
    task = await start(relay)
    vapi.incoming.put_nowait(USER_STARTED)
    await asyncio.wait_for(started.wait(), 1.0)

    await asyncio.wait_for(relay.end("plan_done"), 1.0)

    assert await finished(task) == "plan_done"
    assert cancelled.is_set()
    # A stopping relay is not an on_event failure.
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


async def test_unexpected_error_still_ends_the_vapi_call():
    relay, browser, vapi = make_relay()
    task = await start(relay)

    browser.incoming.put_nowait(ValueError("a bug in a pump"))

    with pytest.raises(ExceptionGroup) as raised:
        await finished(task)
    assert raised.group_contains(ValueError)
    assert vapi.actions == [("send", END_CALL), ("close", 1000)]
    assert browser.actions == [("send", ended("error")), ("close", 1000)]


async def test_cancelling_run_still_ends_the_vapi_call():
    # A server shutdown cancels run(). The Vapi call must not outlive it.
    relay, _, vapi = make_relay()
    task = await start(relay)

    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await finished(task)
    assert vapi.actions == [("send", END_CALL), ("close", 1000)]


# --- Vapi's live events ---


async def test_flush_browser_sends_flush():
    relay, browser, _ = make_relay()
    task = await start(relay)

    await relay.flush_browser()

    assert await browser.next_sent() == {"type": "flush"}
    await relay.end("plan_done")
    await finished(task)


async def test_user_interrupted_frame_flushes_before_later_audio():
    browser_frames_at_event = []
    events = EventLog()

    async def on_event(event) -> None:
        browser_frames_at_event.append(browser.frames())
        await events(event)

    relay, browser, vapi = make_relay(on_event=on_event)
    task = await start(relay)

    for item in [VOICE_1, USER_INTERRUPTED, VOICE_2]:
        vapi.incoming.put_nowait(item)

    assert [await browser.next_sent() for _ in range(3)] == [VOICE_1, {"type": "flush"}, VOICE_2]
    assert await events.next() == UserInterrupted(None)
    # The flush had already gone out when on_event heard about the interruption.
    assert browser_frames_at_event == [[VOICE_1, {"type": "flush"}]]
    await relay.end("plan_done")
    await finished(task)


@pytest.mark.parametrize("last_frame", [HANGUP, ENDED], ids=["hangup", "status-update-ended"])
async def test_hangup_frame_ends_with_vapi_closed(last_frame):
    events = EventLog()
    relay, browser, vapi = make_relay(on_event=events)
    task = await start(relay)

    vapi.incoming.put_nowait(last_frame)

    assert await finished(task) == "vapi_closed"
    assert browser.actions == [("send", ended("vapi_closed")), ("close", 1000)]
    assert vapi.closed.is_set()
    assert events.seen == []


async def test_events_reach_on_event_in_order():
    events = EventLog()
    relay, browser, vapi = make_relay(on_event=events)
    task = await start(relay)

    for item in [
        frame(type="assistant.started"),
        IN_PROGRESS,
        ASSISTANT_STARTED,
        VOICE_1,
        MODEL_OUTPUT,
        ASSISTANT_STOPPED,
        USER_STARTED,
        TRANSCRIPT,
        VOICE_2,
        USER_STOPPED,
    ]:
        vapi.incoming.put_nowait(item)

    expected = [
        StatusUpdate(None, "in-progress"),
        SpeechUpdate(None, "assistant", "started"),
        SpeechUpdate(None, "assistant", "stopped"),
        SpeechUpdate(None, "user", "started"),
        SpeechUpdate(None, "user", "stopped"),
    ]
    assert [await events.next() for _ in expected] == expected
    await relay.end("plan_done")
    await finished(task)
    # Frames Vapi sends that we do not act on (Ignored) are not events for the route.
    assert events.seen == expected
    assert browser.frames() == [VOICE_1, VOICE_2, ended("plan_done")]


async def test_on_event_error_is_logged_and_relaying_continues(caplog):
    events = EventLog()

    async def on_event(event) -> None:
        await events(event)
        if len(events.seen) == 1:
            raise RuntimeError("a bug in the route's handler")

    relay, browser, vapi = make_relay(on_event=on_event)
    task = await start(relay)

    for item in [USER_STARTED, VOICE_1, USER_STOPPED]:
        vapi.incoming.put_nowait(item)

    assert await browser.next_sent() == VOICE_1
    assert [await events.next(), await events.next()] == [
        SpeechUpdate(None, "user", "started"),
        SpeechUpdate(None, "user", "stopped"),
    ]
    await relay.end("plan_done")
    assert await finished(task) == "plan_done"
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1
    assert "SpeechUpdate" in errors[0].getMessage()


async def test_logs_never_hold_frame_contents(caplog):
    # Ruling R19: the call id is a credential. Socket frames do not carry it today, but the relay
    # logs only a frame's type, so it could not leak one that did.
    caplog.set_level(logging.DEBUG, logger="mockingbird.relay")

    async def on_event(event) -> None:
        sessions_by_call: dict = {}
        sessions_by_call[CALL_ID]  # a handler bug: the KeyError's message is the call id

    relay, browser, vapi = make_relay(on_event=on_event)
    task = await start(relay)

    for text in [
        frame(type="conversation-update", call={"id": CALL_ID}),
        frame(type="speech-update", status="started", role="user", callId=CALL_ID),
        f"not json {CALL_ID}",
        frame(type="some-new-type", url=f"wss://vapi.example/{CALL_ID}/transport"),
    ]:
        vapi.incoming.put_nowait(text)
    browser.incoming.put_nowait(f"not json {CALL_ID}")
    vapi.incoming.put_nowait(VOICE_1)

    assert await browser.next_sent() == VOICE_1
    await relay.end("plan_done")
    await finished(task)

    logged = [r.getMessage() + (r.exc_text or "") for r in caplog.records]
    assert any("conversation-update" in line for line in logged)  # types are logged
    assert any("KeyError" in line for line in logged)  # and so is the handler's failure
    assert not [line for line in logged if CALL_ID in line]


# --- Adapters ---


class FakeAsgi:
    """The ASGI server's end of one WebSocket: messages for the app, and what the app sent."""

    def __init__(self, *to_app: dict) -> None:
        self.to_app = [{"type": "websocket.connect"}, *to_app]
        self.from_app: list[dict] = []
        self.send_error: BaseException | None = None

    async def receive(self) -> dict:
        return self.to_app.pop(0)

    async def send(self, message: dict) -> None:
        if self.send_error is not None:
            raise self.send_error
        self.from_app.append(message)


async def accepted(asgi: FakeAsgi) -> StarletteSocket:
    """A StarletteSocket over a real Starlette WebSocket that the route has accepted."""
    ws = WebSocket({"type": "websocket"}, asgi.receive, asgi.send)
    await ws.accept()
    asgi.from_app.clear()  # the accept message
    return StarletteSocket(ws)


async def test_starlette_socket_recv_gives_bytes_then_text_then_none():
    socket = await accepted(
        FakeAsgi(
            {"type": "websocket.receive", "bytes": MIC_1},
            {"type": "websocket.receive", "bytes": None, "text": '{"type": "end"}'},
            {"type": "websocket.disconnect", "code": 1001},
        )
    )

    assert await socket.recv() == MIC_1
    assert await socket.recv() == '{"type": "end"}'
    assert await socket.recv() is None
    # Starlette raises if asked again after a disconnect. The adapter keeps saying None.
    assert await socket.recv() is None


async def test_starlette_socket_sends_binary_and_text():
    asgi = FakeAsgi()
    socket = await accepted(asgi)

    await socket.send_bytes(VOICE_1)
    await socket.send_text('{"type": "flush"}')

    assert asgi.from_app == [
        {"type": "websocket.send", "bytes": VOICE_1},
        {"type": "websocket.send", "text": '{"type": "flush"}'},
    ]


async def test_starlette_socket_send_after_a_drop_raises_socket_closed():
    asgi = FakeAsgi()
    socket = await accepted(asgi)
    # uvicorn raises ClientDisconnected, an OSError, when the browser's connection has gone.
    asgi.send_error = OSError("connection lost")

    with pytest.raises(SocketClosed):
        await socket.send_bytes(VOICE_1)
    with pytest.raises(SocketClosed):  # Starlette now refuses every send itself
        await socket.send_text('{"type": "ping"}')
    await socket.close()  # closing a socket that is already gone is not an error


async def test_starlette_socket_close_sends_the_code_once():
    asgi = FakeAsgi()
    socket = await accepted(asgi)

    await socket.close(1000)
    await socket.close(1000)

    assert asgi.from_app == [{"type": "websocket.close", "code": 1000, "reason": ""}]
    with pytest.raises(SocketClosed):
        await socket.send_text('{"type": "ping"}')


class FakeTransport:
    def __init__(self) -> None:
        self.aborted = False

    def abort(self) -> None:
        self.aborted = True


class FakeConnection:
    """Stands in for a websockets ClientConnection: the parts the adapter uses.

    `close_hangs = True` makes `close()` wait for good, as it does while Vapi's side of the
    closing handshake has not arrived.
    """

    def __init__(self, *incoming: bytes | str | BaseException) -> None:
        self.incoming = list(incoming)
        self.sent: list[bytes | str] = []
        self.close_codes: list[int] = []
        self.send_error: BaseException | None = None
        self.close_hangs = False
        self.transport = FakeTransport()

    async def recv(self) -> bytes | str:
        item = self.incoming.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    async def send(self, message: bytes | str) -> None:
        if self.send_error is not None:
            raise self.send_error
        self.sent.append(message)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.close_codes.append(code)
        if self.close_hangs:
            await asyncio.Event().wait()  # never set


# Vapi closes with no status code (1005) after a hangup. A network failure has no close frame.
VAPI_CLOSED = ConnectionClosedOK(Close(1005, ""), Close(1005, ""), True)
CONNECTION_LOST = ConnectionClosedError(None, None)


@pytest.mark.parametrize("closing", [VAPI_CLOSED, CONNECTION_LOST], ids=["closed", "lost"])
async def test_websockets_socket_recv_gives_frames_then_none(closing):
    socket = WebsocketsSocket(FakeConnection(VOICE_1, HANGUP, closing))

    assert await socket.recv() == VOICE_1
    assert await socket.recv() == HANGUP
    assert await socket.recv() is None


async def test_websockets_socket_sends_bytes_as_binary_and_str_as_text():
    # websockets picks the frame type from the Python type: bytes is binary, str is text.
    conn = FakeConnection()
    socket = WebsocketsSocket(conn)

    await socket.send_bytes(MIC_1)
    await socket.send_text('{"type": "end-call"}')

    assert conn.sent == [MIC_1, '{"type": "end-call"}']
    assert [type(sent) for sent in conn.sent] == [bytes, str]


@pytest.mark.parametrize("closing", [VAPI_CLOSED, CONNECTION_LOST], ids=["closed", "lost"])
async def test_websockets_socket_send_on_a_closed_connection_raises_socket_closed(closing):
    conn = FakeConnection()
    conn.send_error = closing
    socket = WebsocketsSocket(conn)

    with pytest.raises(SocketClosed) as raised:
        await socket.send_text('{"type": "end-call"}')
    assert str(raised.value) == ""
    assert raised.value.__cause__ is None and raised.value.__suppress_context__


async def test_websockets_socket_close_passes_the_code():
    conn = FakeConnection()

    await WebsocketsSocket(conn).close(1000)

    assert conn.close_codes == [1000]
    assert not conn.transport.aborted


async def test_websockets_socket_close_cut_short_drops_the_connection():
    # The relay gives each hang-up step a time limit. A close cut short while it waits for Vapi's
    # side of the closing handshake must not leave the connection open behind it.
    conn = FakeConnection()
    conn.close_hangs = True
    socket = WebsocketsSocket(conn)

    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.05):
            await socket.close(1000)

    assert conn.close_codes == [1000]
    assert conn.transport.aborted
