# /// script
# requires-python = ">=3.11"
# dependencies = ["fastapi", "uvicorn", "httpx", "websockets"]
# ///
"""Task 1 spike: record what Vapi really sends and receives. Throwaway code.

Run it with uv (no pyproject exists yet, so the deps are declared above):

    uv run scripts/spike_vapi.py audio                    # make the two spoken answers
    ngrok http 8000                                       # shell 1
    uv run scripts/spike_vapi.py serve                    # shell 2
    uv run scripts/spike_vapi.py call --interrupt-at 1.5  # shell 3
    uv run scripts/spike_vapi.py fixtures                 # scrub raw logs into fixtures

Raw captures (unscrubbed, never committed) go to scripts/.spike/raw/.
Scrubbed fixtures go to server/tests/fixtures/vapi/.
"""

from __future__ import annotations

import argparse
import array
import asyncio
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import wave
from pathlib import Path

from fastapi import FastAPI, Request  # top level: FastAPI resolves annotations from module globals
from fastapi.responses import JSONResponse, StreamingResponse

ROOT = Path(__file__).resolve().parent.parent
SPIKE = ROOT / "scripts" / ".spike"
RAW = SPIKE / "raw"
FIXTURES = ROOT / "server" / "tests" / "fixtures" / "vapi"
ENV_FILE = ROOT / ".env"

SAMPLE_RATE = 16000
FRAME_SAMPLES = 320  # 20 ms at 16 kHz
FRAME_BYTES = FRAME_SAMPLES * 2  # pcm_s16le
FRAME_SECONDS = 0.02

ANSWERS = {
    "answer1": (
        "At my last internship I led a migration of our billing service from one "
        "big monolith to a few smaller services. I wrote the plan, split the work "
        "between three engineers, and we shipped it two weeks early with no downtime."
    ),
    "answer2": (
        "Sorry to cut in, but I should add that the hardest part was convincing the "
        "team to pause new features while we did the migration."
    ),
}

# The interviewer reply the spike's fake LLM streams. The first one is long on
# purpose, so the second answer can interrupt it while it is still being spoken.
LONG_REPLY = (
    "Thanks, that is helpful. I would like to dig a little deeper into the design "
    "choices you made along the way. Walk me through the hardest tradeoff you faced, "
    "what options you considered, why you picked the one you did, and how you knew "
    "it was the right call afterwards."
)
SHORT_REPLY = "Understood, thanks. That is all I needed for this test."


# --------------------------------------------------------------------------
# Small helpers shared by the subcommands
# --------------------------------------------------------------------------


def load_env() -> dict[str, str]:
    """Read KEY=VALUE lines from .env (no python-dotenv dependency)."""
    values: dict[str, str] = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip('"').strip("'")
    for key in ("VAPI_API_KEY",):
        if key in os.environ:
            values[key] = os.environ[key]
    return values


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


SENSITIVE_HEADERS = {"authorization", "x-vapi-secret", "x-api-key", "cookie", "set-cookie"}


# --------------------------------------------------------------------------
# serve: the fake "brain" and webhook listener that Vapi calls
# --------------------------------------------------------------------------


