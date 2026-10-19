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
    uv run scripts/spike_vapi.py selftest                 # offline checks, no Vapi credit

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
from urllib.parse import urlparse

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

# model.url is "<public url>" + LLM_MOUNT. Whatever Vapi appends after it is question 1.
LLM_MOUNT = "/spike/llm"

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

    @app.post(f"{LLM_MOUNT}/chat/completions")
    async def llm_chat(request: Request):
        return await handle_llm(request, expected_path=True)

    # Catch-all: if Vapi appends a different path, save it and still answer so
    # the call keeps going. Question 1 is about exactly this. The bare model.url
    # (nothing appended at all) is a separate route: "{rest:path}" needs the slash.
    @app.post(LLM_MOUNT)
    @app.post(LLM_MOUNT + "/{rest:path}")
    async def llm_other(request: Request, rest: str = ""):
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
        "model": {"provider": "custom-llm", "url": f"{ngrok}{LLM_MOUNT}", "model": "mockingbird-brain"},
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
        self.frames_path: Path | None = None  # each text frame is appended here as it arrives
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
                frame = {"t": round(t, 3), "raw": msg, "json": parsed}
                state.text_frames.append(frame)
                if state.frames_path is not None:
                    # write now, not at the end: a crash or Ctrl-C must not lose question 4's evidence
                    with state.frames_path.open("a") as fh:
                        fh.write(json.dumps(frame) + "\n")
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


async def run_call(args, transport=None) -> None:
    """Create the call, run the scripted conversation, always save what was seen.

    `transport` is only for the offline selftest (an httpx.MockTransport standing in for Vapi's REST API).
    """
    import httpx
    import websockets

    key = load_env().get("VAPI_API_KEY")
    if not key:
        raise SystemExit("VAPI_API_KEY missing from .env")
    ngrok = (args.ngrok or discover_ngrok()).rstrip("/")
    pcm1 = read_wav(Path(args.wav))
    pcm2 = read_wav(Path(args.wav2)) if args.interrupt_at is not None else b""
    RAW.mkdir(parents=True, exist_ok=True)
    # fixtures uses this to replace the tunnel host, which is the user's, not ours to publish
    write_json(RAW / "public_url.json", {"public_url": ngrok})
    print(f"using public url {ngrok}", flush=True)

    async with httpx.AsyncClient(timeout=30, transport=transport) as http:
        call, artifact_ok = await create_call(http, args.api_base, key, ngrok)
        write_json(RAW / "create_call_response.json", call)
        call_id = call.get("id") if isinstance(call, dict) else None
        transport_info = call.get("transport") if isinstance(call, dict) else None
        ws_url = transport_info.get("websocketCallUrl") if isinstance(transport_info, dict) else None
        print(f"call {call_id} created; artifactPlan accepted: {artifact_ok}", flush=True)
        if not ws_url:
            # The response is already saved. Record what Vapi says about the call, then stop cleanly.
            ended = await fetch_call_after(http, args.api_base, key, call_id)
            save_call_artifacts(
                CallState(), Streamer(), {}, call_id, artifact_ok, ended,
                "create-call response has no transport.websocketCallUrl",
            )
            raise SystemExit("create-call response has no transport.websocketCallUrl; create_call_response.json is saved")

        state = CallState()
        state.frames_path = RAW / "ws_text_frames.jsonl"
        state.frames_path.write_text("")  # frames are appended as they arrive
        streamer = Streamer()
        marks: dict[str, float | bool] = {}
        ended: dict | None = None
        run_error: str | None = None
        # From here on the call exists and costs money: whatever happens (socket closed early,
        # an error, Ctrl-C), poll REST and write the summary so the evidence is never lost.
        try:
            async with websockets.connect(ws_url, max_size=None, ping_interval=None) as ws:
                state.t0 = time.monotonic()
                stop = asyncio.Event()
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
        except BaseException as exc:  # recorded in the summary, then re-raised
            run_error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            ended = await fetch_call_after(http, args.api_base, key, call_id)
            save_call_artifacts(state, streamer, marks, call_id, artifact_ok, ended, run_error)


async def fetch_call_after(http, api_base: str, key: str, call_id) -> dict:
    """Did the call really end on Vapi's side? Ask the REST API. Never raises."""
    ended: dict = {}
    for _ in range(10):
        try:
            got = await http.get(f"{api_base}/call/{call_id}", headers={"Authorization": f"Bearer {key}"})
            body = got.json() if got.status_code < 300 else {"status_code": got.status_code}
            ended = body if isinstance(body, dict) else {"unexpected_body": body}
        except Exception as exc:  # network trouble must not hide the rest of the evidence
            ended = {"poll_error": f"{type(exc).__name__}: {exc}"}
        if ended.get("status") == "ended":
            break
        await asyncio.sleep(1.5)
    write_json(RAW / "get_call_after.json", ended)
    return ended


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
    if not state.closed.is_set():
        await asyncio.sleep(3)

    # 6. hang up
    await send_end_call(ws, state, marks, log)


