"""A scripted smoke call: one real interview through our server, Vapi and Claude.

Run it while `scripts/dev.sh --no-web` is up:

    uv run python scripts/smoke_call.py

It acts like the browser. It starts a full interview with the owner key, opens the interview
socket, and streams 16 kHz microphone frames from the moment the socket opens (silence when
nothing is being said, because the server ends a call that hears nothing for 2 s). It waits for
the interviewer's opening line, answers it, talks over the reply, ends the call, and waits for
the graded report. It prints each step as it passes or fails, and exits 0 only if the pieces
worked: the opening line was heard, a reply to answer 1 was heard, a flush arrived after the
interruption, the server said the call ended, and the report came back that was not
"grading_failed". Exit 1 means a step failed, 2 means it could not get started.

It never prints the owner key, the socket token (the whole socket path), a Vapi call id or any
.env value. Everything it prints goes through one `Printer` that blanks those out, and the
words the server sends back are printed only when they are ones we know (`safe_word`).
"""

import argparse
import array
import asyncio
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
import wave
from collections import deque
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, WebSocketException

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"

# Audio on both sockets: raw pcm_s16le, mono, 16 kHz, 20 ms frames (320 samples, 640 bytes).
SAMPLE_RATE = 16_000
FRAME_BYTES = 640
FRAME_S = 0.02
# A frame whose RMS is above this is speech and not line noise, and the interviewer counts as
# quiet after 0.8 s with none. Vapi streams in bursts with gaps of up to 140 ms inside a line
# (docs/vapi-contract.md), so a gap this long is never a pause between two bursts.
VOICE_RMS = 300
QUIET_GAP_S = 0.8
# A loop that falls this far behind its schedule skips ahead instead of sending a burst.
MAX_LAG_S = 0.5

OPENING_WAIT_S = 30.0
REPLY_WAIT_S = 30.0
FLUSH_WAIT_S = 3.0
ENDED_WAIT_S = 10.0
SEND_SLACK_S = 10.0  # time allowed to send an answer, on top of its length
REPORT_POLL_S = 2.0
REPORT_WAIT_S = 120.0
HTTP_TIMEOUT_S = 15.0

HIDDEN = "[hidden]"
UNRECOGNISED = "unrecognised"
# The only words from the server that get printed (see safe_word). The ended reasons are the
# ones relay.py gives, and the report statuses are the ones report.py saves.
ENDED_REASONS = frozenset({"cap", "browser_closed", "vapi_closed", "plan_done", "error"})
REPORT_STATUSES = frozenset({"graded", "no_answers", "grading_failed"})

# What `say` speaks when no WAV is given. The interviewer's question is picked at random, so
# these are generic: a project led, then something added over the interviewer's reply.
SPOKEN_ANSWERS = (
    "At my last internship I led a migration of our billing service from one big monolith to "
    "a few smaller services. I wrote the plan, split the work between three engineers, and we "
    "shipped it two weeks early with no downtime.",
    "Sorry to cut in, but I should add that the hardest part was convincing the team to pause "
    "new features while we did the migration.",
)


# --------------------------------------------------------------------------------------------
# Audio: loading, splitting, listening
# --------------------------------------------------------------------------------------------


class AudioError(ValueError):
    """An answer could not be loaded or made. The message says what to do about it."""


def load_wav(path: Path) -> bytes:
    """The samples of a 16 kHz mono 16-bit WAV file. Raises AudioError for anything else."""
    try:
        with wave.open(str(path), "rb") as wav:
            rate, channels, width = wav.getframerate(), wav.getnchannels(), wav.getsampwidth()
            pcm = wav.readframes(wav.getnframes())
    except (wave.Error, EOFError, OSError):
        raise AudioError(f"{path.name} is not a readable WAV file") from None
    if (rate, channels, width) != (SAMPLE_RATE, 1, 2):
        raise AudioError(
            f"{path.name} must be 16 kHz mono 16-bit, but it is {rate} Hz, "
            f"{channels} channel{'s' if channels != 1 else ''}, {width * 8}-bit"
        )
    if not pcm:
        raise AudioError(f"{path.name} has no audio in it")
    return pcm