def build_app():
    app = FastAPI()
    state = {"seq": 0, "llm_calls": 0}

    def log_event(**fields) -> int:
        state["seq"] += 1
        fields = {"seq": state["seq"], "t": time.time(), **fields}
        RAW.mkdir(parents=True, exist_ok=True)
        with (RAW / "events.jsonl").open("a") as fh:
            fh.write(json.dumps(fields) + "\n")
        return state["seq"]

    async def read_body(request: Request):
        raw = await request.body()
        try:
            return json.loads(raw)
        except ValueError:
            return {"_unparsed_body": raw.decode("utf-8", "replace")}

    @app.middleware("http")
    async def trace(request: Request, call_next):
        response = await call_next(request)
        print(f"{request.method} {request.url.path} -> {response.status_code}", flush=True)
        return response

    def sse_reply(text: str) -> StreamingResponse:
        base = {
            "id": "chatcmpl-spike",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": "mockingbird-brain",
        }

        def chunk(delta: dict, finish=None) -> str:
            payload = {**base, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
            return f"data: {json.dumps(payload)}\n\n"

        async def stream():
            yield chunk({"role": "assistant", "content": ""})
            for word in text.split(" "):
                yield chunk({"content": word + " "})
                await asyncio.sleep(0.02)
            yield chunk({}, finish="stop")
            yield "data: [DONE]\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream")

    async def handle_llm(request: Request, expected_path: bool):
        body = await read_body(request)
        state["llm_calls"] += 1
        n = log_event(
            kind="llm" if expected_path else "llm_other",
            path=request.url.path,
            query=str(request.url.query),
        )
        write_json(RAW / f"llm_{n:03d}.json", body)
        write_json(
            RAW / f"llm_{n:03d}.meta.json",
            {
                "method": request.method,
                "path": request.url.path,
                "query": str(request.url.query),
                "headers": {
                    k: ("REDACTED" if k.lower() in SENSITIVE_HEADERS else v)
                    for k, v in request.headers.items()
                },
            },
        )
        reply = LONG_REPLY if state["llm_calls"] == 1 else SHORT_REPLY
        return sse_reply(reply)

    @app.post("/spike/llm/chat/completions")
    async def llm_chat(request: Request):
        return await handle_llm(request, expected_path=True)

    # Catch-all: if Vapi appends a different path, save it and still answer so
    # the call keeps going. Question 1 is about exactly this.
    @app.post("/spike/llm/{rest:path}")
    async def llm_other(rest: str, request: Request):
        return await handle_llm(request, expected_path=False)

    @app.post("/spike/webhook")
    async def webhook(request: Request):
        body = await read_body(request)
        message = body.get("message") if isinstance(body, dict) else None
        mtype = (message or {}).get("type", "unknown") if isinstance(message, dict) else "unknown"
        n = log_event(kind="webhook", type=mtype)
        write_json(RAW / f"webhook_{mtype}_{n:03d}.json", body)
        return JSONResponse({})

    @app.get("/spike/health")
    async def health():
        return {"ok": True}

    return app


def cmd_serve(args) -> None:
    import uvicorn

    if RAW.exists():
        shutil.rmtree(RAW)  # each run starts clean
    RAW.mkdir(parents=True)
    uvicorn.run(build_app(), host="127.0.0.1", port=args.port, log_level="warning")


# --------------------------------------------------------------------------
# audio: two spoken answers, 16 kHz mono 16-bit PCM WAV
# --------------------------------------------------------------------------


def cmd_audio(args) -> None:
    SPIKE.mkdir(parents=True, exist_ok=True)
    for name, text in ANSWERS.items():
        aiff, wav = SPIKE / f"{name}.aiff", SPIKE / f"{name}.wav"
        subprocess.run(["say", "-v", "Samantha", "-o", str(aiff), text], check=True)
        subprocess.run(
            ["afconvert", "-f", "WAVE", "-d", f"LEI16@{SAMPLE_RATE}", "-c", "1", str(aiff), str(wav)],
            check=True,
        )
        aiff.unlink()
        pcm = read_wav(wav)
        print(f"{wav}: {len(pcm) / 2 / SAMPLE_RATE:.1f} s")


def read_wav(path: Path) -> bytes:
    with wave.open(str(path), "rb") as wf:
        if (wf.getframerate(), wf.getnchannels(), wf.getsampwidth()) != (SAMPLE_RATE, 1, 2):
            raise SystemExit(f"{path} must be 16 kHz mono 16-bit; run the audio subcommand")
        return wf.readframes(wf.getnframes())


# --------------------------------------------------------------------------
# call: create a Vapi call and stream audio to its WebSocket in real time
# --------------------------------------------------------------------------


def build_assistant(ngrok: str, with_artifact_plan: bool = True) -> dict:
    assistant = {
        "firstMessage": "Hi, this is a Mockingbird test call. Tell me about a project you led.",
        "model": {"provider": "custom-llm", "url": f"{ngrok}/spike/llm", "model": "mockingbird-brain"},
        "server": {"url": f"{ngrok}/spike/webhook"},
        "serverMessages": ["status-update", "speech-update", "user-interrupted", "end-of-call-report"],
        "maxDurationSeconds": 120,
        "silenceTimeoutSeconds": 30,
        "startSpeakingPlan": {"waitSeconds": 0.4},
        "stopSpeakingPlan": {"numWords": 0, "voiceSeconds": 0.2, "backoffSeconds": 1},
        "artifactPlan": {"recordingEnabled": False},
    }
    if not with_artifact_plan:
        del assistant["artifactPlan"]
    return assistant


def discover_ngrok() -> str:
    import httpx

    tunnels = httpx.get("http://127.0.0.1:4040/api/tunnels", timeout=5).json()["tunnels"]
    for t in tunnels:
        if t["public_url"].startswith("https://"):
            return t["public_url"]
    raise SystemExit("no https ngrok tunnel found; run `ngrok http 8000` or pass --ngrok")