async def send_end_call(ws, state: CallState, marks: dict, log=print) -> None:
    """Send {"type": "end-call"} unless Vapi already hung up (silence timeout, error, max duration).

    Question 7 is only answered when the frame really went out, so `end_call_sent` and
    `closed_after_end_call` are only recorded then; `closed_before_end_call` says which case it was.
    """
    from websockets.exceptions import ConnectionClosed

    if state.closed.is_set():
        marks["closed_before_end_call"] = True
        log("socket already closed; end-call not sent")
        return
    try:
        await ws.send(json.dumps({"type": "end-call"}))
    except ConnectionClosed:
        marks["closed_before_end_call"] = True
        log("socket closed just as end-call was sent; not honored or refused")
        return
    marks["closed_before_end_call"] = False
    marks["end_call_sent"] = state.now()
    log('sent text frame {"type": "end-call"}')
    ok = await wait_for(lambda: state.closed.is_set(), 15, "socket to close after end-call", state)
    marks["closed_after_end_call"] = ok
    log(f"socket closed after end-call: {ok}")


def save_call_artifacts(state, streamer, marks, call_id, artifact_ok, ended, run_error=None) -> None:
    # ws_text_frames.jsonl is not written here: the receiver appends each frame as it arrives.
    write_json(RAW / "ws_binary_frames_first20.json", state.first_frames)
    (RAW / "received.pcm").write_bytes(bytes(state.pcm))
    with wave.open(str(RAW / "received_assumed_16k.wav"), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(bytes(state.pcm))

    sizes = sorted({f["bytes"] for f in state.first_frames})
    gaps = [round(b["t"] - a["t"], 4) for a, b in zip(state.first_frames, state.first_frames[1:])]
    duration = round(len(state.pcm) / 2 / SAMPLE_RATE, 3)  # 0.0 when no audio came back
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
        "aborted_with": run_error,
    }
    write_json(RAW / "call_summary.json", summary)
    print(json.dumps(summary, indent=2), flush=True)


def cmd_call(args) -> None:
    asyncio.run(run_call(args))


# --------------------------------------------------------------------------
# fixtures: scrub the raw captures and copy the ones the plan names
# --------------------------------------------------------------------------

_KEY_REDACT = re.compile(
    r"^(authorization|x-api-key|cookie|set-cookie|.*api[-_]?key.*|.*secret.*|.*password.*|.*credential.*|.*token"
    r"|x-forwarded-for|x-real-ip|forwarded|cf-connecting-ip|true-client-ip)$",
    re.I,
)
_COUNT_KEY = re.compile(r"(max|prompt|completion|total|input|output)[-_]?tokens?$", re.I)
# Keys whose value is a capability or recording URL. Redacted by key, whatever the URL scheme.
_URL_KEY = re.compile(
    r"^(websocketCallUrl|listenUrl|controlUrl|logUrl|pcapUrl|recordingUrl|.*RecordingUrl|.*CallUrl)$", re.I
)
_ORG_KEY = re.compile(r"^org[-_]?id$", re.I)
_JWT = re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}")
_BEARER = re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]+", re.I)
_SK = re.compile(r"\b(sk|pk|key)[-_][A-Za-z0-9_-]{16,}")
_URL_WHOLE = re.compile(r"^((?:wss?|https?)://[^/\s]+/).+$", re.I | re.S)
# Anywhere inside a longer string: every ws(s) URL, and every path on a vapi.ai host (call-scoped URLs).
_WS_IN_TEXT = re.compile(r"\b(wss?://[^/\s\"'\\]+/)[^\s\"'\\]+", re.I)
_VAPI_IN_TEXT = re.compile(r"\b(https?://[^/\s\"'\\]*vapi\.ai/)[^\s\"'\\]+", re.I)
_NGROK_HOST = re.compile(
    r"\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.ngrok(?:-free)?\.(?:app|dev|io|pizza|pro)\b", re.I
)
ZERO_UUID = "00000000-0000-0000-0000-000000000000"
TUNNEL_PLACEHOLDER = "tunnel.example"


def scrub(value, literals: dict[str, str], key: str = "", redact: bool = False):
    """Return a copy of a JSON value with anything secret-looking replaced.

    `literals` maps an exact secret string (the API key, the tunnel host) to its replacement.
    `redact` is set once an enclosing key marked the whole subtree secret.
    """
    if _ORG_KEY.match(key):
        return ZERO_UUID
    secret_key = bool(key) and bool(_KEY_REDACT.match(key)) and not _COUNT_KEY.search(key)
    redact = redact or secret_key
    if isinstance(value, dict):
        return {k: scrub(v, literals, k, redact) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub(v, literals, key, redact) for v in value]
    if not isinstance(value, str):
        return value
    if redact:
        return "REDACTED"
    if _URL_KEY.match(key):
        # capability / recording URLs: keep the scheme and host, drop everything after
        whole = _URL_WHOLE.match(value)
        return whole.group(1) + "REDACTED" if whole else "REDACTED"
    for literal, replacement in literals.items():
        if literal and literal in value:
            value = value.replace(literal, replacement)
    stripped = value.strip()
    if stripped[:1] in ("{", "["):
        # JSON hidden inside a string (a raw copy of a frame, tool-call arguments): scrub it by key too
        try:
            inner = json.loads(stripped)
        except ValueError:
            inner = None
        if isinstance(inner, (dict, list)):
            cleaned = scrub(inner, literals)
            if cleaned != inner:
                value = json.dumps(cleaned, ensure_ascii=False)
    value = _BEARER.sub("Bearer REDACTED", value)
    value = _JWT.sub("REDACTED_JWT", value)
    value = _SK.sub("REDACTED_KEY", value)
    value = _NGROK_HOST.sub(TUNNEL_PLACEHOLDER, value)
    value = _WS_IN_TEXT.sub(r"\1REDACTED", value)
    return _VAPI_IN_TEXT.sub(r"\1REDACTED", value)