def split_frames(pcm: bytes) -> list[bytes]:
    """The audio as 640-byte frames. The last one is padded with zeros (silence) to full size."""
    frames = [pcm[i : i + FRAME_BYTES] for i in range(0, len(pcm), FRAME_BYTES)]
    if frames:
        frames[-1] = frames[-1].ljust(FRAME_BYTES, b"\x00")
    return frames


def speech_seconds(pcm: bytes) -> float:
    return len(pcm) / 2 / SAMPLE_RATE


def rms(frame: bytes) -> float:
    """The root mean square of a frame of s16le samples, 0 for no samples."""
    samples = array.array("h")
    samples.frombytes(frame[: len(frame) // 2 * 2])
    if sys.byteorder == "big":
        samples.byteswap()
    if not samples:
        return 0.0
    return math.sqrt(sum(s * s for s in samples) / len(samples))


class SpeechDetector:
    """Tells when the interviewer is speaking, and when a line has ended, from the audio alone.

    A frame is voice when its RMS is above `threshold`. The speaker is quiet once `gap_s` has
    passed since the last voice frame, and only after some voice was heard.
    """

    def __init__(self, gap_s: float = QUIET_GAP_S, threshold: float = VOICE_RMS) -> None:
        self._gap_s = gap_s
        self._threshold = threshold
        self.first_voice_t: float | None = None
        self.last_voice_t: float | None = None

    @property
    def heard(self) -> bool:
        return self.first_voice_t is not None

    def feed(self, frame: bytes, t: float) -> None:
        if rms(frame) > self._threshold:
            if self.first_voice_t is None:
                self.first_voice_t = t
            self.last_voice_t = t

    def quiet(self, now: float) -> bool:
        return self.last_voice_t is not None and now - self.last_voice_t >= self._gap_s

    def reset(self) -> None:
        """Forget what was heard, so the next line is judged alone."""
        self.first_voice_t = None
        self.last_voice_t = None


# --------------------------------------------------------------------------------------------
# The microphone: 640 bytes every 20 ms, on a clock that cannot drift
# --------------------------------------------------------------------------------------------


def send_time(start: float, index: int) -> float:
    """When frame `index` is due: the start plus index * 20 ms. Not the last time plus 20 ms, so
    a late wake-up never moves the frames after it."""
    return start + index * FRAME_S


def send_times(start: float, count: int) -> list[float]:
    return [send_time(start, i) for i in range(count)]


class MicStreamer:
    """The browser's microphone: one frame every 20 ms from the moment `run()` starts, an
    answer's frames while one is queued and silence otherwise.

    The server's relay ends a call that gets no frame from the browser for 2 s (relay.py), so
    the frames never stop, silence included. `clock` and `sleep` can be replaced in a test.
    """

    def __init__(
        self,
        send: Callable[[bytes], Awaitable[None]],
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._send = send
        self._clock = clock
        self._sleep = sleep
        self._queue: deque[bytes] = deque()
        self._queued = 0
        self.speech_sent = 0

    def play(self, pcm: bytes) -> int:
        """Queue an answer. Returns the `speech_sent` count at which all of it has been sent."""
        frames = split_frames(pcm)
        self._queue.extend(frames)
        self._queued += len(frames)
        return self._queued

    async def run(self) -> None:
        """Send frames until the socket closes."""
        start = self._clock()
        index = 0
        while True:
            late = self._clock() - send_time(start, index)
            if late < 0:
                await self._sleep(-late)
            elif late > MAX_LAG_S:
                start = self._clock() - index * FRAME_S  # skip ahead: no burst of catch-up frames
            speech = bool(self._queue)
            frame = self._queue.popleft() if speech else bytes(FRAME_BYTES)
            try:
                await self._send(frame)
            except ConnectionClosed:
                return
            if speech:
                self.speech_sent += 1
            index += 1


# --------------------------------------------------------------------------------------------
# What the run found, and how it is printed
# --------------------------------------------------------------------------------------------


@dataclass
class Outcome:
    started: bool = False
    socket_opened: bool = False
    opening_heard: bool = False
    reply_heard: bool = False  # a reply to answer 1
    flush_seen: bool = False
    reply2_heard: bool = False  # a reply to answer 2: shown, but not needed to pass
    ended: bool = False
    ended_reason: str | None = None
    latency_s: float | None = None  # end of answer 1 to the first reply audio: rough
    report_http: int | None = None
    report_status: str | None = None
    scored_items: int = 0
    verified_quotes: int = 0
    problems: list[str] = field(default_factory=list)


def report_ok(outcome: Outcome) -> bool:
    return outcome.report_http == 200 and outcome.report_status != "grading_failed"


def passed(outcome: Outcome) -> bool:
    return all(
        (
            outcome.started,
            outcome.socket_opened,
            outcome.opening_heard,
            outcome.reply_heard,
            outcome.flush_seen,
            outcome.ended,
            report_ok(outcome),
        )
    )


def safe_word(value: object, allowed: Iterable[str]) -> str:
    """`value` if it is one of the words we know, else "unrecognised". The server's replies
    are printed through this, so nothing it says can put a secret on the screen."""
    return value if isinstance(value, str) and value in allowed else UNRECOGNISED


def scrub(text: str, secrets: Iterable[str]) -> str:
    """`text` with every secret blanked out, the longest first (a path holds its own token)."""
    for secret in sorted({s for s in secrets if s}, key=len, reverse=True):
        text = text.replace(secret, HIDDEN)
    return text


def format_summary(outcome: Outcome, secrets: Iterable[str] = ()) -> str:
    def row(ok: bool, label: str) -> str:
        return f"  {'ok' if ok else 'FAIL':<4} {label}"

    reply2 = "ok" if outcome.reply2_heard else "no"
    latency = "n/a" if outcome.latency_s is None else f"{outcome.latency_s:.1f} s"
    lines = [
        "Smoke call summary",
        row(outcome.started, "start the interview"),
        row(outcome.socket_opened, "open the socket"),
        row(outcome.opening_heard, "hear the opening line"),
        row(outcome.reply_heard, "hear a reply to answer 1"),
        row(outcome.flush_seen, "get a flush after the interruption"),
        f"  {reply2:<4} hear a reply to answer 2 (not required)",
        row(outcome.ended, "hear that the call ended"),
        row(report_ok(outcome), "get the report"),
        f"Rough latency, end of answer 1 to first reply audio: {latency} (rough)",
        f"Flush: {'yes' if outcome.flush_seen else 'no'}",
        f"Ended reason: {outcome.ended_reason or 'none'}",
        f"Report: HTTP {outcome.report_http or 'none'}, status {outcome.report_status or 'none'}, "
        f"{outcome.scored_items} scored items, {outcome.verified_quotes} verified quotes",
    ]
    if outcome.report_status == "no_answers":
        lines.append("  (no_answers: nothing of the spoken answers reached the transcript)")
    if outcome.problems:
        lines.append("Problems:")
        lines += [f"  - {problem}" for problem in outcome.problems]
    lines.append(f"RESULT: {'PASS' if passed(outcome) else 'FAIL'}")
    return scrub("\n".join(lines), secrets)


class Printer:
    """Everything the script prints goes through here, and so has its secrets blanked out."""

    def __init__(
        self, secrets: Iterable[str] = (), write: Callable[[str], None] | None = None
    ) -> None:
        self.secrets = [s for s in secrets if s]
        self._write = write or (lambda text: print(text, flush=True))
        self._started = time.monotonic()

    def hide(self, secret: str | None) -> None:
        if secret:
            self.secrets.append(secret)

    def line(self, text: str) -> None:
        self._write(scrub(text, self.secrets))

    def note(self, text: str) -> None:
        """A progress line, stamped with the seconds since the script started."""
        self.line(f"[{time.monotonic() - self._started:6.1f} s] {text}")


# --------------------------------------------------------------------------------------------
# Small parsers: the report, .env, addresses
# --------------------------------------------------------------------------------------------


def count_report(report: object) -> tuple[int, int]:
    """(scored items, verified quotes) in a report. A quote in `quotes` has already been checked
    against what the candidate said (report.py). Anything odd in the shape counts as zero."""
    scored = quotes = 0
    questions = report.get("questions") if isinstance(report, dict) else None
    for question in questions if isinstance(questions, list) else []:
        items = question.get("items") if isinstance(question, dict) else None
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            scored += item.get("status") == "scored"
            kept = item.get("quotes")
            quotes += len(kept) if isinstance(kept, list) else 0
    return scored, quotes


def parse_dotenv(text: str) -> dict[str, str]:
    """NAME=value lines of a .env file: comments, `export`, spaces and one pair of quotes."""
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        line = line.removeprefix("export ")
        name, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[name.strip()] = value
    return values


def find_owner_key(environ: Mapping[str, str], env_file: Path) -> str | None:
    """OWNER_KEY from the environment, else from the .env file. Empty counts as not set, as it
    does for the server's own settings."""
    if environ.get("OWNER_KEY"):
        return environ["OWNER_KEY"]
    try:
        return parse_dotenv(env_file.read_text()).get("OWNER_KEY") or None
    except OSError:
        return None


def origin_of(base: str) -> str:
    parts = urlsplit(base)
    return f"{parts.scheme}://{parts.netloc}"


def ws_url(base: str, ws_path: str) -> str:
    """The socket's address: the base with http as ws and https as wss, then the returned path."""
    parts = urlsplit(base)
    scheme = "wss" if parts.scheme == "https" else "ws"
    return f"{scheme}://{parts.netloc}{parts.path.rstrip('/')}{ws_path}"


# --------------------------------------------------------------------------------------------
# The run
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Config:
    base: str
    owner_key: str = field(repr=False)
    answer1: bytes = field(repr=False)
    answer2: bytes = field(repr=False)
    interrupt_at: float = 1.5
    max_seconds: float = 150.0  # the live call, from socket open to hang-up
    quiet_gap_s: float = QUIET_GAP_S
    poll_every_s: float = REPORT_POLL_S
    report_wait_s: float = REPORT_WAIT_S


class Hearing:
    """What the browser hears on the socket: the interviewer's audio and the relay's messages."""

    def __init__(self, gap_s: float) -> None:
        self._t0 = time.monotonic()
        self.voice = SpeechDetector(gap_s)
        self.flush_times: list[float] = []
        self.ended = False
        self.ended_reason: object = None
        self.closed = asyncio.Event()

    def now(self) -> float:
        return time.monotonic() - self._t0

    async def listen(self, ws) -> None:
        try:
            async for message in ws:
                if isinstance(message, bytes):
                    self.voice.feed(message, self.now())
                else:
                    self._on_text(message)
        except ConnectionClosed:
            pass
        finally:
            self.closed.set()

    def _on_text(self, text: str) -> None:
        try:
            message = json.loads(text)
        except ValueError:
            return
        kind = message.get("type") if isinstance(message, dict) else None
        if kind == "flush":
            self.flush_times.append(self.now())
        elif kind == "ended":
            self.ended = True
            self.ended_reason = message.get("reason")
        # a "ping" needs no answer, and nothing else is expected

    def gave_up(self, timeout: float) -> str:
        """Why a wait that returned False ended, in words for a problem line."""
        return "before the socket closed" if self.closed.is_set() else f"within {timeout:g} s"

    async def until(self, condition: Callable[[], bool], timeout: float) -> bool:
        """Wait for `condition`. False if the time runs out or the socket closes first."""
        deadline = self.now() + timeout
        while not condition():
            if self.closed.is_set() or self.now() >= deadline:
                return False
            await asyncio.sleep(FRAME_S)
        return True


async def start_interview(
    http: httpx.AsyncClient, config: Config, outcome: Outcome
) -> tuple[str, str, int | None] | None:
    """POST /api/interviews as the owner. Returns (interview id, ws path, cap) or None."""
    try:
        response = await http.post("/api/interviews", headers={"X-Owner-Key": config.owner_key})
    except httpx.HTTPError as exc:
        outcome.problems.append(
            f"could not reach {config.base} ({type(exc).__name__}): is scripts/dev.sh running?"
        )
        return None
    if response.status_code != 201:
        outcome.problems.append(f"starting the interview answered HTTP {response.status_code}")
        return None
    try:
        body = response.json()
        interview_id, ws_path, cap_s = body["interview_id"], body["ws_path"], body.get("cap_s")
    except (ValueError, KeyError, TypeError, AttributeError):
        outcome.problems.append("starting the interview did not return an id and a socket path")
        return None
    if not isinstance(interview_id, str) or not isinstance(ws_path, str):
        outcome.problems.append("starting the interview did not return an id and a socket path")
        return None
    return interview_id, ws_path, cap_s if isinstance(cap_s, int) else None


async def converse(
    config: Config, hearing: Hearing, mic: MicStreamer, out: Printer, outcome: Outcome
) -> None:
    """The scripted conversation. It stops at the first step that fails: the rest depends on it."""
    voice = hearing.voice

    # 1. The opening line: it starts and then goes quiet. Answering earlier would interrupt it.
    outcome.opening_heard = await hearing.until(lambda: voice.quiet(hearing.now()), OPENING_WAIT_S)
    if not outcome.opening_heard:
        outcome.problems.append(f"no opening line was heard {hearing.gave_up(OPENING_WAIT_S)}")
        return
    out.note("opening line heard")

    # 2. Answer 1, then the time until the interviewer's first reply audio.
    target = mic.play(config.answer1)
    out.note(f"streaming answer 1 ({speech_seconds(config.answer1):.1f} s)")
    sent = await hearing.until(
        lambda: mic.speech_sent >= target, speech_seconds(config.answer1) + SEND_SLACK_S
    )
    if not sent:
        outcome.problems.append("answer 1 could not be sent")
        return
    answer1_end = hearing.now()
    voice.reset()
    outcome.reply_heard = await hearing.until(lambda: voice.heard, REPLY_WAIT_S)
    if not outcome.reply_heard:
        outcome.problems.append(f"no reply to answer 1 was heard {hearing.gave_up(REPLY_WAIT_S)}")
        return
    outcome.latency_s = max(0.0, voice.first_voice_t - answer1_end)
    out.note(f"reply to answer 1 started (rough latency {outcome.latency_s:.1f} s)")

    # 3. Answer 2 over the reply. The relay passes on Vapi's "user-interrupted" as a flush.
    await asyncio.sleep(config.interrupt_at)
    answer2_start = hearing.now()
    target = mic.play(config.answer2)
    out.note("streaming answer 2 over the reply")
    outcome.flush_seen = await hearing.until(
        lambda: any(t >= answer2_start for t in hearing.flush_times), FLUSH_WAIT_S
    )
    out.note("flush arrived" if outcome.flush_seen else "no flush arrived")
    if not outcome.flush_seen:
        outcome.problems.append(f"no flush arrived {hearing.gave_up(FLUSH_WAIT_S)}, after answer 2")
    sent = await hearing.until(
        lambda: mic.speech_sent >= target, speech_seconds(config.answer2) + SEND_SLACK_S
    )
    if not sent:
        outcome.problems.append("answer 2 could not be sent")
        return

    # 4. The reply to answer 2 plays out. Then hang_up() ends the call.
    voice.reset()
    outcome.reply2_heard = await hearing.until(lambda: voice.quiet(hearing.now()), REPLY_WAIT_S)
    out.note("reply to answer 2 heard" if outcome.reply2_heard else "no reply to answer 2 heard")


async def hang_up(ws, hearing: Hearing, out: Printer, outcome: Outcome) -> None:
    """End the call and wait for the server to say it ended. Runs however the conversation
    went, so a failed run never leaves a Vapi call running up its bill."""
    if not hearing.ended and not hearing.closed.is_set():
        try:
            await ws.send(json.dumps({"type": "end"}))
        except ConnectionClosed:
            pass
    await hearing.until(lambda: hearing.ended, ENDED_WAIT_S)
    outcome.ended = hearing.ended
    if hearing.ended:
        outcome.ended_reason = safe_word(hearing.ended_reason, ENDED_REASONS)
        out.note(f"call ended: {outcome.ended_reason}")
    else:
        outcome.problems.append("the server never said the call had ended")


async def drive_call(config: Config, url: str, out: Printer, outcome: Outcome) -> None:
    """Open the socket and run the conversation, with the microphone streaming throughout."""
    try:
        async with connect(url) as ws:
            outcome.socket_opened = True
            out.note("socket open, microphone streaming")
            hearing = Hearing(config.quiet_gap_s)
            mic = MicStreamer(ws.send)
            tasks = [asyncio.create_task(mic.run()), asyncio.create_task(hearing.listen(ws))]
            try:
                async with asyncio.timeout(config.max_seconds):
                    await converse(config, hearing, mic, out, outcome)
            except TimeoutError:
                outcome.problems.append(f"the call ran past --max-seconds ({config.max_seconds:g})")
            finally:
                await hang_up(ws, hearing, out, outcome)
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
    except (OSError, WebSocketException) as exc:
        # Only the type: a library's message could hold the address, and its token.
        what = "the socket failed" if outcome.socket_opened else "could not open the socket"
        outcome.problems.append(f"{what} ({type(exc).__name__})")


async def fetch_report(
    http: httpx.AsyncClient, interview_id: str, config: Config, out: Printer, outcome: Outcome
) -> None:
    """Ask for the report until it is there: 202 means it is on its way, 200 is the report."""
    out.note("waiting for the report")
    deadline = time.monotonic() + config.report_wait_s
    while True:
        status = response = None
        try:
            response = await http.get(f"/api/interviews/{interview_id}/report")
            status = response.status_code
        except httpx.HTTPError:
            pass  # a hiccup: ask again until the time is up
        outcome.report_http = status
        if status == 200:
            try:
                report = response.json()
            except ValueError:
                report = None
            word = report.get("status") if isinstance(report, dict) else None
            outcome.report_status = safe_word(word, REPORT_STATUSES)
            outcome.scored_items, outcome.verified_quotes = count_report(report)
            out.note(f"report ready: {outcome.report_status}")
            return
        if status is not None and status != 202:
            outcome.problems.append(f"the report answered HTTP {status}")
            return
        if time.monotonic() >= deadline:
            outcome.problems.append(f"no report after {config.report_wait_s:g} s")
            return
        await asyncio.sleep(config.poll_every_s)


async def run_smoke(config: Config, out: Printer) -> Outcome:
    """The whole smoke call: start, talk, hang up, fetch the report."""
    outcome = Outcome()
    # The server accepts a start from a page on its own site, which this stands in for. The
    # owner key skips that check as well, so it is a stand-in and not a way in.
    headers = {"Origin": origin_of(config.base)}
    async with httpx.AsyncClient(
        base_url=config.base, headers=headers, timeout=HTTP_TIMEOUT_S
    ) as http:
        started = await start_interview(http, config, outcome)
        if started is None:
            return outcome
        interview_id, ws_path, cap_s = started
        outcome.started = True
        url = ws_url(config.base, ws_path)
        # The socket path holds the one-time token: it is never printed (ruling R19).
        for secret in (ws_path, url, *parse_qs(urlsplit(ws_path).query).get("token", [])):
            out.hide(secret)
        out.note(f"interview started (cap {cap_s} s)" if cap_s else "interview started")
        await drive_call(config, url, out, outcome)
        if outcome.socket_opened:
            await fetch_report(http, interview_id, config, out, outcome)
    return outcome


# --------------------------------------------------------------------------------------------
# The command line
# --------------------------------------------------------------------------------------------


def speak(text: str, stem: Path) -> bytes:
    """Speak `text` with macOS `say` and convert it to 16 kHz mono s16 with `afconvert`."""
    if not (shutil.which("say") and shutil.which("afconvert")):
        raise AudioError("without --wav this needs macOS say and afconvert, which were not found")
    aiff, wav = stem.with_suffix(".aiff"), stem.with_suffix(".wav")
    try:
        subprocess.run(["say", "-v", "Samantha", "-o", str(aiff), text], check=True)
        convert = ["afconvert", "-f", "WAVE", "-d", f"LEI16@{SAMPLE_RATE}", "-c", "1"]
        subprocess.run([*convert, str(aiff), str(wav)], check=True)
    except (subprocess.CalledProcessError, OSError):
        raise AudioError("say or afconvert failed: pass --wav with a 16 kHz mono WAV") from None
    return load_wav(wav)


def load_answers(wav: Path | None, directory: Path) -> tuple[bytes, bytes]:
    """The two answers: the given WAV used twice, or two spoken ones made in `directory`."""
    if wav is not None:
        pcm = load_wav(wav)
        return pcm, pcm
    return speak(SPOKEN_ANSWERS[0], directory / "answer1"), speak(
        SPOKEN_ANSWERS[1], directory / "answer2"
    )


def http_url(value: str) -> str:
    if urlsplit(value).scheme not in ("http", "https") or not urlsplit(value).netloc:
        raise argparse.ArgumentTypeError("must be an http:// or https:// address")
    return value


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run one scripted interview through the server, Vapi and Claude, as the "
        "browser would, and check that each piece worked. Start the server first: "
        "scripts/dev.sh --no-web. Needs OWNER_KEY in the environment or .env.",
    )
    parser.add_argument(
        "--base",
        type=http_url,
        default="http://localhost:8000",
        help="the server's address: the API itself, not Vite. The owner key goes to it in a "
        "header, so use localhost or an https address (default %(default)s)",
    )
    parser.add_argument(
        "--wav",
        type=Path,
        help="a 16 kHz mono 16-bit WAV used for both answers. Without it, two answers are "
        "spoken with macOS say and afconvert",
    )
    parser.add_argument(
        "--interrupt-at",
        type=float,
        default=1.5,
        help="seconds after the reply starts that answer 2 cuts in; keep it shorter than the "
        "reply (default %(default)s)",
    )
    parser.add_argument(
        "--max-seconds",
        type=float,
        default=150,
        help="give up on the live call after this long, then hang up. The report wait has its "
        "own limit of 120 s (default %(default)s)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    owner_key = find_owner_key(os.environ, ENV_FILE)
    if owner_key is None:
        print("OWNER_KEY is not set in the environment or in .env.")
        return 2
    out = Printer([owner_key])
    try:
        with tempfile.TemporaryDirectory(prefix="mockingbird-smoke-") as directory:
            answer1, answer2 = load_answers(args.wav, Path(directory))
    except AudioError as exc:
        print(f"Audio: {exc}")
        return 2
    config = Config(
        base=args.base,
        owner_key=owner_key,
        answer1=answer1,
        answer2=answer2,
        interrupt_at=args.interrupt_at,
        max_seconds=args.max_seconds,
    )
    try:
        outcome = asyncio.run(run_smoke(config, out))
    except KeyboardInterrupt:
        print("Interrupted.")
        return 130
    except Exception as exc:
        # Only the type: a traceback could hold the socket address, and its token.
        print(f"The smoke call stopped on an unexpected {type(exc).__name__}.")
        return 1
    out.line(format_summary(outcome, out.secrets))
    return 0 if passed(outcome) else 1


if __name__ == "__main__":
    sys.exit(main())
