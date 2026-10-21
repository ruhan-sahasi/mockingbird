"""The smoke-call client (scripts/smoke_call.py): one scripted interview through the server, as
the browser would run it.

Most of it is pure and tested directly: WAV loading, frame splitting, the 20 ms pacing, the RMS
quiet-gap detector, the report counts, and the pass/fail summary, which must never print a
secret. The last test runs the whole flow against the real app on a local port: the server is
uvicorn on an ephemeral port, with the fakes from test_routes.py behind it, and the one Vapi it
meets is a scripted socket that behaves the way docs/vapi-contract.md says Vapi does (it takes
the candidate's frames, asks the brain for a reply when they go quiet, speaks it, and says
"user-interrupted" when the candidate talks over it).
"""

import array
import asyncio
import importlib.util
import json
import sys
import wave
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import uvicorn
import websockets
from test_routes import (
    BRAIN_SECRET,
    CALL_ID,
    DEFAULT_LINE,
    FakeGrader,
    FakeStreamer,
    FakeVapi,
    FakeVapiConnection,
    frame,
    llm_body,
    said,
)
from websockets.exceptions import ConnectionClosed

from mockingbird.app import create_app
from mockingbird.store import MemoryStore

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "smoke_call.py"


def load_script():
    """scripts/ is not a package, so load the client from its path."""
    spec = importlib.util.spec_from_file_location("smoke_call", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["smoke_call"] = module  # before exec: its dataclasses look themselves up here
    spec.loader.exec_module(module)
    return module


smoke = load_script()

SILENCE = bytes(640)


def constant(level: int, frames: int = 1) -> bytes:
    """`frames` 20 ms frames whose every sample is `level`, so their RMS is exactly `level`."""
    return array.array("h", [level] * (320 * frames)).tobytes()


def write_wav(path: Path, data: bytes, *, rate: int = 16000, channels: int = 1, width: int = 2):
    with wave.open(str(path), "wb") as wav:
        wav.setframerate(rate)
        wav.setnchannels(channels)
        wav.setsampwidth(width)
        wav.writeframes(data)
    return path


# --- WAV loading and frame splitting -------------------------------------------------------------


def test_load_wav_returns_the_samples_of_a_16k_mono_s16_file(tmp_path):
    samples = constant(1234, frames=3)
    assert smoke.load_wav(write_wav(tmp_path / "a.wav", samples)) == samples


@pytest.mark.parametrize(
    ("changes", "complaint"),
    [
        ({"rate": 44100}, "44100 Hz"),
        ({"channels": 2}, "2 channels"),
        ({"width": 1}, "8-bit"),
        ({"width": 4}, "32-bit"),
    ],
)
def test_load_wav_names_what_is_wrong_with_the_format(tmp_path, changes, complaint):
    path = write_wav(tmp_path / "bad.wav", bytes(3200), **changes)
    with pytest.raises(smoke.AudioError, match=complaint):
        smoke.load_wav(path)


def test_load_wav_refuses_a_file_that_is_not_a_wav_or_has_no_audio(tmp_path):
    (tmp_path / "text.wav").write_text("not audio")
    with pytest.raises(smoke.AudioError, match="not a readable WAV"):
        smoke.load_wav(tmp_path / "text.wav")
    with pytest.raises(smoke.AudioError, match="no audio"):
        smoke.load_wav(write_wav(tmp_path / "empty.wav", b""))
    with pytest.raises(smoke.AudioError, match="not a readable WAV"):
        smoke.load_wav(tmp_path / "missing.wav")


def test_split_frames_makes_640_byte_frames_and_pads_the_last_with_zeros():
    pcm = bytes([7]) * 1000
    frames = smoke.split_frames(pcm)
    assert [len(f) for f in frames] == [640, 640]
    assert frames[0] == pcm[:640]
    assert frames[1] == pcm[640:] + bytes(280)


def test_split_frames_adds_no_padding_frame_to_a_whole_number_of_frames():
    assert smoke.split_frames(bytes([1]) * 1280) == [bytes([1]) * 640] * 2
    assert smoke.split_frames(b"") == []


# --- pacing --------------------------------------------------------------------------------------


def test_send_times_are_a_fixed_20_ms_apart_from_the_start_with_no_accumulated_error():
    assert smoke.send_times(10.0, 4) == pytest.approx([10.0, 10.02, 10.04, 10.06])
    # Each time is start + index * 20 ms, not the last time plus 20 ms, so it cannot drift.
    assert smoke.send_time(10.0, 50_000) == pytest.approx(10.0 + 1000.0)


class FakeClock:
    """A clock that only moves when something sleeps. `overshoot` is how late each sleep wakes.
    A loop that sleeps 1000 times without finishing fails the test instead of hanging it."""

    def __init__(self, overshoot: float = 0.0) -> None:
        self.now = 100.0
        self.overshoot = overshoot
        self.sleeps = 0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps += 1
        assert self.sleeps <= 1000, "the loop kept sleeping and never sent what the test waits for"
        self.now += seconds + self.overshoot


async def test_the_mic_keeps_to_its_schedule_however_late_each_sleep_wakes():
    clock = FakeClock(overshoot=0.004)
    sent: list[float] = []

    async def send(frame: bytes) -> None:
        sent.append(clock())
        if len(sent) == 300:
            raise ConnectionClosed(None, None)

    await smoke.MicStreamer(send, clock=clock, sleep=clock.sleep).run()

    # Chaining sleeps of 20 ms would be 4 ms later with every frame: 1.2 s late by frame 300.
    lateness = [t - (sent[0] + i * 0.02) for i, t in enumerate(sent)]
    assert 0 <= min(lateness) and max(lateness) <= 0.0041


async def test_after_a_long_stall_the_mic_goes_on_at_its_pace_and_does_not_burst():
    clock = FakeClock()
    sent: list[float] = []

    async def send(frame: bytes) -> None:
        sent.append(clock())
        if len(sent) == 10:
            clock.now += 2.0  # the event loop was blocked for 2 s
        if len(sent) == 20:
            raise ConnectionClosed(None, None)

    await smoke.MicStreamer(send, clock=clock, sleep=clock.sleep).run()

    gaps = [b - a for a, b in zip(sent, sent[1:], strict=False)]
    assert gaps[9] == pytest.approx(2.0)
    assert gaps[10:] == pytest.approx([0.02] * len(gaps[10:]))


async def test_the_mic_sends_a_queued_answer_first_and_silence_otherwise():
    clock = FakeClock()
    sent: list[bytes] = []

    async def send(frame: bytes) -> None:
        sent.append(frame)
        if len(sent) == 5:
            raise ConnectionClosed(None, None)

    mic = smoke.MicStreamer(send, clock=clock, sleep=clock.sleep)
    answer = bytes([5]) * 1000
    assert mic.play(answer) == 2  # the speech frame count at which this answer is all sent
    await mic.run()

    assert sent[:2] == [answer[:640], answer[640:] + bytes(280)]
    assert sent[2:] == [SILENCE] * 3
    assert {len(f) for f in sent} == {640}
    assert mic.speech_sent == 2


async def test_each_answer_target_counts_on_from_the_one_before():
    mic = smoke.MicStreamer(lambda frame: None)
    assert mic.play(bytes(640 * 3)) == 3
    assert mic.play(bytes(640 * 2)) == 5


# --- hearing the interviewer ---------------------------------------------------------------------


def test_rms_of_a_constant_frame_is_its_level_and_of_nothing_is_zero():
    assert smoke.rms(constant(1000)) == pytest.approx(1000)
    assert smoke.rms(constant(-1000)) == pytest.approx(1000)
    assert smoke.rms(SILENCE) == 0
    assert smoke.rms(b"") == 0


def test_a_frame_is_voice_only_above_the_threshold_of_300():
    voice = smoke.SpeechDetector()
    voice.feed(constant(299), 0.0)
    assert not voice.heard
    voice.feed(constant(301), 0.02)
    assert voice.heard
    assert voice.first_voice_t == 0.02


def test_the_speaker_is_quiet_only_once_a_whole_gap_has_passed_without_voice():
    voice = smoke.SpeechDetector()
    assert not voice.quiet(5.0)  # nothing has been said yet, so there is nothing to go quiet
    for i in range(50):
        voice.feed(constant(5000), i * 0.02)
    last = 49 * 0.02
    voice.feed(SILENCE, last + 0.5)
    assert not voice.quiet(last + 0.79)
    assert voice.quiet(last + 0.81)


def test_a_140_ms_hole_in_the_stream_is_not_a_quiet_gap():
    # Vapi sends in bursts, with gaps of up to 140 ms inside one line (docs/vapi-contract.md).
    voice = smoke.SpeechDetector()
    voice.feed(constant(5000), 1.0)
    assert not voice.quiet(1.14)
    voice.feed(constant(5000), 1.14)
    assert not voice.quiet(1.14 + 0.14)


def test_reset_forgets_what_was_heard_so_the_next_line_is_judged_alone():
    voice = smoke.SpeechDetector()
    voice.feed(constant(5000), 1.0)
    voice.reset()
    assert not voice.heard
    assert not voice.quiet(10.0)
    voice.feed(constant(5000), 12.0)
    assert voice.first_voice_t == 12.0


# --- the report, the environment, the address ----------------------------------------------------

REPORT = {
    "status": "graded",
    "questions": [
        {
            "items": [
                {"status": "scored", "score": 3, "quotes": ["a", "b"]},
                {"status": "not_enough_evidence", "score": None, "quotes": []},
            ]
        },
        {"items": [{"status": "scored", "score": 2, "quotes": ["c"]}]},
    ],
}


def test_count_report_counts_scored_items_and_the_quotes_that_survived():
    assert smoke.count_report(REPORT) == (2, 3)


@pytest.mark.parametrize(
    "report",
    [{}, {"status": "no_answers", "questions": []}, {"questions": "x"}, {"questions": [7, {}]}],
)
def test_count_report_reads_a_report_with_nothing_graded_or_a_strange_shape_as_zero(report):
    assert smoke.count_report(report) == (0, 0)


def test_parse_dotenv_reads_plain_exported_quoted_and_empty_lines():
    text = (
        "# a comment\n"
        "OWNER_KEY=abc123\n"
        'export VAPI_API_KEY="quoted value"\n'
        "ANTHROPIC_API_KEY=\n"
        "SINGLE='x y'\n"
        "  SPACED = padded  \n"
        "not a setting\n"
    )
    assert smoke.parse_dotenv(text) == {
        "OWNER_KEY": "abc123",
        "VAPI_API_KEY": "quoted value",
        "ANTHROPIC_API_KEY": "",
        "SINGLE": "x y",
        "SPACED": "padded",
    }


def test_the_owner_key_comes_from_the_environment_first_then_the_env_file(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("OWNER_KEY=from-file\n")
    assert smoke.find_owner_key({"OWNER_KEY": "from-env"}, env_file) == "from-env"
    assert smoke.find_owner_key({"OWNER_KEY": ""}, env_file) == "from-file"
    assert smoke.find_owner_key({}, env_file) == "from-file"
    assert smoke.find_owner_key({}, tmp_path / "missing") is None
    env_file.write_text("OWNER_KEY=\n")
    assert smoke.find_owner_key({}, env_file) is None


def test_the_socket_address_is_the_base_with_ws_for_http_and_wss_for_https():
    path = "/ws/interview/abc?token=t"
    assert smoke.ws_url("http://localhost:8000", path) == "ws://localhost:8000" + path
    assert smoke.ws_url("https://x.example/", path) == "wss://x.example" + path


def test_the_origin_is_the_bases_scheme_and_host_with_no_path_or_slash():
    assert smoke.origin_of("http://localhost:8000/") == "http://localhost:8000"


def test_the_arguments_default_to_the_briefs_values_and_the_base_must_be_http():
    args = smoke.parse_args([])
    assert (args.base, args.wav, args.interrupt_at, args.max_seconds) == (
        "http://localhost:8000",
        None,
        1.5,
        150,
    )
    with pytest.raises(SystemExit):
        smoke.parse_args(["--base", "ftp://localhost"])


# --- the summary ---------------------------------------------------------------------------------


def good(**changes):
    fields = {
        "started": True,
        "socket_opened": True,
        "opening_heard": True,
        "reply_heard": True,
        "flush_seen": True,
        "reply2_heard": True,
        "ended": True,
        "ended_reason": "browser_closed",
        "latency_s": 1.84,
        "report_http": 200,
        "report_status": "graded",
        "scored_items": 6,
        "verified_quotes": 4,
    }
    return smoke.Outcome(**{**fields, **changes})


def step(text: str, label: str) -> str:
    [line] = [line for line in text.splitlines() if label in line]
    return line


def test_a_run_where_everything_worked_passes():
    outcome = good()
    assert smoke.passed(outcome)
    text = smoke.format_summary(outcome)
    assert "RESULT: PASS" in text
    assert "FAIL" not in text.replace("RESULT: PASS", "")
    assert "1.8 s" in text and "rough" in text
    assert "flush: yes" in text.lower()
    assert "ended reason: browser_closed" in text.lower()
    assert "6 scored items" in text and "4 verified quotes" in text


@pytest.mark.parametrize(
    "broken",
    [
        {"started": False},
        {"socket_opened": False},
        {"opening_heard": False},
        {"reply_heard": False, "latency_s": None},
        {"flush_seen": False},
        {"ended": False, "ended_reason": None},
        {"report_http": None, "report_status": None},
        {"report_http": 404, "report_status": None},
        {"report_http": 202, "report_status": None},
        {"report_status": "grading_failed"},
    ],
)
def test_each_step_the_brief_requires_fails_the_run_when_it_fails(broken):
    outcome = good(**broken)
    assert not smoke.passed(outcome)
    text = smoke.format_summary(outcome)
    assert "RESULT: FAIL" in text
    assert "  FAIL " in text


def test_a_report_of_no_answers_or_no_reply_to_answer_2_does_not_fail_the_run():
    assert smoke.passed(good(report_status="no_answers", scored_items=0, verified_quotes=0))
    outcome = good(reply2_heard=False)
    assert smoke.passed(outcome)
    assert "not required" in step(smoke.format_summary(outcome), "answer 2")


def test_the_summary_lines_say_which_step_failed():
    text = smoke.format_summary(good(flush_seen=False))
    assert step(text, "flush").lstrip().startswith("FAIL")
    assert step(text, "opening").lstrip().startswith("ok")


def test_what_the_server_says_is_printed_only_if_it_is_a_word_we_know():
    reasons = smoke.ENDED_REASONS
    assert smoke.safe_word("browser_closed", reasons) == "browser_closed"
    assert smoke.safe_word("secret-token-1234", reasons) == "unrecognised"
    assert smoke.safe_word(None, reasons) == "unrecognised"
    assert smoke.safe_word({"nested": "x"}, reasons) == "unrecognised"


def test_the_summary_never_contains_the_owner_key_the_token_or_the_call_id():
    owner_key, token, call_id = "owner-key-4f81c9", "tok-A9x7Qe2LmZ", "call-id-01a1569"
    ws_path = f"/ws/interview/abc?token={token}"
    # Hostile values, as if the server had echoed them back or a note had picked them up.
    outcome = good(
        ended_reason=smoke.safe_word(token, smoke.ENDED_REASONS),
        report_status=smoke.safe_word(owner_key, smoke.REPORT_STATUSES),
    )
    outcome.problems.append(f"it said {owner_key} then {call_id}, at {ws_path}")
    text = smoke.format_summary(outcome, secrets=[owner_key, token, call_id, ws_path])
    for secret in (owner_key, token, call_id, ws_path):
        assert secret not in text
    assert "unrecognised" in text  # the words were replaced, not dropped silently


def test_the_printer_hides_every_secret_it_is_given_from_every_line():
    lines: list[str] = []
    out = smoke.Printer(["owner-key-4f81c9"], write=lines.append)
    out.hide("tok-A9x7Qe2LmZ")
    out.line("started with owner-key-4f81c9")
    out.line("socket /ws/interview/x?token=tok-A9x7Qe2LmZ opened")
    assert "owner-key-4f81c9" not in "".join(lines) and "tok-A9x7Qe2LmZ" not in "".join(lines)
    assert "started with" in lines[0]


# --- waiting for the report ----------------------------------------------------------------------


class FakeReportHttp:
    """Stands in for the httpx client: answers each ask with the next scripted answer, the last
    one for every ask after that. An answer is a status code, or an exception to raise."""

    def __init__(self, *answers) -> None:
        self.answers = list(answers)
        self.asked = 0

    async def get(self, url: str):
        assert url.endswith("/api/interviews/iv-1/report")
        answer = self.answers[min(self.asked, len(self.answers) - 1)]
        self.asked += 1
        if isinstance(answer, Exception):
            raise answer
        if answer == 200:
            return httpx.Response(200, json={"status": "graded", "questions": []})
        return httpx.Response(answer, json={"detail": "not yet"})


async def wait_for_report(*answers):
    """Run the client's wait for the report on a clock that moves only when it sleeps, so the
    120 s it may wait costs no time. Returns what it recorded, how long it waited (clock time) and
    how many times it asked."""
    clock = FakeClock()
    http = FakeReportHttp(*answers)
    config = smoke.Config(
        base="http://localhost:8000", owner_key="k", answer1=b"", answer2=b""
    )  # the brief's 2 s poll and 120 s limit, as the defaults
    outcome = smoke.Outcome()
    out = smoke.Printer([], write=lambda line: None)
    await smoke.fetch_report(http, "iv-1", config, out, outcome, clock=clock, sleep=clock.sleep)
    return outcome, clock.now - 100.0, http.asked


async def test_the_report_is_asked_for_again_after_a_404_from_an_interview_not_yet_ended():
    outcome, waited, asked = await wait_for_report(404, 404, 200)
    assert (outcome.report_http, outcome.report_status) == (200, "graded")
    assert outcome.problems == []
    assert (asked, waited) == (3, pytest.approx(4.0))  # every 2 s


@pytest.mark.parametrize("status", [500, 502, 503, 504])
async def test_the_report_is_asked_for_again_after_a_server_error(status):
    outcome, waited, asked = await wait_for_report(status, 200)
    assert (outcome.report_http, outcome.report_status) == (200, "graded")
    assert outcome.problems == []
    assert (asked, waited) == (2, pytest.approx(2.0))


@pytest.mark.parametrize("status", [408, 425, 429])
async def test_the_report_is_asked_for_again_when_the_server_says_try_again(status):
    outcome, _, asked = await wait_for_report(status, 200)
    assert outcome.report_status == "graded" and asked == 2


async def test_the_report_is_asked_for_again_after_a_dropped_connection_or_a_timeout():
    hiccups = (httpx.ConnectError("refused"), httpx.ReadTimeout("slow"))
    outcome, _, asked = await wait_for_report(*hiccups, 200)
    assert outcome.report_status == "graded" and asked == 3


async def test_a_mix_of_202_404_and_500_is_waited_out_until_the_report_is_there():
    outcome, waited, asked = await wait_for_report(404, 500, 202, 202, httpx.ReadTimeout("x"), 200)
    assert (outcome.report_http, outcome.report_status, outcome.problems) == (200, "graded", [])
    assert (asked, waited) == (6, pytest.approx(10.0))


async def test_a_report_that_stays_202_is_asked_for_every_2_s_until_120_s_have_passed():
    outcome, waited, asked = await wait_for_report(202)
    assert outcome.problems == ["no report after 120 s (the last answer was HTTP 202)"]
    assert outcome.report_http == 202 and outcome.report_status is None
    # Asks at 0, 2, ... and 120 s: the one at the limit is the last.
    assert (asked, waited) == (61, pytest.approx(120.0))


@pytest.mark.parametrize("status", [404, 500, 503])
async def test_a_report_that_stays_404_or_5xx_is_asked_for_the_whole_120_s_and_says_which(status):
    outcome, waited, asked = await wait_for_report(status)
    assert outcome.problems == [f"no report after 120 s (the last answer was HTTP {status})"]
    assert outcome.report_http == status
    assert (asked, waited) == (61, pytest.approx(120.0))


async def test_a_server_that_never_answers_is_asked_until_120_s_and_the_problem_says_so():
    outcome, waited, asked = await wait_for_report(httpx.ConnectError("refused"))
    assert outcome.problems == ["no report after 120 s (the last ask got no answer)"]
    assert outcome.report_http is None
    assert (asked, waited) == (61, pytest.approx(120.0))


async def test_an_answer_that_lost_its_status_to_a_hiccup_is_still_reported_by_its_last_status():
    outcome, _, _ = await wait_for_report(404, httpx.ReadTimeout("slow"))
    assert outcome.report_http == 404  # the last status the server gave
    assert outcome.problems == ["no report after 120 s (the last ask got no answer)"]


@pytest.mark.parametrize("status", [400, 401, 403, 405, 422])
async def test_an_answer_that_cannot_turn_into_a_report_stops_the_wait_at_once(status):
    outcome, waited, asked = await wait_for_report(status, 200)
    assert outcome.problems == [f"the report answered HTTP {status}"]
    assert outcome.report_http == status and not smoke.report_ok(outcome)
    assert (asked, waited) == (1, 0.0)


def test_only_an_answer_that_can_still_change_is_asked_for_again():
    again = [202, 404, 408, 425, 429, 500, 502, 503, 504, 599]
    stop = [100, 201, 204, 301, 302, 400, 401, 403, 405, 410, 422, 499]
    assert [s for s in again if not smoke.report_may_change(s)] == []
    assert [s for s in stop if smoke.report_may_change(s)] == []


# --- the whole flow, against the real app --------------------------------------------------------

OPENING_S, FIRST_REPLY_S, SECOND_REPLY_S = 0.4, 1.0, 0.5
ANSWERS = ["I wrote the migration plan and led three engineers.", "And I shipped it early."]


class ScriptedVapi(FakeVapiConnection):
    """Vapi's audio socket for one call, as docs/vapi-contract.md records it.

    It sends a frame every 20 ms, voice while the interviewer speaks and silence otherwise. It
    takes the browser's frames through `send`. When the candidate has talked and then been quiet
    for 0.2 s it asks the real brain for a reply, over HTTP, and speaks it. The candidate talking
    while it speaks is an interruption: it stops and sends "user-interrupted".
    """

    VOICE = bytes([101]) * 640

    def __init__(self, brain_url: str, opening_s: float = OPENING_S) -> None:
        super().__init__()
        self._brain_url = brain_url
        self._opening_s = opening_s
        self._http = httpx.AsyncClient()
        self._voice_left = 0
        self._speaking = False
        self._candidate_talking = False
        self._quiet_frames = 0
        self._utterances = 0
        self._spoken = DEFAULT_LINE  # what Vapi says it spoke of the last reply
        self._tasks: set[asyncio.Task] = set()
        self.interruptions = 0

    def start(self) -> None:
        if self._opening_s:
            self._speak(self._opening_s)
        self._spawn(self._tick())

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self._http.aclose()

    def _spawn(self, coroutine) -> None:
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _speak(self, seconds: float) -> None:
        self.incoming.put_nowait(frame(type="speech-update", status="started", role="assistant"))
        self._voice_left = round(seconds / 0.02)
        self._speaking = True

    async def _tick(self) -> None:
        while not self.closed:
            if self._voice_left > 0:
                self._voice_left -= 1
                self.incoming.put_nowait(self.VOICE)
            else:
                if self._speaking:
                    self._speaking = False
                    self.incoming.put_nowait(
                        frame(type="speech-update", status="stopped", role="assistant")
                    )
                self.incoming.put_nowait(SILENCE)
            await asyncio.sleep(0.02)

    async def send(self, message: bytes | str) -> None:
        await super().send(message)
        if isinstance(message, bytes):
            self._hear(message)

    def _hear(self, mic: bytes) -> None:
        if smoke.rms(mic) > 300:
            self._quiet_frames = 0
            if not self._candidate_talking:
                self._candidate_talking = True
                if self._speaking:
                    self._voice_left, self._speaking = 0, False
                    self.interruptions += 1
                    self.incoming.put_nowait(frame(type="user-interrupted"))
        elif self._candidate_talking:
            self._quiet_frames += 1
            if self._quiet_frames >= 10:
                self._candidate_talking = False
                self._spawn(self._reply())

    async def _reply(self) -> None:
        text = ANSWERS[min(self._utterances, len(ANSWERS) - 1)]
        first = self._utterances == 0
        self._utterances += 1
        response = await self._http.post(
            self._brain_url, json=llm_body(text, None if first else self._spoken)
        )
        self._spoken = said(response)
        self._speak(FIRST_REPLY_S if first else SECOND_REPLY_S)


@pytest.fixture
async def live_app(settings, monkeypatch):
    """The real app, served by uvicorn on a local port, with the route tests' fakes behind it.
    The Vapi it connects to is a ScriptedVapi, made when the app opens its socket: a test sets
    `opening_s` before the run to change how the call opens (0 is a call that stays silent)."""
    store, vapi, streamer, grader = MemoryStore(), FakeVapi(), FakeStreamer(), FakeGrader()
    app = create_app(
        settings, store=store, vapi=vapi, streamer_factory=lambda: streamer, grader=grader
    )
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    serving = asyncio.create_task(server.serve())
    for _ in range(500):
        if server.started or serving.done():
            break
        await asyncio.sleep(0.01)
    assert server.started, "the test server did not start"
    port = server.servers[0].sockets[0].getsockname()[1]
    live = SimpleNamespace(
        base=f"http://127.0.0.1:{port}", settings=settings, opening_s=OPENING_S, vapi_socket=None
    )

    # The app reaches Vapi through `websockets.connect`, which this replaces. The client imports
    # its own connect from websockets.asyncio.client, so it is not affected.
    async def fake_connect(url, **kwargs):
        brain_url = f"{live.base}/vapi/llm/{BRAIN_SECRET}/chat/completions"
        live.vapi_socket = ScriptedVapi(brain_url, live.opening_s)
        live.vapi_socket.start()
        return live.vapi_socket

    monkeypatch.setattr(websockets, "connect", fake_connect)
    try:
        yield live
    finally:
        if live.vapi_socket is not None:
            await live.vapi_socket.stop()
        server.should_exit = True
        await serving


def smoke_config(live, **changes):
    answer = constant(8000, frames=25)  # 0.5 s, loud enough to be voice
    fields = {
        "base": live.base,
        "owner_key": live.settings.owner_key,
        "answer1": answer,
        "answer2": answer,
        "interrupt_at": 0.2,
        "max_seconds": 30.0,
        "quiet_gap_s": 0.2,
        "poll_every_s": 0.05,
    }
    return smoke.Config(**{**fields, **changes})


async def test_the_client_runs_a_whole_interview_against_the_real_app(live_app):
    lines: list[str] = []
    out = smoke.Printer([live_app.settings.owner_key], write=lines.append)
    outcome = await asyncio.wait_for(smoke.run_smoke(smoke_config(live_app), out), 60)

    printed = "\n".join([*lines, smoke.format_summary(outcome)])
    assert smoke.passed(outcome), printed
    # A full interview, which is what the owner key gets. A demo's cap would be 300 s.
    assert f"interview started (cap {live_app.settings.full_cap_s} s)" in printed
    assert outcome.latency_s is not None and outcome.latency_s >= 0
    assert outcome.ended_reason == "browser_closed"
    assert outcome.report_status == "graded"
    assert (outcome.scored_items, outcome.verified_quotes) == (1, 1)
    scripted = live_app.vapi_socket
    assert scripted.interruptions == 1
    # The relay passed on what the client sent, in whole 640-byte frames, and nothing else
    # reached Vapi but its own hang-up.
    assert {len(m) for m in scripted.sent if isinstance(m, bytes)} == {640}
    assert [m for m in scripted.sent if isinstance(m, str)] == [json.dumps({"type": "end-call"})]
    # The owner key, the socket path (which holds the token) and the call id never reached the
    # output, which holds every progress line and the summary.
    for secret in (live_app.settings.owner_key, CALL_ID, "token=", "/ws/interview/"):
        assert secret not in printed


async def test_a_call_that_goes_wrong_is_still_hung_up_and_the_run_fails(live_app):
    live_app.opening_s = 0  # Vapi never speaks the opening line
    lines: list[str] = []
    out = smoke.Printer([live_app.settings.owner_key], write=lines.append)
    config = smoke_config(live_app, max_seconds=1.0)  # so the wait for it does not take 30 s
    outcome = await asyncio.wait_for(smoke.run_smoke(config, out), 60)

    assert not smoke.passed(outcome)
    assert outcome.socket_opened and not outcome.opening_heard
    assert any("--max-seconds" in problem for problem in outcome.problems)
    # It still ended the call: the server heard the end request and told Vapi to end the call,
    # and the report for an interview nobody answered came back.
    assert outcome.ended and outcome.ended_reason == "browser_closed"
    scripted = live_app.vapi_socket
    assert [m for m in scripted.sent if isinstance(m, str)] == [json.dumps({"type": "end-call"})]
    assert (outcome.report_http, outcome.report_status) == (200, "no_answers")