# What must never appear in a published fixture. Each is checked on the scrubbed text, as a second
# line of defence: if scrub() misses something, fixtures fails loudly instead of writing it.
_LEAK_PATTERNS = [
    ("a Bearer token", re.compile(r"Bearer\s+(?!REDACTED)", re.I)),
    ("JWT-looking text (eyJ)", re.compile(r"eyJ")),
    ("an sk-/pk-/key- style key", _SK),
    ("a ws(s):// URL with a path", re.compile(r"wss?://[^/\s\"'\\]+/(?!REDACTED)", re.I)),
    ("a path on a vapi.ai host", re.compile(r"vapi\.ai/(?!REDACTED)[^\s\"'\\]", re.I)),
    (
        "a control/listen/transport URL",
        re.compile(r"(?:wss?|https?)://[^\s\"'\\]*/(?:control|listen|transport)\b", re.I),
    ),
    ("an ngrok host", re.compile(r"\.ngrok(?:-free)?\.(?:app|dev|io|pizza|pro)\b", re.I)),
    ("an orgId that is not the zero UUID", re.compile(r'"orgId"\s*:\s*"(?!0{8}-0{4}-0{4}-0{4}-0{12}")', re.I)),
]


def find_leaks(text: str, forbidden: list[str]) -> list[str]:
    """Names of the leak kinds found in `text`. Never returns the matched text itself."""
    found = [name for name, pattern in _LEAK_PATTERNS if pattern.search(text)]
    if any(f and f in text for f in forbidden):
        found.append("a literal secret (the API key or the tunnel host)")
    return found


def cmd_fixtures(args) -> None:
    api_key = load_env().get("VAPI_API_KEY")
    literals: dict[str, str] = {}
    if api_key:
        literals[api_key] = "REDACTED"
    if (RAW / "public_url.json").exists():
        host = urlparse(json.loads((RAW / "public_url.json").read_text()).get("public_url", "")).hostname
        if host:
            literals[host] = TUNNEL_PLACEHOLDER
    forbidden = list(literals)
    events = [json.loads(line) for line in (RAW / "events.jsonl").read_text().splitlines() if line]
    FIXTURES.mkdir(parents=True, exist_ok=True)
    written: list[str] = []

    def write_fixture(name: str, text: str) -> None:
        leaks = find_leaks(text, forbidden)
        if leaks:
            raise SystemExit(f"refusing to write {name}: still contains {', '.join(leaks)} after scrubbing")
        (FIXTURES / name).write_text(text)
        written.append(name)

    def emit(name: str, data) -> None:
        write_fixture(name, json.dumps(scrub(data, literals), indent=2, ensure_ascii=False) + "\n")

    def raw_for(event) -> dict:
        if event["kind"] == "webhook":
            return json.loads((RAW / f"webhook_{event['type']}_{event['seq']:03d}.json").read_text())
        return json.loads((RAW / f"llm_{event['seq']:03d}.json").read_text())

    llm = [e for e in events if e["kind"] in ("llm", "llm_other")]
    hooks = [e for e in events if e["kind"] == "webhook"]
    fixture_for_seq: dict[int, str] = {}

    if llm:
        emit("llm_request_turn1.json", raw_for(llm[0]))
        fixture_for_seq[llm[0]["seq"]] = "llm_request_turn1.json"
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
        fixture_for_seq.setdefault(pick["seq"], "llm_request_after_interrupt.json")
        print(f"llm_request_after_interrupt.json = seq {pick['seq']}: {rule}")

    # Question 1 evidence: the path Vapi appends to model.url, plus the query and headers it sent.
    paths = []
    for e in llm:
        meta_file = RAW / f"llm_{e['seq']:03d}.meta.json"
        meta = json.loads(meta_file.read_text()) if meta_file.exists() else {}
        path = meta.get("path", e["path"])
        paths.append(
            {
                "seq": e["seq"],
                "method": meta.get("method", "POST"),
                "path": path,
                "appended_to_model_url": path[len(LLM_MOUNT):] if path.startswith(LLM_MOUNT) else None,
                "matched_chat_completions_route": e["kind"] == "llm",
                "query": meta.get("query", e.get("query", "")),
                "headers": meta.get("headers", {}),
                "body_fixture": fixture_for_seq.get(e["seq"]),
            }
        )
    if paths:
        emit("llm_request_paths.json", paths)

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
            if not line:
                continue
            frame = json.loads(line)
            if frame.get("json") is not None:
                frame.pop("raw", None)  # same payload twice; the parsed copy is the one scrub() can read by key
            lines.append(json.dumps(scrub(frame, literals)))
    write_fixture("ws_text_frames.jsonl", "".join(l + "\n" for l in lines))

    sweep_fixtures(forbidden)
    print("wrote to", FIXTURES)
    for name in sorted(written):
        print("  ", name)
    print("Scrubbing is automatic but not proof. Read every file above before you commit it.")


def sweep_fixtures(forbidden: list[str]) -> None:
    """Re-read everything in the fixtures directory (also stale files from earlier runs)."""
    problems = []
    for path in sorted(FIXTURES.rglob("*")):
        if path.is_file():
            for leak in find_leaks(path.read_text(errors="replace"), forbidden):
                problems.append(f"{path.relative_to(FIXTURES)}: {leak}")
    if problems:
        raise SystemExit("fixtures are NOT safe to commit:\n  " + "\n  ".join(problems))


