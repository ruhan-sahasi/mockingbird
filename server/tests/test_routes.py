"""The routes end to end: start an interview, run its call, answer Vapi's brain requests and
webhooks, and serve the report.

`create_app` gets a MemoryStore, a FakeVapi, a FakeStreamer and a FakeGrader. `websockets.connect`
is patched to return a FakeVapiConnection, so the real Relay and WebsocketsSocket run against it.
The TestClient runs the app's event loop in a thread of its own. A test hands Vapi's frames to
the fake through `client.portal` and waits until the relay has dealt with each one and is reading
again, so every assertion after it sees what that frame did.

Every test that opens the interview socket ends the call and waits for the route to finish its
clean-up before it leaves the `websocket_connect` block, because leaving the block cancels
whatever the route is still doing.
"""

import asyncio
import functools
import gc
import io
import json
import logging
import queue
import threading
import time
import wave
import weakref
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs

import pytest
import websockets
import yaml
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketState
from websockets.exceptions import ConnectionClosedOK

from mockingbird import routes_vapi, routes_ws
from mockingbird.app import create_app
from mockingbird.audio_store import LocalAudioStore
from mockingbird.brain import WRAP_UP_LINE, Brain
from mockingbird.models import Interview
from mockingbird.plan import DEMO, InterviewPlan, Question
from mockingbird.relay import Relay
from mockingbird.report import GradeResult, ItemGrade, QuestionGrade
from mockingbird.routes_api import QUESTIONS_PATH
from mockingbird.sessions import (
    LiveSession,
    SessionRegistry,
    purge_once,
    run_sweeper,
    sweep_once,
)
from mockingbird.sse import SSE_DONE
from mockingbird.stats import summarize_traces
from mockingbird.store import MemoryStore
from mockingbird.tracing import CallTracer, TurnTrace
from mockingbird.vapi_client import VapiCall, VapiError
from mockingbird.vapi_types import LlmRequest, SpeechUpdate, UserInterrupted

BRAIN_SECRET = "test-brain-secret"
WEBHOOK_SECRET = "test-webhook-secret"
LLM_PATH = f"/vapi/llm/{BRAIN_SECRET}/chat/completions"
WEBHOOK_PATH = f"/vapi/webhook/{WEBHOOK_SECRET}"
CALL_ID = "call_1"
SITE = "https://mockingbird.test"  # public_base_url in conftest's settings
SOCKET_URL = "ws://fake"
ENDED_LINE = "Sorry, this interview has ended."
DEFAULT_LINE = "Tell me more about that."
HANG = object()  # a script step that never finishes, like a Claude reply still streaming

MIC = bytes([1]) * 640  # one 20 ms frame from the browser, too quiet to count as speech
SPEECH = bytes([0, 40]) * 320  # one 20 ms frame of the candidate talking (every sample 10240)
VOICE = bytes([101]) * 640  # one 20 ms frame of the interviewer's voice


def frame(**fields) -> str:
    return json.dumps(fields)


# The shapes recorded in fixtures/vapi/ws_text_frames.jsonl.
ASSISTANT_STARTED = frame(type="speech-update", status="started", role="assistant", turn=1)
ASSISTANT_STOPPED = frame(type="speech-update", status="stopped", role="assistant", turn=1)
USER_STARTED = frame(type="speech-update", status="started", role="user", turn=2)
USER_STOPPED = frame(type="speech-update", status="stopped", role="user", turn=2)
USER_INTERRUPTED = frame(type="user-interrupted", turnId="turn-2")


class FakeStreamer:
    """Plays one script per Claude call: text chunks to yield, or HANG to wait until cancelled.
    With no script left it says DEFAULT_LINE. `hanging` gets the call's number each time a
    script reaches HANG, so a test thread can wait for it."""

    def __init__(self) -> None:
        self.scripts: list[list] = []
        self.calls = 0
        self.hanging: queue.Queue[int] = queue.Queue()
        self.cancelled = 0

    def stream(self, *, system, messages):
        self.calls += 1
        script = self.scripts.pop(0) if self.scripts else [DEFAULT_LINE]
        return self._play(script, self.calls)

    async def _play(self, script, number):
        try:
            for step in script:
                if step is HANG:
                    self.hanging.put(number)
                    await asyncio.Event().wait()
                else:
                    yield step
        except asyncio.CancelledError:
            self.cancelled += 1
            raise


class FakeAudioStore:
    """The recordings store. `put_error` makes every upload fail; `delete_errors` makes a delete
    fail for the prefixes named in it."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.put_error: Exception | None = None
        self.deleted: list[str] = []
        self.delete_errors: dict[str, Exception] = {}

    async def put_wav(self, path: str, data: bytes) -> None:
        if self.put_error is not None:
            raise self.put_error
        self.files[path] = data

    async def delete_prefix(self, prefix: str) -> None:
        if prefix in self.delete_errors:
            raise self.delete_errors[prefix]
        self.deleted.append(prefix)

    async def ensure_ready(self) -> None:
        return None


class FakeVapi:
    """Vapi's REST API. `delete_errors` makes delete_call fail for the calls named in it, and
    `create_delay` makes creating a call take that long, as it does against the real API."""

    def __init__(self) -> None:
        self.call_id = CALL_ID
        self.assistants: list[dict] = []
        self.error: Exception | None = None
        self.deleted: list[str] = []
        self.delete_errors: dict[str, Exception] = {}
        self.create_delay = 0.0

    async def create_call(self, assistant: dict) -> VapiCall:
        self.assistants.append(assistant)
        if self.create_delay:
            await asyncio.sleep(self.create_delay)
        if self.error is not None:
            raise self.error
        return VapiCall(self.call_id, SOCKET_URL)

    async def delete_call(self, call_id: str) -> None:
        if call_id in self.delete_errors:
            raise self.delete_errors[call_id]
        self.deleted.append(call_id)


class FakeVapiConnection:
    """Stands in for the `websockets` connection to Vapi's audio socket. An exception put into
    `incoming` is raised from `recv()`, like a bug in the library. `close_delay` makes closing
    take that long, like a slow closing handshake."""

    def __init__(self) -> None:
        self.incoming: asyncio.Queue[bytes | str | BaseException | None] = asyncio.Queue()
        self.sent: list[bytes | str] = []
        self.closed = False
        self.close_delay = 0.0
        self.transport = SimpleNamespace(abort=lambda: None)
        self._reading = asyncio.Event()

    async def recv(self) -> bytes | str:
        if self.incoming.empty():
            self._reading.set()
        item = await self.incoming.get()
        if item is None:
            raise ConnectionClosedOK(None, None)
        if isinstance(item, BaseException):
            raise item
        return item

    async def send(self, message: bytes | str) -> None:
        if self.closed:
            raise ConnectionClosedOK(None, None)
        self.sent.append(message)

    async def close(self, code: int = 1000) -> None:
        if self.close_delay:
            await asyncio.sleep(self.close_delay)
        if not self.closed:
            self.closed = True
            self.incoming.put_nowait(None)

    async def wait_reading(self) -> None:
        """Wait until the relay is waiting for Vapi's next frame."""
        await asyncio.wait_for(self._reading.wait(), 2.0)

    async def deliver(self, message: bytes | str) -> None:
        """Hand the relay one frame, and wait until it has dealt with it and is reading again."""
        self._reading.clear()
        self.incoming.put_nowait(message)
        await self.wait_reading()

    async def push(self, message: bytes | str) -> None:
        """Hand the relay one frame without waiting: for a frame that ends the call."""
        self.incoming.put_nowait(message)

    def text_sent(self) -> list[dict]:
        return [json.loads(m) for m in self.sent if isinstance(m, str)]


class FakeGrader:
    """Grades question 0 with one quote. `hold()` makes it wait until `release()`."""

    QUOTE = "I wrote the migration plan"

    def __init__(self) -> None:
        self._go = threading.Event()
        self._go.set()
        self.calls = 0

    def hold(self) -> None:
        self._go.clear()

    def release(self) -> None:
        self._go.set()

    async def grade(self, turns, questions) -> GradeResult:
        self.calls += 1
        while not self._go.is_set():
            await asyncio.sleep(0.01)
        item = ItemGrade(item="action", score=3, rationale="Named the steps.", quotes=[self.QUOTE])
        return GradeResult(questions=[QuestionGrade(question_idx=0, items=[item])])


@pytest.fixture
def fakes(monkeypatch):
    f = SimpleNamespace(
        store=MemoryStore(),
        vapi=FakeVapi(),
        streamer=FakeStreamer(),
        grader=FakeGrader(),
        audio=FakeAudioStore(),
        vapi_socket=FakeVapiConnection(),
        connects=[],
    )

    async def fake_connect(url, **kwargs):
        f.connects.append((url, kwargs))
        return f.vapi_socket

    monkeypatch.setattr(websockets, "connect", fake_connect)
    # The relay ends a call once the browser sends nothing for 2 s, and a test's browser sends
    # only what the test needs. Give it longer, so a slow machine cannot end a call early.
    monkeypatch.setattr(routes_ws, "Relay", functools.partial(Relay, browser_timeout_s=30.0))
    return f


@pytest.fixture
def app(settings, fakes):
    return create_app(
        settings,
        store=fakes.store,
        vapi=fakes.vapi,
        streamer_factory=lambda: fakes.streamer,
        grader=fakes.grader,
        audio_store=fakes.audio,
    )


@pytest.fixture
def client(app):
    # https, so the client sends the Secure visitor cookie back, and the Origin a browser on the
    # site sends with a start (routes_api.py refuses a start without it).
    with TestClient(app, base_url="https://testserver", headers={"origin": SITE}) as c:
        yield c


# --- helpers -----------------------------------------------------------------------------------