def rms(frame: bytes) -> float:
    samples = array.array("h")
    samples.frombytes(frame[: len(frame) // 2 * 2])
    if sys.byteorder == "big":
        samples.byteswap()
    if not samples:
        return 0.0
    return math.sqrt(sum(s * s for s in samples) / len(samples))


class CallState:
    """What the receiver learns about the interviewer's audio, shared with the controller."""

    VOICE_RMS = 300  # int16 RMS above this counts as speech, not line noise
    GAP = 0.8  # seconds without energetic frames before we call the speaker "quiet"

    def __init__(self) -> None:
        self.t0 = time.monotonic()
        self.last_voice_t: float | None = None
        self.rx_bytes = 0
        self.rx_frames = 0
        self.first_frames: list[dict] = []
        self.text_frames: list[dict] = []
        self.pcm = bytearray()
        self.closed = asyncio.Event()
        self.close_info: dict | None = None

    def now(self) -> float:
        return time.monotonic() - self.t0

    def speaking(self) -> bool:
        return self.last_voice_t is not None and self.now() - self.last_voice_t < self.GAP


async def wait_for(cond, timeout: float, what: str, state: CallState) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        if state.closed.is_set():
            print(f"[{state.now():6.2f}] socket closed while waiting for {what}", flush=True)
            return False
        await asyncio.sleep(0.02)
    print(f"[{state.now():6.2f}] TIMEOUT waiting for {what}", flush=True)
    return False


class Streamer:
    """Sends one 20 ms frame every 20 ms: queued speech first, silence otherwise."""

    def __init__(self) -> None:
        self.queue = bytearray()
        self.sent = 0

    def enqueue(self, pcm: bytes) -> None:
        self.queue += pcm

    @property
    def drained(self) -> bool:
        return not self.queue

    async def run(self, ws, state: CallState, stop: asyncio.Event) -> None:
        from websockets.exceptions import ConnectionClosed

        due = time.monotonic()
        while not stop.is_set() and not state.closed.is_set():
            frame = bytes(self.queue[:FRAME_BYTES])
            del self.queue[:FRAME_BYTES]
            frame = frame.ljust(FRAME_BYTES, b"\x00")
            try:
                await ws.send(frame)
            except ConnectionClosed:
                return
            self.sent += 1
            due += FRAME_SECONDS
            delay = due - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            elif delay < -0.5:  # fell far behind; resync instead of bursting
                due = time.monotonic()


async def receiver(ws, state: CallState) -> None:
    from websockets.exceptions import ConnectionClosed

    try:
        async for msg in ws:
            t = state.now()
            if isinstance(msg, (bytes, bytearray)):
                state.rx_bytes += len(msg)
                state.rx_frames += 1
                state.pcm += msg
                if len(state.first_frames) < 20:
                    state.first_frames.append({"i": state.rx_frames, "t": round(t, 4), "bytes": len(msg)})
                if rms(bytes(msg)) > state.VOICE_RMS:
                    state.last_voice_t = t
            else:
                try:
                    parsed = json.loads(msg)
                except ValueError:
                    parsed = None
                state.text_frames.append({"t": round(t, 3), "raw": msg, "json": parsed})
                print(f"[{t:6.2f}] TEXT FRAME {msg[:200]}", flush=True)
    except ConnectionClosed:
        pass
    finally:
        rc = getattr(ws, "close_code", None)
        reason = getattr(ws, "close_reason", None)
        state.close_info = {"t": round(state.now(), 3), "code": rc, "reason": reason}
        state.closed.set()
        print(f"[{state.now():6.2f}] socket closed code={rc} reason={reason!r}", flush=True)


async def create_call(http, api_base: str, key: str, ngrok: str) -> tuple[dict, bool]:
    """POST /call. Returns (response json, artifact_plan_accepted)."""
    headers = {"Authorization": f"Bearer {key}"}
    transport = {
        "provider": "vapi.websocket",
        "audioFormat": {"format": "pcm_s16le", "container": "raw", "sampleRate": SAMPLE_RATE},
    }
    body = {"assistant": build_assistant(ngrok), "transport": transport}
    resp = await http.post(f"{api_base}/call", json=body, headers=headers)
    if resp.status_code < 300:
        return resp.json(), True

    err = {"status": resp.status_code, "body": _json_or_text(resp)}
    write_json(RAW / "create_call_error_with_artifactplan.json", err)
    print(f"POST /call with artifactPlan failed: {resp.status_code} {resp.text[:500]}", flush=True)
    if "artifactPlan" not in resp.text and "recordingEnabled" not in resp.text:
        raise SystemExit("create call failed for a reason other than artifactPlan; see raw/ error file")
    body["assistant"] = build_assistant(ngrok, with_artifact_plan=False)
    resp = await http.post(f"{api_base}/call", json=body, headers=headers)
    if resp.status_code >= 300:
        write_json(RAW / "create_call_error_retry.json", {"status": resp.status_code, "body": _json_or_text(resp)})
        raise SystemExit(f"retry without artifactPlan also failed: {resp.status_code} {resp.text[:500]}")
    return resp.json(), False


def _json_or_text(resp):
    try:
        return resp.json()
    except ValueError:
        return resp.text


async def run_call(args) -> None:
    import httpx
    import websockets

    key = load_env().get("VAPI_API_KEY")
    if not key:
        raise SystemExit("VAPI_API_KEY missing from .env")
    ngrok = (args.ngrok or discover_ngrok()).rstrip("/")
    pcm1 = read_wav(Path(args.wav))
    pcm2 = read_wav(Path(args.wav2)) if args.interrupt_at is not None else b""
    RAW.mkdir(parents=True, exist_ok=True)
    print(f"using public url {ngrok}", flush=True)

    async with httpx.AsyncClient(timeout=30) as http:
        call, artifact_ok = await create_call(http, args.api_base, key, ngrok)
        write_json(RAW / "create_call_response.json", call)
        ws_url = call["transport"]["websocketCallUrl"]
        call_id = call.get("id")
        print(f"call {call_id} created; artifactPlan accepted: {artifact_ok}", flush=True)

        state = CallState()
        streamer = Streamer()
        stop = asyncio.Event()
        marks: dict[str, float] = {}
        async with websockets.connect(ws_url, max_size=None, ping_interval=None) as ws:
            state.t0 = time.monotonic()
            send_task = asyncio.create_task(streamer.run(ws, state, stop))
            recv_task = asyncio.create_task(receiver(ws, state))
            try:
                await drive(args, ws, state, streamer, pcm1, pcm2, marks)
            finally:
                stop.set()
                await send_task
                if not state.closed.is_set():
                    await asyncio.sleep(0)  # let the receiver see any close
                    try:
                        await asyncio.wait_for(recv_task, timeout=1)
                    except asyncio.TimeoutError:
                        pass
                if not recv_task.done():
                    recv_task.cancel()

        # Did the call really end on Vapi's side? Ask the REST API.
        ended = None
        for _ in range(10):
            got = await http.get(
                f"{args.api_base}/call/{call_id}", headers={"Authorization": f"Bearer {key}"}
            )
            ended = got.json() if got.status_code < 300 else {"status_code": got.status_code}
            if ended.get("status") == "ended":
                break
            await asyncio.sleep(1.5)
        write_json(RAW / "get_call_after.json", ended)

    save_call_artifacts(state, streamer, marks, call_id, artifact_ok, ended)


async def drive(args, ws, state, streamer, pcm1, pcm2, marks) -> None:
    """The scripted conversation. Marks record when each step happened."""

    def log(msg: str) -> None:
        print(f"[{state.now():6.2f}] {msg}", flush=True)

    # 1. let the interviewer finish the opening line, so answer 1 is not an interruption
    ok = await wait_for(lambda: state.last_voice_t is not None, 30, "opening line to start", state)
    marks["opening_started"] = state.last_voice_t if ok else -1
    log("opening line started" if ok else "no opening audio heard; answering anyway")
    if ok:
        await wait_for(lambda: not state.speaking(), 30, "opening line to end", state)
        log("opening line ended")
    marks["opening_ended"] = state.now()

    # 2. answer 1
    marks["answer1_start"] = state.now()
    streamer.enqueue(pcm1)
    log(f"streaming answer 1 ({len(pcm1) / 2 / SAMPLE_RATE:.1f} s)")
    await wait_for(lambda: streamer.drained, 60, "answer 1 to finish sending", state)
    marks["answer1_end"] = state.now()

    # 3. wait for the interviewer's reply to start
    ok = await wait_for(
        lambda: state.last_voice_t is not None and state.last_voice_t > marks["answer1_end"],
        30, "interviewer reply to start", state,
    )
    marks["reply1_started"] = state.last_voice_t if ok else -1
    log("interviewer reply started" if ok else "no reply audio heard")

    # 4. interrupt it (or just let it finish)
    if args.interrupt_at is not None and ok:
        await asyncio.sleep(args.interrupt_at)
        marks["answer2_start"] = state.now()
        marks["answer2_start_while_speaking"] = state.speaking()
        streamer.enqueue(pcm2)
        log(f"streaming answer 2 over the reply (interviewer speaking: {state.speaking()})")
        await wait_for(lambda: streamer.drained, 60, "answer 2 to finish sending", state)
        marks["answer2_end"] = state.now()
        after = marks["answer2_end"]
    else:
        after = marks["answer1_end"]

    # 5. let the final reply play out, then give webhooks a moment
    await wait_for(
        lambda: state.last_voice_t is not None and state.last_voice_t > after + 1 and not state.speaking(),
        40, "final reply to end", state,
    )
    log("final reply ended (or timed out)")
    await asyncio.sleep(3)

    # 6. hang up
    marks["end_call_sent"] = state.now()
    await ws.send(json.dumps({"type": "end-call"}))
    log('sent text frame {"type": "end-call"}')
    ok = await wait_for(lambda: state.closed.is_set(), 15, "socket to close after end-call", state)
    marks["closed_after_end_call"] = ok
    log(f"socket closed after end-call: {ok}")


def save_call_artifacts(state, streamer, marks, call_id, artifact_ok, ended) -> None:
    with (RAW / "ws_text_frames.jsonl").open("w") as fh:
        for frame in state.text_frames:
            fh.write(json.dumps(frame) + "\n")
    write_json(RAW / "ws_binary_frames_first20.json", state.first_frames)
    (RAW / "received.pcm").write_bytes(bytes(state.pcm))
    with wave.open(str(RAW / "received_assumed_16k.wav"), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(bytes(state.pcm))

    sizes = sorted({f["bytes"] for f in state.first_frames})
    gaps = [round(b["t"] - a["t"], 4) for a, b in zip(state.first_frames, state.first_frames[1:])]
    duration = state.pcm and len(state.pcm) / 2 / SAMPLE_RATE
    summary = {
        "call_id": call_id,
        "artifact_plan_accepted": artifact_ok,
        "marks": {k: (round(v, 3) if isinstance(v, float) else v) for k, v in marks.items()},
        "frames_sent": streamer.sent,
        "binary_frames_received": state.rx_frames,
        "binary_bytes_received": state.rx_bytes,
        "first_frame_byte_sizes": sizes,
        "first_frame_gaps_s": gaps,
        "received_seconds_if_16k": duration,
        "close": state.close_info,
        "text_frame_count": len(state.text_frames),
        "get_call_status_after": (ended or {}).get("status"),
        "get_call_ended_reason_after": (ended or {}).get("endedReason"),
    }
    write_json(RAW / "call_summary.json", summary)
    print(json.dumps(summary, indent=2), flush=True)


def cmd_call(args) -> None:
    asyncio.run(run_call(args))


# --------------------------------------------------------------------------
# fixtures: scrub the raw captures and copy the ones the plan names
# --------------------------------------------------------------------------

_KEY_REDACT = re.compile(
    r"^(authorization|x-api-key|api[-_]?key|cookie|set-cookie|.*secret.*|.*password.*|.*credential.*|.*token)$",
    re.I,
)
_COUNT_KEY = re.compile(r"(max|prompt|completion|total|input|output)[-_]?tokens?$", re.I)
_JWT = re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}")
_BEARER = re.compile(r"Bearer\s+\S+", re.I)
_SK = re.compile(r"\b(sk|pk|key)[-_][A-Za-z0-9_-]{16,}")
_WS_URL = re.compile(r"^(wss?://[^/]+/).+$")
ZERO_UUID = "00000000-0000-0000-0000-000000000000"


def scrub(value, api_key: str | None, key: str = ""):
    """Return a copy of a JSON value with anything secret-looking replaced."""
    if key == "orgId":
        return ZERO_UUID
    if isinstance(value, dict):
        return {k: scrub(v, api_key, k) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub(v, api_key, key) for v in value]
    if isinstance(value, str):
        if key and _KEY_REDACT.match(key) and not _COUNT_KEY.search(key):
            return "REDACTED"
        if api_key and api_key in value:
            value = value.replace(api_key, "REDACTED")
        value = _BEARER.sub("Bearer REDACTED", value)
        value = _JWT.sub("REDACTED_JWT", value)
        value = _SK.sub("REDACTED_KEY", value)
        # websocketCallUrl, listenUrl and controlUrl are capability URLs: keep host only
        return _WS_URL.sub(r"\1REDACTED", value)
    return value


def cmd_fixtures(args) -> None:
    api_key = load_env().get("VAPI_API_KEY")
    events = [json.loads(line) for line in (RAW / "events.jsonl").read_text().splitlines() if line]
    FIXTURES.mkdir(parents=True, exist_ok=True)
    written: list[str] = []

    def emit(name: str, data) -> None:
        write_json(FIXTURES / name, scrub(data, api_key))
        written.append(name)

    def raw_for(event) -> dict:
        if event["kind"] == "webhook":
            return json.loads((RAW / f"webhook_{event['type']}_{event['seq']:03d}.json").read_text())
        return json.loads((RAW / f"llm_{event['seq']:03d}.json").read_text())

    llm = [e for e in events if e["kind"] in ("llm", "llm_other")]
    hooks = [e for e in events if e["kind"] == "webhook"]

    if llm:
        emit("llm_request_turn1.json", raw_for(llm[0]))
    interrupted = [e for e in hooks if e["type"] == "user-interrupted"]
    if interrupted:
        emit("webhook_user_interrupted.json", raw_for(interrupted[0]))
        later = [e for e in llm if e["seq"] > interrupted[0]["seq"]]
        pick, rule = (later[0], "first LLM request after the first user-interrupted webhook") if later else (None, "")
    else:
        pick, rule = None, ""
    if pick is None and len(llm) > 1:
        pick, rule = llm[1], "second LLM request (no later one than a user-interrupted webhook)"
    if pick:
        emit("llm_request_after_interrupt.json", raw_for(pick))
        print(f"llm_request_after_interrupt.json = seq {pick['seq']}: {rule}")

    seen_speech: set[tuple] = set()
    seen_status: set[str] = set()
    for e in hooks:
        body = raw_for(e)
        msg = body.get("message", {}) if isinstance(body, dict) else {}
        if e["type"] == "speech-update":
            if "webhook_speech_update.json" not in written:
                emit("webhook_speech_update.json", body)
            combo = (msg.get("role"), msg.get("status"))
            if combo not in seen_speech:
                seen_speech.add(combo)
                emit(f"webhook_speech_update_{combo[0]}_{combo[1]}.json", body)
        elif e["type"] == "status-update":
            status = str(msg.get("status"))
            if status not in seen_status:
                seen_status.add(status)
                emit(f"webhook_status_update_{status}.json", body)
        elif e["type"] == "end-of-call-report" and "webhook_end_of_call_report.json" not in written:
            emit("webhook_end_of_call_report.json", body)

    for name in ("create_call_response.json", "create_call_error_with_artifactplan.json"):
        if (RAW / name).exists():
            emit(name, json.loads((RAW / name).read_text()))
    for name in ("ws_binary_frames_first20.json", "call_summary.json", "get_call_after.json"):
        if (RAW / name).exists():
            emit(name, json.loads((RAW / name).read_text()))

    lines = []
    src = RAW / "ws_text_frames.jsonl"
    if src.exists():
        for line in src.read_text().splitlines():
            if line:
                lines.append(json.dumps(scrub(json.loads(line), api_key)))
    (FIXTURES / "ws_text_frames.jsonl").write_text("".join(l + "\n" for l in lines))
    written.append("ws_text_frames.jsonl")

    print("wrote to", FIXTURES)
    for name in sorted(written):
        print("  ", name)


# --------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("serve", help="run the fake brain + webhook server")
    p.add_argument("--port", type=int, default=8000)
    p.set_defaults(fn=cmd_serve)

    p = sub.add_parser("audio", help="make the two spoken answers with say + afconvert")
    p.set_defaults(fn=cmd_audio)

    p = sub.add_parser("call", help="create a Vapi call and stream audio to it")
    p.add_argument("--wav", default=str(SPIKE / "answer1.wav"), help="first answer, 16 kHz mono WAV")
    p.add_argument("--wav2", default=str(SPIKE / "answer2.wav"), help="answer that interrupts the reply")
    p.add_argument("--interrupt-at", type=float, default=None,
                   help="seconds after the reply audio starts to stream --wav2 over it")
    p.add_argument("--ngrok", default=None, help="public https URL (default: ask local ngrok)")
    p.add_argument("--api-base", default="https://api.vapi.ai")
    p.set_defaults(fn=cmd_call)

    p = sub.add_parser("fixtures", help="scrub raw captures into server/tests/fixtures/vapi")
    p.set_defaults(fn=cmd_fixtures)

    args = parser.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