# --------------------------------------------------------------------------
# selftest: offline checks of the parts that decide whether the one paid run is wasted
# --------------------------------------------------------------------------

FAKE_KEY = "test-key-0123456789abcdefghij"
FAKE_ORG = "11111111-aaaa-bbbb-cccc-222222222222"
FAKE_CALL = "99999999-8888-7777-6666-555555555555"
CAP_LISTEN = f"wss://phone-call-websocket.aws-us-west-2-backend-production2.vapi.ai/{FAKE_CALL}/listen"
CAP_CONTROL = f"https://phone-call-websocket.aws-us-west-2-backend-production2.vapi.ai/{FAKE_CALL}/control"
CAP_TRANSPORT = f"wss://phone-call-websocket.aws-us-west-2-backend-production2.vapi.ai/{FAKE_CALL}/transport"
FAKE_NGROK = "https://abcd-12-34-56-78.ngrok-free.app"


def _tone(seconds: float, amplitude: int = 8000) -> bytes:
    n = int(seconds * SAMPLE_RATE)
    samples = array.array("h", (int(amplitude * math.sin(2 * math.pi * 440 * i / SAMPLE_RATE)) for i in range(n)))
    if sys.byteorder == "big":
        samples.byteswap()
    return samples.tobytes()


def _write_wav(path: Path, pcm: bytes) -> None:
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(pcm)


class _Sandbox:
    """Point the module's paths at a temp dir and give it a fake API key, then put everything back."""

    def __enter__(self):
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        g = globals()
        self.saved = {k: g[k] for k in ("SPIKE", "RAW", "FIXTURES", "ENV_FILE")}
        self.saved_env = os.environ.get("VAPI_API_KEY")
        g.update(SPIKE=root / "spike", RAW=root / "spike" / "raw", FIXTURES=root / "fixtures", ENV_FILE=root / ".env")
        os.environ["VAPI_API_KEY"] = FAKE_KEY
        RAW.mkdir(parents=True)
        return root

    def __exit__(self, *exc):
        globals().update(self.saved)
        if self.saved_env is None:
            os.environ.pop("VAPI_API_KEY", None)
        else:
            os.environ["VAPI_API_KEY"] = self.saved_env
        self.tmp.cleanup()


def _fake_vapi_rest(ws_port: int, with_socket: bool = True):
    """An httpx.MockTransport that answers POST /call and GET /call/{id} like Vapi, capability URLs and all."""
    import httpx

    call_object = {
        "id": FAKE_CALL,
        "orgId": FAKE_ORG,
        "status": "ended",
        "endedReason": "customer-ended-call",
        "transport": {"provider": "vapi.websocket", "websocketCallUrl": f"ws://127.0.0.1:{ws_port}/{FAKE_CALL}/transport"},
        "monitor": {"listenUrl": CAP_LISTEN, "controlUrl": CAP_CONTROL},
    }

    def handler(request: "httpx.Request") -> "httpx.Response":
        assert request.headers["authorization"] == f"Bearer {FAKE_KEY}"
        if request.method == "POST" and request.url.path == "/call":
            body = json.loads(request.content)
            assert body["assistant"]["artifactPlan"] == {"recordingEnabled": False}
            assert body["assistant"]["model"]["url"] == FAKE_NGROK + LLM_MOUNT
            created = {**call_object, "status": "queued"}
            if not with_socket:
                created["transport"] = {"provider": "vapi.websocket"}
            return httpx.Response(201, json=created)
        if request.method == "GET" and request.url.path == f"/call/{FAKE_CALL}":
            return httpx.Response(200, json=call_object)
        return httpx.Response(404, json={"message": "unexpected request"})

    return httpx.MockTransport(handler)


def _call_args(root: Path, **overrides):
    wav = root / "tone.wav"
    _write_wav(wav, _tone(0.5))
    values = dict(wav=str(wav), wav2=str(wav), interrupt_at=None, ngrok=FAKE_NGROK, api_base="http://vapi.test")
    values.update(overrides)
    return argparse.Namespace(**values)


async def _drain(ws) -> None:
    """A real server keeps reading; one that does not stalls the close handshake for 10 s."""
    try:
        async for _ in ws:
            pass
    except Exception:
        pass


async def _with_fake_socket(handler, body):
    import websockets

    server = await websockets.serve(handler, "127.0.0.1", 0)
    try:
        return await body(server.sockets[0].getsockname()[1])
    finally:
        server.close()
        await server.wait_closed()