def start(client: TestClient, **headers) -> dict:
    r = client.post("/api/interviews", headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


def llm_body(user_text: str, spoken: str | None = None, call_id: str = CALL_ID) -> dict:
    """A custom-LLM request: Vapi's own system prompt, the last spoken interviewer line, and the
    candidate's answer so far."""
    messages = [{"role": "system", "content": "You are an assistant."}]
    if spoken is not None:
        messages.append({"role": "assistant", "content": spoken})
    messages.append({"role": "user", "content": user_text})
    return {"call": {"id": call_id}, "messages": messages}


def ask(client: TestClient, user_text: str, spoken: str | None = None, **kwargs):
    return client.post(LLM_PATH, json=llm_body(user_text, spoken, **kwargs))


def said(response) -> str:
    """The text an SSE reply carries, after checking it is a whole stream Vapi can read."""
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = [e for e in response.text.split("\n\n") if e]
    assert events[-1] == "data: [DONE]"
    chunks = [json.loads(e.removeprefix("data: ")) for e in events[:-1]]
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
    return "".join(c["choices"][0]["delta"].get("content", "") for c in chunks)


def in_background(call) -> tuple[threading.Thread, list]:
    """Run a blocking request in a thread. Its response lands in the returned list."""
    results: list = []
    thread = threading.Thread(target=lambda: results.append(call()))
    thread.start()
    return thread, results


def wait_until(condition, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out waiting"
        time.sleep(0.01)


def get(client: TestClient, store: MemoryStore, interview_id: str) -> Interview:
    return client.portal.call(store.get_interview, interview_id)


def closed_with(client: TestClient, ws_path: str) -> int:
    """Open the interview socket and return the code the route closed it with, failing if it
    stays open. The route must accept the socket first: one refused before that reaches a
    browser as a failed handshake with no code at all, so `websocket_connect` must not raise."""
    with client.websocket_connect(ws_path) as ws:
        thread, received = in_background(lambda: ws.receive())
        thread.join(2.0)
        assert not thread.is_alive(), "the socket was not closed"
    [message] = received
    assert message["type"] == "websocket.close"
    return message["code"]


def wait_cleaned_up(app, interview_id: str) -> None:
    wait_until(lambda: app.state.registry.by_interview(interview_id) is None)


def hang_up(app, ws, interview_id: str) -> None:
    """End the call from the browser, as its End button does, and wait for the clean-up."""
    ws.send_json({"type": "end"})
    assert ws.receive_json() == {"type": "ended", "reason": "browser_closed"}
    wait_cleaned_up(app, interview_id)


def assert_history_valid(history: list[dict]) -> None:
    assert history[0]["role"] == "user"
    for i, message in enumerate(history):
        if message["role"] == "system":
            assert history[i + 1]["role"] == "assistant"


def make_interview_record(id: str, status: str = "live") -> Interview:
    """An interview put straight into a store or a session, for tests below the routes."""
    return Interview(
        id=id,
        mode="demo",
        status=status,
        visitor_hash="v",
        ip_hash="ip",
        cap_s=300,
        questions=["Q"],
        created_at=datetime.now(UTC),
    )


def make_session(interview: Interview, streamer: FakeStreamer | None = None) -> LiveSession:
    """A session whose token was used, for a test below the routes."""
    plan = InterviewPlan(DEMO, [Question("q0", "Tell me about a time.", "ownership")])
    brain = Brain(plan, streamer or FakeStreamer(), time.monotonic)
    return LiveSession(interview, brain, token="", token_expires_at=0.0)


class FakeBrowserSocket:
    """Just enough of a Starlette WebSocket, already accepted, to run a call's route straight
    from a test: what StarletteSocket uses, and the app. The browser sends nothing."""

    def __init__(self, app) -> None:
        self.app = app
        self.client_state = WebSocketState.CONNECTED
        self.sent: list[dict] = []
        self.closed_with: int | None = None

    async def receive(self) -> dict:
        await asyncio.Event().wait()  # never set
        raise AssertionError("unreachable")

    async def send(self, message: dict) -> None:
        self.sent.append(message)

    async def close(self, code: int = 1000) -> None:
        self.closed_with = code

    def text_sent(self) -> list[dict]:
        return [json.loads(m["text"]) for m in self.sent if m.get("text") is not None]


async def collect(events) -> list[str]:
    return [event async for event in events]


# --- POST /api/interviews ----------------------------------------------------------------------


def test_start_returns_ws_path_and_sets_cookie(client, fakes):
    r = client.post("/api/interviews")

    assert r.status_code == 201
    body = r.json()
    assert set(body) == {"interview_id", "ws_path", "cap_s"}
    assert body["cap_s"] == 300
    path, _, query = body["ws_path"].partition("?")
    assert path == f"/ws/interview/{body['interview_id']}"
    assert len(parse_qs(query)["token"][0]) >= 32

    cookie = r.headers["set-cookie"]
    assert cookie.startswith("mb_visitor=")
    for attribute in ("HttpOnly", "Secure", "SameSite=Lax", "Max-Age=31536000"):
        assert attribute in cookie

    interview = get(client, fakes.store, body["interview_id"])
    assert (interview.status, interview.mode, interview.cap_s) == ("created", "demo", 300)
    bank = {q["text"] for q in yaml.safe_load(QUESTIONS_PATH.read_text())}
    assert len(interview.questions) == 1  # a demo asks one main question
    assert interview.questions[0] in bank


def test_an_owner_start_is_a_full_interview_with_three_questions(client, fakes, settings):
    body = start(client, **{"X-Owner-Key": settings.owner_key})

    interview = get(client, fakes.store, body["interview_id"])
    assert (interview.mode, interview.cap_s, body["cap_s"]) == ("full", 1200, 1200)
    assert len(set(interview.questions)) == 3


# --- the time caps -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("owner", "cap_s", "minutes"), [(False, 120, 2), (True, 90, 1)], ids=["demo", "full"]
)
def test_the_cap_from_the_settings_reaches_the_relay_vapi_and_the_plan(
    settings, fakes, monkeypatch, owner, cap_s, minutes
):
    # The three cost controls for one call: the relay ends it at the cap, Vapi's own limit is the
    # backstop 30 s later, and the plan's clock wraps up before the cap. Each must follow
    # DEMO_CAP_S or FULL_CAP_S, not the default 300 or 1200.
    relays: list[float] = []

    def recording_relay(*args, **kwargs):
        relays.append(kwargs["cap_s"])
        return Relay(*args, browser_timeout_s=30.0, **kwargs)

    monkeypatch.setattr(routes_ws, "Relay", recording_relay)
    capped = settings.model_copy(update={"demo_cap_s": 120, "full_cap_s": 90})
    app = create_app(
        capped,
        store=fakes.store,
        vapi=fakes.vapi,
        streamer_factory=lambda: fakes.streamer,
        grader=fakes.grader,
    )
    headers = {"X-Owner-Key": settings.owner_key} if owner else {}

    with TestClient(app, base_url="https://testserver", headers={"origin": SITE}) as client:
        body = start(client, **headers)
        assert body["cap_s"] == cap_s
        assert get(client, fakes.store, body["interview_id"]).cap_s == cap_s

        with client.websocket_connect(body["ws_path"]) as ws:
            client.portal.call(fakes.vapi_socket.wait_reading)
            [assistant] = fakes.vapi.assistants
            assert assistant["maxDurationSeconds"] == cap_s + 30
            # The opening line states the plan's clock, which is the cap.
            assert f"This is a {minutes} minute behavioral interview" in assistant["firstMessage"]
            assert relays == [cap_s]
            hang_up(app, ws, body["interview_id"])


# --- WS /ws/interview/{id} ---------------------------------------------------------------------


def test_ws_runs_the_call_and_cleans_up_after_it(client, app, fakes):
    body = start(client)
    interview_id = body["interview_id"]

    with client.websocket_connect(body["ws_path"]) as ws:
        client.portal.call(fakes.vapi_socket.wait_reading)

        live = get(client, fakes.store, interview_id)
        assert live.status == "live"
        assert live.vapi_call_id == CALL_ID  # kept server-side, so the webhook can find it
        [assistant] = fakes.vapi.assistants
        assert assistant["model"]["url"] == f"https://mockingbird.test/vapi/llm/{BRAIN_SECRET}"
        assert (
            assistant["server"]["url"] == f"https://mockingbird.test/vapi/webhook/{WEBHOOK_SECRET}"
        )
        assert assistant["maxDurationSeconds"] == 300 + 30
        assert assistant["firstMessage"].startswith("Hi, I'm Mockingbird")
        assert assistant["firstMessage"].endswith(live.questions[0])
        assert fakes.connects == [(SOCKET_URL, {"close_timeout": 2.0})]

        # A live interview has no report on its way yet: only an ended one does.
        assert client.get(f"/api/interviews/{interview_id}/report").status_code == 404

        # Audio goes through both ways.
        ws.send_bytes(MIC)
        wait_until(lambda: MIC in fakes.vapi_socket.sent)
        client.portal.call(fakes.vapi_socket.deliver, VOICE)
        assert ws.receive_bytes() == VOICE

        hang_up(app, ws, interview_id)

    ended = get(client, fakes.store, interview_id)
    assert ended.status in ("ended", "graded")
    assert ended.ended_reason == "browser_closed"
    assert ended.duration_s == 0
    assert fakes.vapi_socket.text_sent()[-1] == {"type": "end-call"}
    assert fakes.vapi_socket.closed

    turns = client.portal.call(fakes.store.list_turns, interview_id)
    assert [(t.idx, t.speaker) for t in turns] == [(0, "interviewer")]  # the opening line
    assert turns[0].text == assistant["firstMessage"]

    # Nobody answered, so the report says so without asking the grader.
    wait_until(lambda: client.get(f"/api/interviews/{interview_id}/report").status_code == 200)
    report = client.get(f"/api/interviews/{interview_id}/report").json()
    assert report["status"] == "no_answers"
    assert fakes.grader.calls == 0
    assert get(client, fakes.store, interview_id).status == "graded"

    # Vapi asking the brain about the call now gets the fixed line (Review Focus 2).
    assert said(ask(client, "Are you still there?")) == ENDED_LINE


def decode_wav(data: bytes) -> tuple[int, int, int, bytes]:
    with wave.open(io.BytesIO(data)) as w:
        return w.getframerate(), w.getnchannels(), w.getsampwidth(), w.readframes(w.getnframes())


def test_a_call_records_and_uploads_both_tracks(client, app, fakes):
    body = start(client)
    interview_id = body["interview_id"]

    with client.websocket_connect(body["ws_path"]) as ws:
        client.portal.call(fakes.vapi_socket.wait_reading)
        for _ in range(5):
            ws.send_bytes(MIC)
        wait_until(lambda: fakes.vapi_socket.sent.count(MIC) == 5)
        for _ in range(3):
            client.portal.call(fakes.vapi_socket.deliver, VOICE)
            assert ws.receive_bytes() == VOICE
        hang_up(app, ws, interview_id)

    wait_until(lambda: get(client, fakes.store, interview_id).recording is not None)
    base = f"interviews/{interview_id}"
    assert sorted(fakes.audio.files) == [f"{base}/candidate.wav", f"{base}/interviewer.wav"]
    assert decode_wav(fakes.audio.files[f"{base}/candidate.wav"]) == (16000, 1, 2, MIC * 5)
    assert decode_wav(fakes.audio.files[f"{base}/interviewer.wav"]) == (16000, 1, 2, VOICE * 3)
    recording = get(client, fakes.store, interview_id).recording
    assert recording["status"] == "uploaded"
    assert recording["candidate"]["frames"] == 5
    assert recording["interviewer"]["frames"] == 3


def test_mic_frames_sent_before_the_call_went_live_do_not_shift_the_candidate_track(
    client, app, fakes
):
    # The page sends mic frames as soon as its socket opens, while the call is still being set
    # up. They wait in the socket and reach the relay all at once when it starts, so the first
    # one arrives near 0 ms whatever it holds. The track is dated from its newest frame instead.
    fakes.vapi.create_delay = 0.3
    body = start(client)
    interview_id = body["interview_id"]

    with client.websocket_connect(body["ws_path"]) as ws:
        for _ in range(5):
            ws.send_bytes(MIC)  # before the call is live
        wait_until(lambda: fakes.vapi_socket.sent.count(MIC) == 5)
        hang_up(app, ws, interview_id)

    wait_until(lambda: get(client, fakes.store, interview_id).recording is not None)
    candidate = get(client, fakes.store, interview_id).recording["candidate"]
    assert candidate["frames"] == 5
    assert candidate["start_ms"] == pytest.approx(candidate["last_frame_ms"] - 4 * 20)
    # The five 20 ms frames hold 100 ms of audio, captured before the burst that delivered them.
    assert candidate["start_ms"] < candidate["first_frame_ms"]


def test_a_finished_calls_audio_is_freed_without_waiting_for_the_garbage_collector(
    client, app, fakes
):
    # The session and its relay point at each other (the relay's live events read the session),
    # and both reach the recorder. Unless the clean-up breaks that loop, a finished call's audio,
    # tens of MB, stays in memory until Python's next full garbage collection. So this runs with
    # the collector off: only reference counting can free the recorder.
    body = start(client)
    interview_id = body["interview_id"]
    gc.disable()
    try:
        with client.websocket_connect(body["ws_path"]) as ws:
            client.portal.call(fakes.vapi_socket.wait_reading)
            recorder = weakref.ref(app.state.registry.by_interview(interview_id).recorder)
            for _ in range(5):
                ws.send_bytes(MIC)
            wait_until(lambda: fakes.vapi_socket.sent.count(MIC) == 5)
            client.portal.call(fakes.vapi_socket.deliver, VOICE)
            assert ws.receive_bytes() == VOICE
            hang_up(app, ws, interview_id)

        wait_until(lambda: get(client, fakes.store, interview_id).recording is not None)
        wait_until(lambda: not app.state.background)
        wait_until(lambda: recorder() is None)
    finally:
        gc.enable()
    assert get(client, fakes.store, interview_id).recording["status"] == "uploaded"


def test_upload_failure_still_builds_the_report(client, app, fakes, caplog):
    caplog.set_level(logging.DEBUG)
    fakes.audio.put_error = RuntimeError("storage said secret-detail")
    body = start(client)
    interview_id = body["interview_id"]

    with client.websocket_connect(body["ws_path"]) as ws:
        client.portal.call(fakes.vapi_socket.wait_reading)
        ws.send_bytes(MIC)
        wait_until(lambda: MIC in fakes.vapi_socket.sent)
        hang_up(app, ws, interview_id)

    report_url = f"/api/interviews/{interview_id}/report"
    wait_until(lambda: client.get(report_url).status_code == 200)
    wait_until(lambda: get(client, fakes.store, interview_id).recording is not None)
    assert get(client, fakes.store, interview_id).recording == {"status": "upload_failed"}
    assert "RuntimeError" in caplog.text
    assert "secret-detail" not in caplog.text


def test_a_call_that_never_went_live_has_no_recording(client, app, fakes):
    fakes.vapi.error = VapiError("Vapi returned 503: try again")
    body = start(client)
    closed_with(client, body["ws_path"])
    wait_cleaned_up(app, body["interview_id"])
    assert get(client, fakes.store, body["interview_id"]).recording is None
    assert fakes.audio.files == {}


def everything_under(folder: Path) -> list[str]:
    if not folder.exists():
        return []
    return sorted(str(path.relative_to(folder)) for path in folder.rglob("*"))


def test_a_test_call_records_into_its_tmp_folder_and_never_into_the_repo(settings, fakes, tmp_path):
    # Without an injected audio store the app keeps recordings in RECORDINGS_DIR, "recordings"
    # under the working directory by default. The settings fixture moves that into tmp_path, so
    # running the tests never leaves WAV files in the repo (ruling P2-R22).
    repo = Path(__file__).resolve().parents[2]
    repo_folders = {repo / "recordings", repo / "server" / "recordings", Path.cwd() / "recordings"}
    before = {folder: everything_under(folder) for folder in repo_folders}
    app = create_app(
        settings,
        store=fakes.store,
        vapi=fakes.vapi,
        streamer_factory=lambda: fakes.streamer,
        grader=fakes.grader,
    )

    with TestClient(app, base_url="https://testserver", headers={"origin": SITE}) as client:
        audio = app.state.audio_store
        assert isinstance(audio, LocalAudioStore)
        assert audio._root.is_relative_to(tmp_path.resolve())
        body = start(client)
        interview_id = body["interview_id"]
        with client.websocket_connect(body["ws_path"]) as ws:
            client.portal.call(fakes.vapi_socket.wait_reading)
            ws.send_bytes(MIC)
            wait_until(lambda: MIC in fakes.vapi_socket.sent)
            hang_up(app, ws, interview_id)
        wait_until(lambda: get(client, fakes.store, interview_id).recording is not None)
        assert get(client, fakes.store, interview_id).recording["status"] == "uploaded"

    written = tmp_path / "recordings" / "interviews" / interview_id
    assert sorted(path.name for path in written.iterdir()) == ["candidate.wav", "interviewer.wav"]
    assert {folder: everything_under(folder) for folder in repo_folders} == before


def test_the_interview_is_ended_before_the_browser_hears_the_call_ended(client, app, fakes):
    # The browser asks for the report as soon as it hears the call ended, and only an ended
    # interview has one on its way. A slow close of Vapi's socket holds the rest of the clean-up
    # back, so the ask lands before it.
    fakes.vapi_socket.close_delay = 0.8
    body = start(client)
    interview_id = body["interview_id"]

    with client.websocket_connect(body["ws_path"]) as ws:
        client.portal.call(fakes.vapi_socket.wait_reading)

        ws.send_json({"type": "end"})
        assert ws.receive_json() == {"type": "ended", "reason": "browser_closed"}
        report = client.get(f"/api/interviews/{interview_id}/report")
        assert app.state.registry.by_interview(interview_id) is not None  # clean-up still to come

        assert (report.status_code, report.json()) == (202, {"status": "grading"})
        ended = get(client, fakes.store, interview_id)
        assert (ended.status, ended.ended_reason, ended.duration_s) == (
            "ended",
            "browser_closed",
            0,
        )
        wait_cleaned_up(app, interview_id)


def test_ws_token_reuse_closes_4401(client, app, fakes):
    body = start(client)

    with client.websocket_connect(body["ws_path"]) as ws:
        client.portal.call(fakes.vapi_socket.wait_reading)

        # A second tab with the same link, while the call is live.
        assert closed_with(client, body["ws_path"]) == 4401
        assert len(fakes.vapi.assistants) == 1  # no second call was created

        hang_up(app, ws, body["interview_id"])

    # The same link replayed after the call.
    assert closed_with(client, body["ws_path"]) == 4401


def test_the_close_codes_are_the_ones_the_browser_expects():
    # web/src/interviewSession.test.ts reads the same file and maps each code to its message.
    wire = json.loads((Path(__file__).parent / "fixtures" / "wire_protocol.json").read_text())
    assert routes_ws.TOKEN_REJECTED == wire["close_codes"]["token_rejected"]
    assert routes_ws.INTERNAL_ERROR == wire["close_codes"]["call_not_started"]


@pytest.mark.parametrize("wrong", ["token", "interview"])
def test_ws_with_a_wrong_token_or_interview_closes_4401(client, app, fakes, wrong):
    body = start(client)
    path, _, query = body["ws_path"].partition("?")
    if wrong == "token":
        ws_path = f"{path}?token=not-the-token"
    else:
        ws_path = f"/ws/interview/not-an-interview?{query}"

    assert closed_with(client, ws_path) == 4401
    assert fakes.vapi.assistants == []
    # The real link still works afterwards: a wrong guess does not burn the token.
    assert app.state.registry.by_interview(body["interview_id"]).token


def test_ws_with_an_expired_token_closes_4401(client, app, fakes):
    body = start(client)
    registry = app.state.registry
    real_clock = registry.clock
    registry.clock = lambda: real_clock() + 61

    assert closed_with(client, body["ws_path"]) == 4401
    assert fakes.vapi.assistants == []


def test_a_call_that_cannot_be_created_closes_the_socket_and_frees_the_visitor(
    client, app, fakes, caplog
):
    caplog.set_level(logging.DEBUG)
    fakes.vapi.error = VapiError("Vapi returned 500: try again")
    body = start(client)

    assert closed_with(client, body["ws_path"]) == 1011
    wait_cleaned_up(app, body["interview_id"])

    interview = get(client, fakes.store, body["interview_id"])
    assert (interview.status, interview.ended_reason) == ("abandoned", "error")
    # Vapi's reason is already scrubbed of secrets, and it is what a first deploy needs to see.
    assert "failed: VapiError: Vapi returned 500: try again" in caplog.text
    # No call ran, so the visitor's demo for today is not spent.
    fakes.vapi.error = None
    assert client.post("/api/interviews").status_code == 201


def test_a_failed_connect_to_vapi_logs_no_url(client, app, fakes, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)

    async def failing_connect(url, **kwargs):
        raise OSError(f"could not reach {url}")

    monkeypatch.setattr(websockets, "connect", failing_connect)
    body = start(client)

    assert closed_with(client, body["ws_path"]) == 1011
    wait_cleaned_up(app, body["interview_id"])

    assert get(client, fakes.store, body["interview_id"]).status == "abandoned"
    assert SOCKET_URL not in caplog.text
    assert CALL_ID not in caplog.text


def test_a_relay_that_fails_mid_call_still_ends_saves_and_grades_the_interview(
    client, app, fakes, caplog
):
    # run() raises an ExceptionGroup when one of its tasks fails. The interview must still end,
    # keep its turns and get its report.
    caplog.set_level(logging.DEBUG)
    body = start(client)
    interview_id = body["interview_id"]
    answer = "I wrote the migration plan and walked the team through it."

    with client.websocket_connect(body["ws_path"]) as ws:
        client.portal.call(fakes.vapi_socket.wait_reading)
        said(ask(client, answer))
        client.portal.call(fakes.vapi_socket.deliver, ASSISTANT_STARTED)

        client.portal.call(fakes.vapi_socket.push, RuntimeError("a bug in the socket library"))

        assert ws.receive_json() == {"type": "ended", "reason": "error"}
        wait_cleaned_up(app, interview_id)

    assert "failed: ExceptionGroup" in caplog.text
    assert fakes.vapi_socket.text_sent()[-1] == {"type": "end-call"}
    ended = get(client, fakes.store, interview_id)
    assert ended.status in ("ended", "graded")
    assert ended.ended_reason == "error"
    turns = client.portal.call(fakes.store.list_turns, interview_id)
    assert [(t.speaker, t.text) for t in turns][1:] == [
        ("candidate", answer),
        ("interviewer", DEFAULT_LINE),
    ]
    report_url = f"/api/interviews/{interview_id}/report"
    wait_until(lambda: client.get(report_url).status_code == 200)
    assert client.get(report_url).json()["status"] == "graded"
    assert fakes.grader.calls == 1


async def test_a_call_cancelled_mid_relay_is_still_cleaned_up(app, fakes):
    # A server shutdown cancels the route while relay.run() is running. CancelledError is not an
    # Exception, so only a `finally` still ends the interview, saves its turns and grades it.
    store = fakes.store
    interview = make_interview_record("i1", status="created")
    await store.create_interview(interview)
    session = make_session(interview, fakes.streamer)
    app.state.registry.add(session)
    browser = FakeBrowserSocket(app)

    call = asyncio.create_task(routes_ws._run_call(browser, session))
    await fakes.vapi_socket.wait_reading()
    assert (await store.get_interview("i1")).status == "live"

    call.cancel()
    await asyncio.wait({call}, timeout=2.0)

    assert call.cancelled()
    assert fakes.vapi_socket.text_sent()[-1] == {"type": "end-call"}
    assert browser.text_sent()[-1] == {"type": "ended", "reason": "error"}
    ended = await store.get_interview("i1")
    assert ended.status in ("ended", "graded")
    assert (ended.ended_reason, ended.duration_s) == ("error", 0)
    assert [t.speaker for t in await store.list_turns("i1")] == ["interviewer"]  # the opening
    assert app.state.registry.by_interview("i1") is None
    for _ in range(100):  # the report is graded in the background
        if await store.get_report("i1") is not None:
            break
        await asyncio.sleep(0.01)
    assert (await store.get_report("i1"))["status"] == "no_answers"


# --- the relay's live events -------------------------------------------------------------------


def test_speech_started_event_commits_draft(client, app, fakes):
    body = start(client)
    interview_id = body["interview_id"]
    answer = "I wrote the plan and shipped it."

    with client.websocket_connect(body["ws_path"]) as ws:
        client.portal.call(fakes.vapi_socket.wait_reading)
        brain = app.state.registry.by_interview(interview_id).brain

        assert said(ask(client, answer)) == DEFAULT_LINE
        assert [t.speaker for t in brain.turns] == ["interviewer"]  # a draft is not a turn yet

        # The candidate starting to speak commits nothing.
        client.portal.call(fakes.vapi_socket.deliver, USER_STARTED)
        assert [t.speaker for t in brain.turns] == ["interviewer"]

        client.portal.call(fakes.vapi_socket.deliver, ASSISTANT_STARTED)
        assert [(t.speaker, t.text) for t in brain.turns][1:] == [("candidate", answer)]

        hang_up(app, ws, interview_id)

    # The reply that was being spoken is written down when the call ends.
    turns = client.portal.call(fakes.store.list_turns, interview_id)
    assert [(t.speaker, t.text) for t in turns][1:] == [
        ("candidate", answer),
        ("interviewer", DEFAULT_LINE),
    ]


@pytest.mark.parametrize("reply", ["written", "still_streaming"])
def test_an_answer_the_call_ends_on_is_saved_and_graded(client, app, fakes, reply):
    # The visitor answers and presses End before the interviewer replies: Vapi has asked the
    # brain for a reply, and never started speaking it. The answer must still be graded.
    if reply == "still_streaming":
        fakes.streamer.scripts = [["Tell me", HANG]]
    body = start(client)
    interview_id = body["interview_id"]
    answer = f"{FakeGrader.QUOTE} and walked the team through it."

    with client.websocket_connect(body["ws_path"]) as ws:
        client.portal.call(fakes.vapi_socket.wait_reading)
        if reply == "written":
            assert said(ask(client, answer)) == DEFAULT_LINE
        else:
            thread, _ = in_background(lambda: ask(client, answer))
            assert fakes.streamer.hanging.get(timeout=2) == 1
        hang_up(app, ws, interview_id)
    if reply == "still_streaming":
        thread.join(2)
        assert fakes.streamer.cancelled == 1

    turns = client.portal.call(fakes.store.list_turns, interview_id)
    assert [(t.speaker, t.text, t.question_idx) for t in turns][1:] == [("candidate", answer, 0)]
    report_url = f"/api/interviews/{interview_id}/report"
    wait_until(lambda: client.get(report_url).status_code == 200)
    report = client.get(report_url).json()
    assert report["status"] == "graded"
    assert report["questions"][0]["items"][0]["quotes"] == [FakeGrader.QUOTE]
    assert fakes.grader.calls == 1


def test_assistant_stopped_after_committed_wrap_up_ends_call(client, app, fakes):
    body = start(client)
    interview_id = body["interview_id"]

    with client.websocket_connect(body["ws_path"]) as ws:
        client.portal.call(fakes.vapi_socket.wait_reading)
        session = app.state.registry.by_interview(interview_id)

        # A demo asks two follow-ups. Their speech stopping does not end the call.
        spoken = None
        for answer in ("First answer.", "Second answer."):
            assert said(ask(client, answer, spoken)) == DEFAULT_LINE
            client.portal.call(fakes.vapi_socket.deliver, ASSISTANT_STARTED)
            client.portal.call(fakes.vapi_socket.deliver, ASSISTANT_STOPPED)
            spoken = DEFAULT_LINE
        assert said(ask(client, "Third answer.", spoken)) == WRAP_UP_LINE

        # The wrap-up is only drafted, so the end of the last line's speech is not the end.
        client.portal.call(fakes.vapi_socket.deliver, ASSISTANT_STOPPED)
        assert not session.brain.finished

        client.portal.call(fakes.vapi_socket.deliver, ASSISTANT_STARTED)
        assert session.brain.finished
        # Vapi asking for more after the wrap-up was spoken gets the fixed line.
        assert said(ask(client, "Thanks, bye!", WRAP_UP_LINE)) == ENDED_LINE

        client.portal.call(fakes.vapi_socket.push, ASSISTANT_STOPPED)
        assert ws.receive_json() == {"type": "ended", "reason": "plan_done"}
        wait_cleaned_up(app, interview_id)

    ended = get(client, fakes.store, interview_id)
    assert ended.ended_reason == "plan_done"
    turns = client.portal.call(fakes.store.list_turns, interview_id)
    assert turns[-1].speaker == "interviewer"
    assert turns[-1].text == WRAP_UP_LINE


def test_overlapping_llm_requests_commit_only_the_spoken_one(client, app, fakes):
    # Vapi sends a request as each part of the answer comes in, and speaks only the last reply.
    fakes.streamer.scripts = [[HANG], [HANG]]
    partials = ["I led", "I led the migration", "I led the migration and it shipped."]
    body = start(client)
    interview_id = body["interview_id"]

    with client.websocket_connect(body["ws_path"]) as ws:
        client.portal.call(fakes.vapi_socket.wait_reading)
        brain = app.state.registry.by_interview(interview_id).brain

        first, first_reply = in_background(lambda: ask(client, partials[0]))
        assert fakes.streamer.hanging.get(timeout=2) == 1
        second, second_reply = in_background(lambda: ask(client, partials[1]))
        assert fakes.streamer.hanging.get(timeout=2) == 2
        assert said(ask(client, partials[2])) == DEFAULT_LINE
        first.join(2)
        second.join(2)

        assert fakes.streamer.cancelled == 2
        assert said(first_reply[0]) == said(second_reply[0]) == ""  # cut off, and ended cleanly

        client.portal.call(fakes.vapi_socket.deliver, ASSISTANT_STARTED)
        assert [(t.speaker, t.text) for t in brain.turns][1:] == [("candidate", partials[2])]

        # The next request writes the spoken reply into the history, and only that one.
        said(ask(client, "Next answer.", DEFAULT_LINE))
        history = brain.history
        assert_history_valid(history)
        assert [m["content"] for m in history if m["role"] == "user"][1:] == [partials[2]]
        assert [m["content"] for m in history if m["role"] == "assistant"][1:] == [DEFAULT_LINE]

        hang_up(app, ws, interview_id)


# --- latency tracing ---------------------------------------------------------------------------


def test_a_spoken_reply_is_traced_hop_by_hop_and_stored(client, app, fakes):
    body = start(client)
    interview_id = body["interview_id"]

    with client.websocket_connect(body["ws_path"]) as ws:
        client.portal.call(fakes.vapi_socket.wait_reading)
        # Vapi speaks the opening line itself, so no request was traced for it.
        client.portal.call(fakes.vapi_socket.deliver, ASSISTANT_STARTED)
        client.portal.call(fakes.vapi_socket.deliver, VOICE)
        assert ws.receive_bytes() == VOICE
        client.portal.call(fakes.vapi_socket.deliver, ASSISTANT_STOPPED)

        ws.send_bytes(SPEECH)  # the end of the candidate's answer
        ws.send_bytes(MIC)
        wait_until(lambda: MIC in fakes.vapi_socket.sent)
        client.portal.call(fakes.vapi_socket.deliver, USER_STOPPED)
        assert said(ask(client, "I wrote the plan and shipped it.")) == DEFAULT_LINE
        client.portal.call(fakes.vapi_socket.deliver, ASSISTANT_STARTED)
        client.portal.call(fakes.vapi_socket.deliver, VOICE)
        assert ws.receive_bytes() == VOICE
        # Vapi says the candidate talked over the reply at once, which stands in for a cut-in.
        client.portal.call(fakes.vapi_socket.deliver, USER_INTERRUPTED)
        assert ws.receive_json() == {"type": "flush"}
        # The browser times the reply itself and reports it on the same socket. A number out of
        # range is dropped, and a second report of the same number does not replace the first.
        # Its talk over, 2.4 s into what the candidate heard, says it was no cut-in after all.
        ws.send_json({"type": "timing", "stop_to_audible_ms": 1420, "interrupt_to_silence_ms": -5})
        ws.send_json(
            {
                "type": "timing",
                "stop_to_audible_ms": 900,
                "interrupt_to_silence_ms": 180,
                "talk_over_ms": 2400,
            }
        )
        hang_up(app, ws, interview_id)

    since = datetime.now(UTC) - timedelta(days=1)
    [trace] = client.portal.call(fakes.store.list_latency_traces, since)
    turns = client.portal.call(fakes.store.list_turns, interview_id)
    assert (turns[trace.turn_idx].speaker, turns[trace.turn_idx].text) == (
        "interviewer",
        DEFAULT_LINE,
    )
    assert trace.user_stopped_ms is not None
    assert trace.last_candidate_audio_ms <= trace.user_stopped_ms
    assert trace.user_stopped_ms <= trace.request_ms <= trace.first_token_ms
    assert trace.first_token_ms <= trace.first_chunk_ms <= trace.assistant_started_ms
    assert trace.assistant_started_ms <= trace.first_audio_ms
    assert trace.cut_in is False
    assert trace.browser_stop_to_audible_ms == 1420.0
    assert trace.interrupt_to_silence_ms == 180.0
    assert trace.talk_over_ms == 2400.0


class StepClock:
    """Seconds, moved by hand, for a tracer driven straight from a test."""

    def __init__(self) -> None:
        self.now = 500.0

    def __call__(self) -> float:
        return self.now


async def test_the_live_events_feed_the_calls_tracer():
    session = make_session(make_interview_record("i1"))
    session.brain.start()
    clock = StepClock()
    session.tracer = CallTracer(clock)
    session.tracer.start()
    on_event = routes_ws._live_events(session)

    clock.now += 1.0
    await on_event(SpeechUpdate(None, "user", "stopped"))
    clock.now += 0.5
    events = await collect(routes_vapi.start_reply(session, LlmRequest(CALL_ID, "I led it.", None)))
    assert any(DEFAULT_LINE in event for event in events)
    clock.now += 0.5
    await on_event(SpeechUpdate(None, "assistant", "started"))
    trace = session.brain.committing_trace
    assert (trace.user_stopped_ms, trace.request_ms, trace.assistant_started_ms) == (
        1000.0,
        1500.0,
        2000.0,
    )
    assert trace.first_token_ms == trace.first_chunk_ms == 1500.0

    clock.now += 1.0  # inside the 1500 ms window
    await on_event(UserInterrupted(None))
    assert trace.cut_in is True

    session.brain.finalize()
    assert session.tracer.committed() == [trace]
    assert trace.turn_idx == 2


class SlowStreamer:
    """Claude, taking 400 ms on `clock` to its first text, then saying DEFAULT_LINE. With
    `silent`, it writes nothing at all, so the brain says its fallback line."""

    def __init__(self, clock: StepClock, silent: bool = False) -> None:
        self.clock = clock
        self.silent = silent

    def stream(self, *, system, messages):
        return self._play()

    async def _play(self):
        self.clock.now += 0.4
        if not self.silent:
            yield DEFAULT_LINE


def traced_session(silent: bool = False) -> tuple[LiveSession, StepClock]:
    """A started session whose brain, tracer and Claude all run on one hand-moved clock."""
    clock = StepClock()
    plan = InterviewPlan(DEMO, [Question("q0", "Tell me about a time.", "ownership")])
    brain = Brain(plan, SlowStreamer(clock, silent), clock)
    session = LiveSession(make_interview_record("i1"), brain, token="", token_expires_at=0.0)
    brain.start()
    session.tracer = CallTracer(clock)
    session.tracer.start()
    return session, clock


async def speak_reply(session: LiveSession, answer: str) -> TurnTrace:
    """Vapi asks for a reply to `answer` and starts speaking it. Returns its trace."""
    await collect(routes_vapi.start_reply(session, LlmRequest(CALL_ID, answer, None)))
    await routes_ws._live_events(session)(SpeechUpdate(None, "assistant", "started"))
    return session.brain.committing_trace


async def test_the_wrap_up_line_is_never_timed_as_claudes_first_token():
    # The wrap-up never goes to Claude, so it takes no time to write. Counted as Claude's first
    # token it would drag that hop's median toward 0: with one follow-up, to 0 itself.
    session, clock = traced_session()
    follow_up = await speak_reply(session, "I led the migration.")
    clock.now += DEMO.total_s  # past the wrap-up reserve
    wrap_up = await speak_reply(session, "We shipped it.")
    assert session.brain.finished

    assert follow_up.first_token_ms - follow_up.request_ms == pytest.approx(400)
    assert wrap_up.first_token_ms is None
    assert wrap_up.request_ms is not None and wrap_up.first_chunk_ms is not None
    session.brain.finalize()
    hops = summarize_traces(session.tracer.committed())["hops_ms"]
    assert hops["request_to_first_token"] == pytest.approx(400)
    assert hops["first_chunk_to_assistant_started"] is not None


async def test_a_fallback_line_after_claude_wrote_nothing_is_never_timed_as_its_first_token():
    session, _ = traced_session(silent=True)
    trace = await speak_reply(session, "I led the migration.")
    assert trace.first_token_ms is None
    assert trace.first_chunk_ms is not None  # the fallback line still went out to Vapi


async def test_a_session_without_a_tracer_still_handles_every_live_event():
    # Sessions built before the call goes live, and many tests, have no tracer.
    session = make_session(make_interview_record("i1"))
    session.brain.start()
    on_event = routes_ws._live_events(session)

    await on_event(SpeechUpdate(None, "user", "stopped"))
    await collect(routes_vapi.start_reply(session, LlmRequest(CALL_ID, "I led it.", None)))
    await on_event(SpeechUpdate(None, "assistant", "started"))
    await on_event(UserInterrupted(None))

    assert [t.speaker for t in session.brain.turns] == ["interviewer", "candidate"]


def test_one_audio_listener_failing_never_starves_the_other(caplog):
    heard = []

    def broken(direction: str, frame: bytes) -> None:
        raise RuntimeError("listener said secret-detail")

    on_audio = routes_ws._fan_out(broken, lambda direction, frame: heard.append(direction))
    on_audio("interviewer", VOICE)
    on_audio("candidate", MIC)

    assert heard == ["interviewer", "candidate"]
    assert "RuntimeError" in caplog.text
    assert "secret-detail" not in caplog.text


@pytest.mark.parametrize("stamp", ["new_trace", "first_token", "first_chunk"])
def test_a_tracer_that_breaks_never_cuts_off_the_reply(
    client, app, fakes, monkeypatch, caplog, stamp
):
    def broken(self, *args):
        raise RuntimeError("tracer said secret-detail")

    monkeypatch.setattr(routes_ws, "CallTracer", type("Broken", (CallTracer,), {stamp: broken}))
    body = start(client)
    interview_id = body["interview_id"]

    with client.websocket_connect(body["ws_path"]) as ws:
        client.portal.call(fakes.vapi_socket.wait_reading)
        assert said(ask(client, "I led the migration.")) == DEFAULT_LINE  # every chunk, then DONE
        client.portal.call(fakes.vapi_socket.deliver, ASSISTANT_STARTED)
        hang_up(app, ws, interview_id)

    report_url = f"/api/interviews/{interview_id}/report"
    wait_until(lambda: client.get(report_url).status_code == 200)
    turns = client.portal.call(fakes.store.list_turns, interview_id)
    assert [(t.speaker, t.text) for t in turns][1:] == [
        ("candidate", "I led the migration."),
        ("interviewer", DEFAULT_LINE),
    ]
    failures = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert failures == ["tracing a reply failed: RuntimeError"]
    assert "secret-detail" not in caplog.text


def test_a_trace_write_that_fails_is_logged_and_the_clean_up_goes_on(client, app, fakes, caplog):
    async def failing_write(interview_id, traces):
        raise RuntimeError("database said secret-detail")

    fakes.store.add_latency_traces = failing_write
    body = start(client)
    interview_id = body["interview_id"]

    with client.websocket_connect(body["ws_path"]) as ws:
        client.portal.call(fakes.vapi_socket.wait_reading)
        assert said(ask(client, "I led the migration.")) == DEFAULT_LINE
        client.portal.call(fakes.vapi_socket.deliver, ASSISTANT_STARTED)
        hang_up(app, ws, interview_id)

    report_url = f"/api/interviews/{interview_id}/report"
    wait_until(lambda: client.get(report_url).status_code == 200)
    turns = client.portal.call(fakes.store.list_turns, interview_id)
    assert [t.speaker for t in turns] == ["interviewer", "candidate", "interviewer"]
    assert "could not save the latency traces: RuntimeError" in caplog.text
    assert "secret-detail" not in caplog.text


# --- POST /vapi/llm/{secret}/chat/completions --------------------------------------------------


@pytest.mark.parametrize("secret", ["wrong", f"{BRAIN_SECRET}x", "sécret"])
def test_llm_wrong_secret_404(client, fakes, secret):
    r = client.post(f"/vapi/llm/{secret}/chat/completions", json=llm_body("Hi."))
    assert r.status_code == 404
    assert fakes.streamer.calls == 0


@pytest.mark.parametrize(
    "body",
    [
        json.dumps(llm_body("Hello?", call_id="not-a-live-call")),
        json.dumps({"messages": [{"role": "user", "content": "no call id"}]}),
        "{not json",
    ],
    ids=["unknown-call", "no-call-id", "not-json"],
)
def test_llm_unknown_call_streams_ended_line(client, fakes, body):
    r = client.post(LLM_PATH, content=body, headers={"content-type": "application/json"})
    assert said(r) == ENDED_LINE
    assert fakes.streamer.calls == 0


def test_llm_new_request_cancels_inflight_reply(client, app, fakes):
    fakes.streamer.scripts = [[HANG]]
    body = start(client)
    interview_id = body["interview_id"]

    with client.websocket_connect(body["ws_path"]) as ws:
        client.portal.call(fakes.vapi_socket.wait_reading)
        session = app.state.registry.by_interview(interview_id)

        first, first_reply = in_background(lambda: ask(client, "I led the"))
        assert fakes.streamer.hanging.get(timeout=2) == 1
        first_task = session.reply_task

        assert said(ask(client, "I led the migration.")) == DEFAULT_LINE
        first.join(2)

        assert not first.is_alive()
        assert first_task.cancelled()
        assert fakes.streamer.cancelled == 1
        assert said(first_reply[0]) == ""  # the old stream ended cleanly, with nothing in it
        assert_history_valid(session.brain.history)

        # The reply to the second request is the one that gets spoken and recorded.
        client.portal.call(fakes.vapi_socket.deliver, ASSISTANT_STARTED)
        said(ask(client, "Next answer.", DEFAULT_LINE))
        history = session.brain.history
        assert_history_valid(history)
        assert [m["content"] for m in history if m["role"] == "user"][1:] == [
            "I led the migration."
        ]

        hang_up(app, ws, interview_id)


async def test_a_reply_cancelled_before_its_task_ever_ran_still_ends_its_stream():
    # Two requests can be handled in one turn of the event loop, so the second can cancel the
    # first's draft before that task has run at all. The first stream must still end, not hang.
    session = make_session(make_interview_record("i1"))
    session.brain.start()

    first = routes_vapi.start_reply(session, LlmRequest(CALL_ID, "I led", None))
    second = routes_vapi.start_reply(session, LlmRequest(CALL_ID, "I led the team.", None))

    first_events = await asyncio.wait_for(collect(first), 2.0)
    second_events = await asyncio.wait_for(collect(second), 2.0)
    assert first_events[-1] == SSE_DONE
    assert not any('"content"' in event for event in first_events)
    assert second_events[-1] == SSE_DONE
    assert any(DEFAULT_LINE in event for event in second_events)


# --- POST /vapi/webhook/{secret} ---------------------------------------------------------------


def end_of_call_report(ended_reason: str, call_id: str = CALL_ID) -> dict:
    return {
        "message": {
            "type": "end-of-call-report",
            "endedReason": ended_reason,
            "call": {"id": call_id},
        }
    }


async def add_ended_interview(store: MemoryStore, ended_reason: str) -> None:
    await store.create_interview(
        Interview(
            id="i1",
            mode="demo",
            status="graded",
            visitor_hash="v",
            ip_hash="ip",
            cap_s=300,
            questions=["Q"],
            created_at=datetime.now(UTC),
            vapi_call_id=CALL_ID,
            ended_reason=ended_reason,
            duration_s=42,
        )
    )


@pytest.mark.parametrize("secret", ["wrong", f"{WEBHOOK_SECRET}x", "sécret"])
def test_webhook_wrong_secret_404(client, fakes, secret):
    client.portal.call(add_ended_interview, fakes.store, "vapi_closed")

    r = client.post(f"/vapi/webhook/{secret}", json=end_of_call_report("silence-timed-out"))

    assert r.status_code == 404
    assert get(client, fakes.store, "i1").ended_reason == "vapi_closed"


def test_end_of_call_report_records_vapis_reason_when_vapi_ended_the_call(client, fakes):
    client.portal.call(add_ended_interview, fakes.store, "vapi_closed")

    r = client.post(WEBHOOK_PATH, json=end_of_call_report("silence-timed-out"))

    assert (r.status_code, r.json()) == (200, {})
    assert get(client, fakes.store, "i1").ended_reason == "silence-timed-out"


@pytest.mark.parametrize("ours", ["cap", "browser_closed", "plan_done", "error"])
def test_end_of_call_report_keeps_our_reason_when_we_ended_the_call(client, fakes, ours):
    # Vapi labels a call we ended with a fixed string that says nothing (vapi-contract.md, Q7).
    client.portal.call(add_ended_interview, fakes.store, ours)

    r = client.post(
        WEBHOOK_PATH, json=end_of_call_report("assistant-ended-call-after-message-spoken")
    )

    assert (r.status_code, r.json()) == (200, {})
    assert get(client, fakes.store, "i1").ended_reason == ours


@pytest.mark.parametrize(
    "body",
    [
        {"message": {"type": "status-update", "status": "ended", "call": {"id": CALL_ID}}},
        {"message": {"type": "speech-update", "status": "stopped", "role": "assistant"}},
        {"message": {"type": "user-interrupted", "call": {"id": CALL_ID}}},
        {"message": {"type": "conversation-update", "call": {"id": CALL_ID}}},
        end_of_call_report("silence-timed-out", call_id="someone-elses-call"),
        {"not": "a vapi message"},
        "{not json",
    ],
)
def test_every_other_webhook_gets_200_and_changes_nothing(client, fakes, body):
    client.portal.call(add_ended_interview, fakes.store, "vapi_closed")

    if isinstance(body, str):
        r = client.post(WEBHOOK_PATH, content=body, headers={"content-type": "application/json"})
    else:
        r = client.post(WEBHOOK_PATH, json=body)

    assert (r.status_code, r.json()) == (200, {})
    assert get(client, fakes.store, "i1").ended_reason == "vapi_closed"


def test_a_status_update_webhook_does_not_end_a_live_call(client, app, fakes):
    body = start(client)

    with client.websocket_connect(body["ws_path"]) as ws:
        client.portal.call(fakes.vapi_socket.wait_reading)

        r = client.post(
            WEBHOOK_PATH,
            json={"message": {"type": "status-update", "status": "ended", "call": {"id": CALL_ID}}},
        )

        assert r.status_code == 200
        # The relay still relays: live events come from the audio socket, not webhooks.
        client.portal.call(fakes.vapi_socket.deliver, VOICE)
        assert ws.receive_bytes() == VOICE
        hang_up(app, ws, body["interview_id"])


# --- GET /api/interviews/{id}/report -----------------------------------------------------------


def test_report_202_then_200(client, app, fakes):
    fakes.grader.hold()
    body = start(client)
    interview_id = body["interview_id"]

    with client.websocket_connect(body["ws_path"]) as ws:
        client.portal.call(fakes.vapi_socket.wait_reading)
        said(ask(client, "I wrote the migration plan and walked the team through it."))
        client.portal.call(fakes.vapi_socket.deliver, ASSISTANT_STARTED)
        hang_up(app, ws, interview_id)

    grading = client.get(f"/api/interviews/{interview_id}/report")
    assert (grading.status_code, grading.json()) == (202, {"status": "grading"})

    fakes.grader.release()
    wait_until(lambda: client.get(f"/api/interviews/{interview_id}/report").status_code == 200)

    report = client.get(f"/api/interviews/{interview_id}/report").json()
    assert report["status"] == "graded"
    [question] = report["questions"]
    [item] = question["items"]
    assert (item["item"], item["score"], item["quotes"]) == ("action", 3, [FakeGrader.QUOTE])
    assert get(client, fakes.store, interview_id).status == "graded"


def test_report_for_an_unknown_interview_is_404(client):
    assert client.get("/api/interviews/not-an-interview/report").status_code == 404


def test_report_for_an_interview_that_never_started_is_404(client):
    body = start(client)
    assert client.get(f"/api/interviews/{body['interview_id']}/report").status_code == 404


# --- GET /api/stats ------------------------------------------------------------------------------


def keep_traces(client: TestClient, store: MemoryStore, interview_id: str, *traces) -> None:
    """Put a finished interview and its traces straight into the store."""
    client.portal.call(store.create_interview, make_interview_record(interview_id, "graded"))
    client.portal.call(store.add_latency_traces, interview_id, list(traces))


@pytest.mark.parametrize("days", [0, 31, -1, "week"])
def test_stats_refuse_a_window_outside_1_to_30_days(client, days):
    assert client.get(f"/api/stats?days={days}").status_code == 422


def test_stats_summarize_the_traces_for_anyone_and_name_no_interview(client, fakes):
    interview_id = "iv-7f3a-private"
    traces = [
        TurnTrace(
            turn_idx=1,
            request_ms=100.0,
            first_token_ms=500.0,
            assistant_started_ms=900.0,
            browser_stop_to_audible_ms=1420.0,
            interrupt_to_silence_ms=180.0,
            cut_in=True,
        ),
        TurnTrace(turn_idx=3, assistant_started_ms=4000.0, browser_stop_to_audible_ms=900.0),
    ]
    keep_traces(client, fakes.store, interview_id, *traces)

    # A plain request: no owner key, no cookie.
    response = TestClient(client.app).get("/api/stats")
    assert response.status_code == 200
    body = response.json()
    assert body == summarize_traces(traces)
    assert body["turns"] == 2
    assert body["stop_to_audible_ms"] == {"p50": 900.0, "p95": 1420.0, "n": 2}
    assert body["cut_in_rate"] == 0.5
    # Only numbers about all the calls together: nothing that points at one interview.
    assert interview_id not in response.text
    assert "interview_id" not in response.text
    assert "turn_idx" not in response.text


def test_stats_cover_the_days_asked_for_and_7_by_default(settings):
    now = datetime.now(UTC)
    kept_at = [now]
    store = MemoryStore(clock=lambda: kept_at[0])
    with TestClient(create_app(settings, store=store, audio_store=FakeAudioStore())) as c:
        for interview_id, age in (("three-days", 3), ("ten-days", 10), ("forty-days", 40)):
            kept_at[0] = now - timedelta(days=age)
            keep_traces(c, store, interview_id, TurnTrace(turn_idx=1, assistant_started_ms=1.0))
        kept_at[0] = now

        def turns(query: str) -> int:
            response = c.get(f"/api/stats{query}")
            assert response.status_code == 200
            return response.json()["turns"]

        assert turns("?days=1") == 0
        assert turns("?days=2") == 0
        assert turns("") == 1
        assert turns("?days=7") == 1
        assert turns("?days=30") == 2


# --- the sweeper -------------------------------------------------------------------------------


def test_sweep_marks_expired_unused_interview_abandoned(client, app, fakes):
    body = start(client)
    interview_id = body["interview_id"]
    registry = app.state.registry
    created = get(client, fakes.store, interview_id)

    client.portal.call(sweep_once, registry, fakes.store, registry.clock() + 59)
    assert get(client, fakes.store, interview_id).status == "created"  # not expired yet

    client.portal.call(sweep_once, registry, fakes.store, registry.clock() + 61)

    assert get(client, fakes.store, interview_id).status == "abandoned"
    assert registry.by_interview(interview_id) is None
    starts = client.portal.call(fakes.store.starts_today, created.visitor_hash, created.ip_hash)
    assert starts == 0
    assert closed_with(client, body["ws_path"]) == 4401


async def test_sweep_ends_a_live_interview_that_outlived_its_cap_as_stale():
    # Ruling R23: a call left live by a crash must stop counting against the daily budget.
    store = MemoryStore()
    registry = SessionRegistry()
    now = time.time()

    def live(id: str, age_s: float) -> Interview:
        return Interview(
            id=id,
            mode="demo",
            status="live",
            visitor_hash=f"v-{id}",
            ip_hash=f"ip-{id}",
            cap_s=300,
            questions=["Q"],
            created_at=datetime.fromtimestamp(now - age_s, UTC),
        )

    crashed = live("crashed", 300 + 61)
    young = live("young", 300 + 59)
    running = live("running", 300 + 61)  # still in this process's registry
    for i in (crashed, young, running):
        await store.create_interview(i)
    registry.add(make_session(running))

    await sweep_once(registry, store, now)

    for id in ("crashed", "running"):
        stale = await store.get_interview(id)
        assert (stale.status, stale.ended_reason, stale.duration_s) == ("ended", "stale", 300)
    assert (await store.get_interview("young")).status == "live"
    # The session is its route's to remove, once the call ends.
    assert registry.by_interview("running") is not None


def test_a_call_marked_stale_while_it_runs_gets_its_real_end_from_its_route(client, app, fakes):
    # A socket opened near the end of its token's minute goes live late, so the sweeper can mark
    # the call stale in its last seconds. The route has the last word when the call ends.
    body = start(client)
    interview_id = body["interview_id"]

    with client.websocket_connect(body["ws_path"]) as ws:
        client.portal.call(fakes.vapi_socket.wait_reading)
        created = get(client, fakes.store, interview_id).created_at.timestamp()
        client.portal.call(sweep_once, app.state.registry, fakes.store, created + 300 + 61)
        assert get(client, fakes.store, interview_id).ended_reason == "stale"

        hang_up(app, ws, interview_id)

    ended = get(client, fakes.store, interview_id)
    assert ended.status in ("ended", "graded")
    assert (ended.ended_reason, ended.duration_s) == ("browser_closed", 0)


async def test_sweep_leaves_alone_an_interview_its_route_ended_after_the_sweep_listed_it():
    class EndedMeanwhile(MemoryStore):
        async def list_live_interviews(self) -> list[Interview]:
            listed = await super().list_live_interviews()
            # The call's route ends it between the sweeper's read and its write.
            await self.update_interview("i1", status="ended", ended_reason="cap", duration_s=297)
            return listed

    store = EndedMeanwhile()
    await store.create_interview(make_interview_record("i1"))

    await sweep_once(SessionRegistry(), store, time.time() + 300 + 61)

    ended = await store.get_interview("i1")
    assert (ended.status, ended.ended_reason, ended.duration_s) == ("ended", "cap", 297)


async def test_sweep_abandons_a_created_interview_that_a_restart_left_behind():
    # Its session was in the memory of the process that stopped, so its token is gone and it can
    # never start. Created, it held its visitor's demo and a cap of the budget all day.
    store = MemoryStore()
    registry = SessionRegistry()
    now = time.time()

    def created(id: str, age_s: float) -> Interview:
        return Interview(
            id=id,
            mode="demo",
            status="created",
            visitor_hash=f"v-{id}",
            ip_hash=f"ip-{id}",
            cap_s=300,
            questions=["Q"],
            created_at=datetime.fromtimestamp(now - age_s, UTC),
        )

    orphan, young, opening = created("orphan", 61), created("young", 30), created("opening", 90)
    for i in (orphan, young, opening):
        await store.create_interview(i)
    registry.add(make_session(opening))  # its token was used; its route is connecting the call

    await sweep_once(registry, store, now)

    assert (await store.get_interview("orphan")).status == "abandoned"
    assert (await store.get_interview("young")).status == "created"
    assert (await store.get_interview("opening")).status == "created"
    assert await store.starts_today("v-orphan", "ip-orphan") == 0
    assert await store.seconds_used_today() == 2 * 300


async def test_sweep_leaves_a_session_whose_token_was_used_alone():
    store = MemoryStore()
    registry = SessionRegistry()
    interview = Interview(
        id="i1",
        mode="demo",
        status="created",
        visitor_hash="v",
        ip_hash="ip",
        cap_s=300,
        questions=["Q"],
        created_at=datetime.now(UTC) - timedelta(minutes=2),
    )
    await store.create_interview(interview)
    registry.add(make_session(interview))  # token "" means it was used

    await sweep_once(registry, store, time.time() + 120)

    assert (await store.get_interview("i1")).status == "created"
    assert registry.by_interview("i1") is not None


class SweptStore(MemoryStore):
    """A MemoryStore that counts sweeps (each one lists the live interviews once) and records
    each purge as (days, the sweep it came after). It can be told to fail the next few."""

    def __init__(self) -> None:
        super().__init__()
        self.sweeps = 0
        self.purges: list[tuple[int, int]] = []
        self.failing_sweeps = 0
        self.failing_purges = 0

    async def list_live_interviews(self) -> list[Interview]:
        self.sweeps += 1
        if self.failing_sweeps:
            self.failing_sweeps -= 1
            raise RuntimeError(f"the database is down, call {CALL_ID}")
        return await super().list_live_interviews()

    async def delete_demo_data_older_than(self, days: int, **kwargs) -> int:
        self.purges.append((days, self.sweeps))
        if self.failing_purges:
            self.failing_purges -= 1
            raise RuntimeError(f"the database is down, call {CALL_ID}")
        return await super().delete_demo_data_older_than(days, **kwargs)


T0 = 1_800_000_000.0  # some time in 2027, in seconds since the epoch


def scripted_clock(*seconds: float):
    """A clock that reads T0 plus each of `seconds` in turn, then stays at the last."""
    readings = iter(seconds)
    return lambda: T0 + next(readings, seconds[-1])


async def no_wait(seconds: float) -> None:
    await asyncio.sleep(0)


async def until(condition, timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not condition():
        assert loop.time() < deadline, "timed out waiting"
        await asyncio.sleep(0)


async def stop(task: asyncio.Task) -> None:
    task.cancel()
    await asyncio.wait({task}, timeout=1.0)
    assert task.cancelled()


async def test_the_sweeper_sweeps_every_minute_and_deletes_week_old_demo_data_hourly():
    store = SweptStore()
    waits: list[float] = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)
        await asyncio.sleep(0)

    clock = scripted_clock(0, 60, 3599, 3600, 3660, 7199, 7200)  # one reading per sweep
    sweeper = asyncio.create_task(
        run_sweeper(
            SessionRegistry(), store, FakeVapi(), FakeAudioStore(), clock=clock, sleep=sleep
        )
    )
    await until(lambda: store.sweeps >= 9)
    await stop(sweeper)

    # At start, then on the first sweep an hour or more after the last purge: 3600 and 7200.
    assert store.purges == [(7, 1), (7, 4), (7, 7)]
    assert set(waits) == {60.0}


async def test_a_failing_sweep_or_purge_is_logged_and_the_sweeper_goes_on(caplog):
    store = SweptStore()
    store.failing_sweeps = 2
    store.failing_purges = 1
    clock = scripted_clock(0, 60, 120, 3600)
    sweeper = asyncio.create_task(
        run_sweeper(
            SessionRegistry(), store, FakeVapi(), FakeAudioStore(), clock=clock, sleep=no_wait
        )
    )
    await until(lambda: store.sweeps >= 5)
    await stop(sweeper)

    # The failed sweep did not skip the purge, and the failed purge is tried again an hour on.
    assert store.purges == [(7, 1), (7, 4)]
    failures = sorted(r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR)
    assert failures == [
        "could not delete old demo data: RuntimeError",
        "could not sweep the sessions: RuntimeError",
        "could not sweep the sessions: RuntimeError",
    ]
    assert CALL_ID not in caplog.text


@pytest.mark.parametrize("when", ["mid_sweep", "between_sweeps"])
async def test_cancelling_the_sweeper_stops_it_whatever_it_is_doing(when):
    class Store(SweptStore):
        async def list_live_interviews(self) -> list[Interview]:
            listed = await super().list_live_interviews()
            if when == "mid_sweep":
                await asyncio.Event().wait()  # a query that never comes back
            return listed

    store = Store()
    sweeper = asyncio.create_task(
        run_sweeper(SessionRegistry(), store, FakeVapi(), FakeAudioStore())
    )
    if when == "mid_sweep":
        await until(lambda: store.sweeps == 1)
    else:
        await until(lambda: store.purges)  # then it waits a minute for the next sweep
        await asyncio.sleep(0)

    await stop(sweeper)


def old_demo(id: str, call_id: str | None, days: float = 8, mode: str = "demo") -> Interview:
    return Interview(
        id=id,
        mode=mode,
        status="graded",
        visitor_hash="v",
        ip_hash="ip",
        cap_s=300,
        questions=["Q"],
        created_at=datetime.now(UTC) - timedelta(days=days),
        vapi_call_id=call_id,
    )


async def test_the_purge_deletes_each_old_demos_call_at_vapi_before_its_row():
    # Vapi keeps its own copy of every call: the transcript, the messages, logs and a summary.
    # The 7-day retention covers that copy too, and only the row knows which call it was.
    store = MemoryStore()
    for interview in (
        old_demo("old-a", "call-a"),
        old_demo("old-b", "call-b"),
        old_demo("old-no-call", None),  # its call never got created
        old_demo("recent", "call-recent", days=6),
        old_demo("old-full", "call-full", mode="full"),  # full mode is kept, here and at Vapi
    ):
        await store.create_interview(interview)

    class Vapi(FakeVapi):
        async def delete_call(self, call_id: str) -> None:
            assert await store.get_interview_by_call(call_id) is not None  # the row comes after
            await super().delete_call(call_id)

    vapi = Vapi()
    await purge_once(store, vapi, FakeAudioStore())

    assert sorted(vapi.deleted) == ["call-a", "call-b"]
    for gone in ("old-a", "old-b", "old-no-call"):
        assert await store.get_interview(gone) is None
    for kept in ("recent", "old-full"):
        assert await store.get_interview(kept) is not None


async def test_purge_deletes_recordings_before_rows():
    store = MemoryStore()
    for interview in (
        old_demo("old-a", "call-a"),
        old_demo("recent", "call-recent", days=6),
        old_demo("old-full", "call-full", mode="full"),
    ):
        await store.create_interview(interview)

    class Audio(FakeAudioStore):
        async def delete_prefix(self, prefix: str) -> None:
            assert await store.get_interview("old-a") is not None  # the row comes after
            await super().delete_prefix(prefix)

    audio = Audio()
    await purge_once(store, FakeVapi(), audio)

    assert audio.deleted == ["interviews/old-a/"]
    assert await store.get_interview("old-a") is None
    assert await store.get_interview("recent") is not None
    assert await store.get_interview("old-full") is not None


async def test_an_interview_that_turns_old_mid_purge_keeps_its_row_until_its_audio_is_deleted():
    # Every store call reads the clock, and the clock moves an hour per read. This interview is
    # not 7 days old when the purge lists the recordings to delete, but is by the time the rows
    # go. Its folder was never deleted, so its row must stay: it is the only thing that names it.
    base = datetime.now(UTC)
    reads = iter(range(1, 100))
    store = MemoryStore(clock=lambda: base + timedelta(hours=next(reads)))
    crossing = old_demo("crossing", "call-crossing")
    crossing.created_at = base - timedelta(days=7) + timedelta(minutes=150)
    await store.create_interview(crossing)
    audio = FakeAudioStore()
    vapi = FakeVapi()

    await purge_once(store, vapi, audio)

    assert audio.deleted == []
    assert await store.get_interview("crossing") is not None

    await purge_once(store, vapi, audio)  # an hour later: now it is listed first

    assert audio.deleted == ["interviews/crossing/"]
    assert "call-crossing" in vapi.deleted
    assert await store.get_interview("crossing") is None


async def test_purge_keeps_rows_when_storage_delete_fails(caplog):
    caplog.set_level(logging.DEBUG)
    store = MemoryStore()
    await store.create_interview(old_demo("stuck", "call-stuck"))
    await store.create_interview(old_demo("fine", "call-fine"))
    audio = FakeAudioStore()
    audio.delete_errors = {"interviews/stuck/": RuntimeError("bucket said secret-detail")}
    vapi = FakeVapi()

    await purge_once(store, vapi, audio)

    assert await store.get_interview("fine") is None
    stuck = await store.get_interview("stuck")
    assert stuck is not None and stuck.vapi_call_id == "call-stuck"
    assert "RuntimeError" in caplog.text
    assert "secret-detail" not in caplog.text

    audio.delete_errors = {}
    await purge_once(store, vapi, audio)  # an hour later
    assert audio.deleted == ["interviews/fine/", "interviews/stuck/"]
    assert await store.get_interview("stuck") is None


async def test_a_call_vapi_would_not_delete_keeps_its_row_for_the_next_purge(caplog):
    caplog.set_level(logging.DEBUG)
    secret_id = "01a15690-48f3-7331-a348-64c2e5c04886"
    store = MemoryStore()
    await store.create_interview(old_demo("stuck", secret_id))
    await store.create_interview(old_demo("fine", "call-fine"))
    vapi = FakeVapi()
    vapi.delete_errors = {
        secret_id: VapiError("Vapi returned 503: try again"),
    }

    await purge_once(store, vapi, FakeAudioStore())

    assert vapi.deleted == ["call-fine"]
    assert await store.get_interview("fine") is None
    assert (await store.get_interview("stuck")).vapi_call_id == secret_id
    [warning] = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert "Vapi returned 503: try again" in warning.getMessage()
    assert secret_id not in caplog.text

    vapi.delete_errors = {}
    await purge_once(store, vapi, FakeAudioStore())  # an hour later
    assert vapi.deleted == ["call-fine", secret_id]
    assert await store.get_interview("stuck") is None


async def test_a_purge_whose_vapi_delete_breaks_logs_only_the_error_type(caplog):
    caplog.set_level(logging.DEBUG)
    store = MemoryStore()
    await store.create_interview(old_demo("stuck", "call-stuck"))
    vapi = FakeVapi()
    vapi.delete_errors = {"call-stuck": RuntimeError("bug near call-stuck")}

    await purge_once(store, vapi, FakeAudioStore())

    assert await store.get_interview("stuck") is not None
    assert "RuntimeError" in caplog.text
    assert "call-stuck" not in caplog.text


def test_the_app_runs_the_sweeper_from_start_up_to_shutdown(settings, fakes):
    store = SweptStore()
    app = create_app(
        settings,
        store=store,
        vapi=fakes.vapi,
        streamer_factory=lambda: fakes.streamer,
        grader=fakes.grader,
    )

    with TestClient(app):
        wait_until(lambda: store.purges)
        assert (store.sweeps, store.purges) == (1, [(7, 1)])
        sweeper = app.state.sweeper
        assert not sweeper.done()

    assert sweeper.cancelled()


# --- ruling R19 --------------------------------------------------------------------------------


def test_the_call_id_and_socket_url_never_reach_the_browser_or_the_logs(client, app, fakes, caplog):
    caplog.set_level(logging.DEBUG)
    secret_id = "01a15690-48f3-7331-a348-64c2e5c04886"  # shaped like a real one
    fakes.vapi.call_id = secret_id
    shown: list[str] = []  # everything the browser, or Vapi's brain requests, got back

    r = client.post("/api/interviews")
    shown += [r.text, str(r.headers)]
    interview_id = r.json()["interview_id"]
    report_url = f"/api/interviews/{interview_id}/report"
    with client.websocket_connect(r.json()["ws_path"]) as ws:
        client.portal.call(fakes.vapi_socket.wait_reading)
        shown.append(ask(client, "I wrote the plan.", call_id=secret_id).text)
        client.portal.call(fakes.vapi_socket.deliver, ASSISTANT_STARTED)
        client.post(WEBHOOK_PATH, json=end_of_call_report("customer-ended-call", secret_id))
        client.post(WEBHOOK_PATH, json={"message": {"type": "hang", "call": {"id": secret_id}}})
        ws.send_json({"type": "end"})
        shown.append(json.dumps(ws.receive_json()))
        wait_cleaned_up(app, interview_id)
    shown.append(ask(client, "Hello?", call_id=secret_id).text)  # the call is over now
    wait_until(lambda: client.get(report_url).status_code == 200)
    shown.append(client.get(report_url).text)

    for text in [*shown, caplog.text]:
        assert secret_id not in text
        assert SOCKET_URL not in text