def test_scrub_hides_capability_urls_by_key_and_by_shape():
    payload = {
        "monitor": {"listenUrl": CAP_LISTEN, "controlUrl": CAP_CONTROL},
        "transport": {"websocketCallUrl": CAP_TRANSPORT},
        "artifact": {
            "recordingUrl": f"https://storage.vapi.ai/{FAKE_CALL}.wav",
            "stereoRecordingUrl": f"https://storage.vapi.ai/{FAKE_CALL}-stereo.wav",
            "logUrl": f"https://logs.vapi.ai/{FAKE_CALL}.log",
        },
        "note": f"connect to {CAP_TRANSPORT} now, or poll https://api.vapi.ai/call/{FAKE_CALL}.",
    }
    out = scrub(payload, {})
    text = json.dumps(out)
    assert out["monitor"]["controlUrl"] == "https://phone-call-websocket.aws-us-west-2-backend-production2.vapi.ai/REDACTED"
    assert out["monitor"]["listenUrl"].endswith("vapi.ai/REDACTED")
    assert out["artifact"]["recordingUrl"] == "https://storage.vapi.ai/REDACTED"
    assert out["artifact"]["stereoRecordingUrl"] == "https://storage.vapi.ai/REDACTED"
    assert out["artifact"]["logUrl"] == "https://logs.vapi.ai/REDACTED"
    assert FAKE_CALL not in text, text
    assert not find_leaks(text, []), find_leaks(text, [])
    # The key rule is what protects URLs on hosts the shape rule does not know (storage buckets, a proxy).
    elsewhere = {
        "monitor": {"controlUrl": f"https://call-control.example.net/{FAKE_CALL}/c", "listenUrl": f"wss://l.example.net/{FAKE_CALL}"},
        "artifact": {
            "recordingUrl": f"https://bucket.s3.amazonaws.com/{FAKE_CALL}.wav",
            "stereoRecordingUrl": f"https://bucket.s3.amazonaws.com/{FAKE_CALL}-s.wav",
            "logUrl": f"https://logs.example.net/{FAKE_CALL}.log",
        },
    }
    out = scrub(elsewhere, {})
    assert out["monitor"] == {"controlUrl": "https://call-control.example.net/REDACTED", "listenUrl": "wss://l.example.net/REDACTED"}
    assert out["artifact"]["recordingUrl"] == "https://bucket.s3.amazonaws.com/REDACTED"
    assert out["artifact"]["stereoRecordingUrl"] == "https://bucket.s3.amazonaws.com/REDACTED"
    assert out["artifact"]["logUrl"] == "https://logs.example.net/REDACTED"
    assert FAKE_CALL not in json.dumps(out)


def test_scrub_redacts_a_whole_secret_subtree_and_nested_json():
    payload = {
        "secret": {"value": "hunter2", "list": ["a", {"b": "c"}]},
        "credentials": [{"id": "cred-1", "apiKey": "xyz"}],
        "server": {"url": FAKE_NGROK + "/spike/webhook", "headers": {"Authorization": "Bearer abc.def"}},
        "orgId": FAKE_ORG,
        "max_tokens": 200,
        "frame_copy": json.dumps({"orgId": FAKE_ORG, "url": CAP_TRANSPORT, "token": "t0k3n"}),
    }
    out = scrub(payload, {"abcd-12-34-56-78.ngrok-free.app": TUNNEL_PLACEHOLDER})
    text = json.dumps(out)
    assert out["secret"] == {"value": "REDACTED", "list": ["REDACTED", {"b": "REDACTED"}]}
    assert out["credentials"] == [{"id": "REDACTED", "apiKey": "REDACTED"}]
    assert out["server"]["url"] == f"https://{TUNNEL_PLACEHOLDER}/spike/webhook"
    assert out["server"]["headers"]["Authorization"] == "REDACTED"
    assert out["orgId"] == ZERO_UUID and out["max_tokens"] == 200
    inner = json.loads(out["frame_copy"])
    assert inner == {"orgId": ZERO_UUID, "url": inner["url"], "token": "REDACTED"}, inner
    assert inner["url"].endswith("vapi.ai/REDACTED")
    for needle in (FAKE_ORG, "hunter2", "t0k3n", "ngrok", FAKE_CALL):
        assert needle not in text, needle


def test_scrub_handles_key_literals_bearer_and_jwt_in_free_text():
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.c2lnbmF0dXJl"
    text = f"call with Bearer {FAKE_KEY} and {jwt}, key {FAKE_KEY}, host eu.ngrok.io, sk-abcdefghijklmnopqrstuvwxyz"
    out = scrub(text, {FAKE_KEY: "REDACTED"})
    assert FAKE_KEY not in out and jwt not in out and "ngrok" not in out and "sk-abc" not in out, out
    assert "Bearer REDACTED" in out


def test_find_leaks_flags_what_scrub_might_miss():
    clean = json.dumps({"orgId": ZERO_UUID, "u": "wss://h.vapi.ai/REDACTED", "auth": "Bearer REDACTED"})
    assert find_leaks(clean, [FAKE_KEY]) == []
    dirty = {
        "a Bearer token": "Authorization: Bearer abc123",
        "JWT-looking text (eyJ)": "x eyJabc y",
        "a ws(s):// URL with a path": "wss://h.example/secret-path",
        "a path on a vapi.ai host": "https://api.vapi.ai/call/abc",
        "a control/listen/transport URL": "https://h.example/x/control",
        "an ngrok host": "abc.ngrok-free.app",
        "an orgId that is not the zero UUID": f'"orgId": "{FAKE_ORG}"',
        "a literal secret (the API key or the tunnel host)": f"leaked {FAKE_KEY}",
    }
    for name, text in dirty.items():
        assert name in find_leaks(text, [FAKE_KEY]), (name, find_leaks(text, [FAKE_KEY]))


def test_summary_is_written_when_no_audio_arrived():
    with _Sandbox():
        async def go():
            state, streamer = CallState(), Streamer()
            save_call_artifacts(state, streamer, {"closed_before_end_call": True}, FAKE_CALL, True, None)

        asyncio.run(go())
        summary = json.loads((RAW / "call_summary.json").read_text())
        assert summary["received_seconds_if_16k"] == 0.0, summary
        assert summary["marks"]["closed_before_end_call"] is True
        assert (RAW / "received.pcm").read_bytes() == b""


def test_end_call_is_not_sent_into_a_closed_socket():
    from websockets.exceptions import ConnectionClosedOK

    class DeadSocket:
        sent = 0

        async def send(self, data):
            DeadSocket.sent += 1
            raise ConnectionClosedOK(None, None)

    async def go():
        # 1. we already saw the close: do not even try
        state = CallState()
        state.closed.set()
        marks: dict = {}
        await send_end_call(DeadSocket(), state, marks, lambda m: None)
        assert marks == {"closed_before_end_call": True} and DeadSocket.sent == 0, marks
        # 2. the close races the send: swallow ConnectionClosed
        state, marks = CallState(), {}
        await send_end_call(DeadSocket(), state, marks, lambda m: None)
        assert marks == {"closed_before_end_call": True} and DeadSocket.sent == 1, marks

    asyncio.run(go())


def test_call_survives_the_socket_closing_early_and_keeps_the_evidence():
    async def handler(ws):
        drain = asyncio.create_task(_drain(ws))
        await ws.send(json.dumps({"type": "transport-ready", "listenUrl": CAP_LISTEN}))
        await asyncio.sleep(0.4)
        await ws.close(code=1011, reason="pipeline-error")
        await drain

    with _Sandbox() as root:
        async def body(port):
            await run_call(_call_args(root), transport=_fake_vapi_rest(port))

        asyncio.run(_with_fake_socket(handler, body))
        summary = json.loads((RAW / "call_summary.json").read_text())
        assert summary["close"]["code"] == 1011 and summary["close"]["reason"] == "pipeline-error", summary
        assert summary["marks"]["closed_before_end_call"] is True
        assert "end_call_sent" not in summary["marks"]
        assert summary["received_seconds_if_16k"] == 0.0 and summary["text_frame_count"] == 1
        assert summary["get_call_status_after"] == "ended" and summary["aborted_with"] is None
        frames = (RAW / "ws_text_frames.jsonl").read_text().splitlines()
        assert len(frames) == 1 and json.loads(frames[0])["json"]["type"] == "transport-ready"
        assert json.loads((RAW / "get_call_after.json").read_text())["id"] == FAKE_CALL


def test_an_error_mid_run_still_writes_frames_summary_and_get_call():
    async def handler(ws):
        await ws.send(json.dumps({"type": "hello"}))
        await _drain(ws)

    async def exploding_drive(args, ws, state, streamer, pcm1, pcm2, marks):
        marks["opening_started"] = 0.1
        await asyncio.sleep(0.3)
        raise RuntimeError("boom")

    with _Sandbox() as root:
        real_drive = globals()["drive"]
        globals()["drive"] = exploding_drive
        try:
            async def body(port):
                try:
                    await run_call(_call_args(root), transport=_fake_vapi_rest(port))
                except RuntimeError as exc:
                    assert str(exc) == "boom"
                else:
                    raise AssertionError("run_call swallowed the error")

            asyncio.run(_with_fake_socket(handler, body))
        finally:
            globals()["drive"] = real_drive
        summary = json.loads((RAW / "call_summary.json").read_text())
        assert summary["aborted_with"] == "RuntimeError: boom", summary
        assert summary["marks"] == {"opening_started": 0.1} and summary["text_frame_count"] == 1
        assert len((RAW / "ws_text_frames.jsonl").read_text().splitlines()) == 1
        assert (RAW / "get_call_after.json").exists() and (RAW / "received.pcm").exists()


def test_full_call_still_ends_on_end_call():
    async def handler(ws):
        loop = asyncio.get_running_loop()

        async def speak(seconds):
            tone = _tone(seconds)
            for i in range(0, len(tone), FRAME_BYTES):
                await ws.send(tone[i : i + FRAME_BYTES])
                await asyncio.sleep(FRAME_SECONDS)

        await ws.send(json.dumps({"type": "hello"}))
        await speak(0.6)  # the opening line
        last_loud, replied, reply_task = None, False, None
        async for msg in ws:
            if isinstance(msg, bytes):
                now = loop.time()
                if rms(msg) > CallState.VOICE_RMS:
                    last_loud = now
                elif last_loud is not None and not replied and now - last_loud > 0.3:
                    replied = True
                    reply_task = asyncio.create_task(speak(1.5))
            elif json.loads(msg).get("type") == "end-call":
                await ws.close(code=1000, reason="end-call honored")
                return

    with _Sandbox() as root:
        async def body(port):
            await run_call(_call_args(root), transport=_fake_vapi_rest(port))

        asyncio.run(_with_fake_socket(handler, body))
        summary = json.loads((RAW / "call_summary.json").read_text())
        marks = summary["marks"]
        assert marks["closed_before_end_call"] is False and marks["closed_after_end_call"] is True, marks
        assert marks["reply1_started"] > marks["answer1_end"] and "end_call_sent" in marks, marks
        assert summary["close"]["code"] == 1000 and summary["received_seconds_if_16k"] > 1.5, summary
        assert summary["first_frame_byte_sizes"] == [FRAME_BYTES] and summary["aborted_with"] is None


def test_the_bare_model_url_and_any_appended_path_get_an_answer_and_are_recorded():
    from fastapi.testclient import TestClient

    with _Sandbox():
        client = TestClient(build_app())
        for path in (LLM_MOUNT, LLM_MOUNT + "/", LLM_MOUNT + "/v1/chat/completions", LLM_MOUNT + "/chat/completions"):
            resp = client.post(path, json={"messages": [], "call": {"id": "c1"}})
            assert resp.status_code == 200 and resp.text.rstrip().endswith("data: [DONE]"), (path, resp.status_code)
            assert '"finish_reason": "stop"' in resp.text
        events = [json.loads(line) for line in (RAW / "events.jsonl").read_text().splitlines()]
        assert [(e["kind"], e["path"]) for e in events] == [
            ("llm_other", LLM_MOUNT),
            ("llm_other", LLM_MOUNT + "/"),
            ("llm_other", LLM_MOUNT + "/v1/chat/completions"),
            ("llm", LLM_MOUNT + "/chat/completions"),
        ], events
        for e in events:
            assert json.loads((RAW / f"llm_{e['seq']:03d}.json").read_text())["call"] == {"id": "c1"}
            assert json.loads((RAW / f"llm_{e['seq']:03d}.meta.json").read_text())["path"] == e["path"]


def test_a_create_call_response_without_a_socket_url_is_saved_and_exits_cleanly():
    with _Sandbox() as root:
        async def go():
            try:
                await run_call(_call_args(root), transport=_fake_vapi_rest(0, with_socket=False))
            except SystemExit as exc:
                assert "websocketCallUrl" in str(exc), exc
            else:
                raise AssertionError("run_call carried on without a socket URL")

        asyncio.run(go())
        saved = json.loads((RAW / "create_call_response.json").read_text())
        assert saved["id"] == FAKE_CALL and "websocketCallUrl" not in saved["transport"], saved
        summary = json.loads((RAW / "call_summary.json").read_text())
        assert summary["call_id"] == FAKE_CALL and "websocketCallUrl" in summary["aborted_with"], summary
        assert summary["get_call_status_after"] == "ended"
        assert (RAW / "get_call_after.json").exists()


def _seed_raw_capture() -> None:
    """A synthetic capture shaped like what serve + call would leave in RAW."""
    events = [
        {"seq": 1, "t": 1.0, "kind": "webhook", "type": "status-update"},
        {"seq": 2, "t": 2.0, "kind": "llm", "path": f"{LLM_MOUNT}/chat/completions", "query": ""},
        {"seq": 3, "t": 3.0, "kind": "webhook", "type": "speech-update"},
        {"seq": 4, "t": 4.0, "kind": "webhook", "type": "user-interrupted"},
        {"seq": 5, "t": 5.0, "kind": "llm_other", "path": f"{LLM_MOUNT}/v1/chat/completions", "query": "x=1"},
        {"seq": 6, "t": 6.0, "kind": "webhook", "type": "end-of-call-report"},
    ]
    (RAW / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
    call = {
        "id": FAKE_CALL,
        "orgId": FAKE_ORG,
        "monitor": {"listenUrl": CAP_LISTEN, "controlUrl": CAP_CONTROL},
        "assistant": {"model": {"url": FAKE_NGROK + LLM_MOUNT}, "server": {"url": FAKE_NGROK + "/spike/webhook"}},
    }
    messages = [{"role": "user", "content": "hello"}]
    meta = {
        "method": "POST",
        "query": "",
        "headers": {
            "host": "abcd-12-34-56-78.ngrok-free.app",
            "authorization": "REDACTED",
            "x-forwarded-for": "203.0.113.9",
            "user-agent": "vapi",
        },
    }
    write_json(RAW / "llm_002.json", {"call": call, "messages": messages})
    write_json(RAW / "llm_002.meta.json", {**meta, "path": f"{LLM_MOUNT}/chat/completions"})
    write_json(RAW / "llm_005.json", {"call": call, "messages": messages + messages})
    write_json(RAW / "llm_005.meta.json", {**meta, "path": f"{LLM_MOUNT}/v1/chat/completions", "query": "x=1"})
    write_json(RAW / "webhook_status-update_001.json", {"message": {"type": "status-update", "status": "in-progress", "call": call}})
    write_json(RAW / "webhook_speech-update_003.json", {"message": {"type": "speech-update", "role": "assistant", "status": "started"}})
    write_json(RAW / "webhook_user-interrupted_004.json", {"message": {"type": "user-interrupted"}})
    write_json(
        RAW / "webhook_end-of-call-report_006.json",
        {"message": {"type": "end-of-call-report", "artifact": {"recordingUrl": f"https://storage.vapi.ai/{FAKE_CALL}.wav"}, "call": call}},
    )
    write_json(RAW / "create_call_response.json", {**call, "transport": {"websocketCallUrl": CAP_TRANSPORT}})
    write_json(RAW / "get_call_after.json", call)
    write_json(RAW / "call_summary.json", {"call_id": FAKE_CALL, "marks": {}, "close": {"code": 1000, "reason": ""}})
    write_json(RAW / "ws_binary_frames_first20.json", [{"i": 1, "t": 0.1, "bytes": 640}])
    write_json(RAW / "public_url.json", {"public_url": FAKE_NGROK})
    frame_json = {"type": "transport-ready", "orgId": FAKE_ORG, "listenUrl": CAP_LISTEN, "token": "abc"}
    (RAW / "ws_text_frames.jsonl").write_text(
        json.dumps({"t": 0.5, "raw": json.dumps(frame_json), "json": frame_json}) + "\n"
        + json.dumps({"t": 0.9, "raw": f"not json, but mentions {CAP_TRANSPORT}", "json": None}) + "\n"
    )


def test_fixtures_publish_the_path_evidence_and_nothing_secret():
    with _Sandbox():
        _seed_raw_capture()
        cmd_fixtures(argparse.Namespace())
        names = sorted(p.name for p in FIXTURES.iterdir())
        for expected in (
            "llm_request_paths.json", "llm_request_turn1.json", "llm_request_after_interrupt.json",
            "webhook_speech_update.json", "webhook_user_interrupted.json", "webhook_status_update_in-progress.json",
            "webhook_end_of_call_report.json", "create_call_response.json", "ws_text_frames.jsonl",
        ):
            assert expected in names, (expected, names)
        paths = json.loads((FIXTURES / "llm_request_paths.json").read_text())
        assert [p["appended_to_model_url"] for p in paths] == ["/chat/completions", "/v1/chat/completions"], paths
        assert paths[0]["query"] == "" and paths[1]["query"] == "x=1"
        assert paths[0]["matched_chat_completions_route"] is True and paths[1]["matched_chat_completions_route"] is False
        assert paths[0]["body_fixture"] == "llm_request_turn1.json"
        assert paths[1]["body_fixture"] == "llm_request_after_interrupt.json"
        assert paths[0]["headers"]["user-agent"] == "vapi" and paths[0]["headers"]["x-forwarded-for"] == "REDACTED"
        assert paths[0]["headers"]["host"] == TUNNEL_PLACEHOLDER
        frames = [json.loads(line) for line in (FIXTURES / "ws_text_frames.jsonl").read_text().splitlines()]
        assert "raw" not in frames[0] and frames[0]["json"]["orgId"] == ZERO_UUID and frames[0]["json"]["token"] == "REDACTED"
        assert frames[1]["json"] is None and frames[1]["raw"].endswith("vapi.ai/REDACTED")
        everything = "".join(p.read_text() for p in FIXTURES.iterdir())
        # the bare call id is kept (it is an id, not a credential); the URLs that carried it are gone
        for needle in (FAKE_ORG, f"/{FAKE_CALL}", "ngrok", "203.0.113.9", "abc\"", FAKE_KEY):
            assert needle not in everything, needle
        assert FAKE_CALL in (FIXTURES / "create_call_response.json").read_text()
        assert find_leaks(everything, [FAKE_KEY]) == []


def test_fixtures_refuse_to_write_what_scrub_missed():
    with _Sandbox():
        _seed_raw_capture()
        # "eyJ" followed by too little to look like a full JWT: scrub() lets it through, the check must not
        write_json(RAW / "webhook_end-of-call-report_006.json", {"message": {"type": "end-of-call-report", "note": "eyJabc"}})
        try:
            cmd_fixtures(argparse.Namespace())
        except SystemExit as exc:
            assert "JWT-looking text" in str(exc) and "webhook_end_of_call_report.json" in str(exc), exc
        else:
            raise AssertionError("fixtures wrote a file that still looks like it holds a JWT")
        assert not (FIXTURES / "webhook_end_of_call_report.json").exists()


def test_the_sweep_catches_stale_dirty_files():
    with _Sandbox():
        _seed_raw_capture()
        FIXTURES.mkdir(parents=True)
        (FIXTURES / "old.json").write_text(f'{{"u": "{CAP_CONTROL}"}}')
        try:
            cmd_fixtures(argparse.Namespace())
        except SystemExit as exc:
            assert "old.json" in str(exc) and "NOT safe to commit" in str(exc), exc
        else:
            raise AssertionError("sweep ignored a dirty file already in the fixtures directory")


SELFTESTS = [
    test_scrub_hides_capability_urls_by_key_and_by_shape,
    test_scrub_redacts_a_whole_secret_subtree_and_nested_json,
    test_scrub_handles_key_literals_bearer_and_jwt_in_free_text,
    test_find_leaks_flags_what_scrub_might_miss,
    test_summary_is_written_when_no_audio_arrived,
    test_end_call_is_not_sent_into_a_closed_socket,
    test_call_survives_the_socket_closing_early_and_keeps_the_evidence,
    test_an_error_mid_run_still_writes_frames_summary_and_get_call,
    test_full_call_still_ends_on_end_call,
    test_the_bare_model_url_and_any_appended_path_get_an_answer_and_are_recorded,
    test_a_create_call_response_without_a_socket_url_is_saved_and_exits_cleanly,
    test_fixtures_publish_the_path_evidence_and_nothing_secret,
    test_fixtures_refuse_to_write_what_scrub_missed,
    test_the_sweep_catches_stale_dirty_files,
]


def cmd_selftest(args) -> None:
    import contextlib
    import io

    failed = 0
    for test in SELFTESTS:
        started = time.monotonic()
        noise = io.StringIO()  # the code under test prints a lot; show it only when a test fails
        try:
            with contextlib.redirect_stdout(noise):
                test()
        except BaseException as exc:  # SystemExit from the code under test counts as a failure too
            failed += 1
            print(noise.getvalue()[-1500:], end="")
            print(f"FAIL {test.__name__}: {type(exc).__name__}: {exc}", flush=True)
        else:
            print(f"ok   {test.__name__} ({time.monotonic() - started:.1f} s)", flush=True)
    print(f"{len(SELFTESTS) - failed} passed, {failed} failed")
    if failed:
        raise SystemExit(1)


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

    p = sub.add_parser("selftest", help="offline checks (scrubber, early close, fixtures); spends no Vapi credit")
    p.set_defaults(fn=cmd_selftest)

    args = parser.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
