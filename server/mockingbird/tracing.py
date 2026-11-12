"""Server-side latency tracing: when each hop of an interviewer reply happened.

One `CallTracer` runs per live call. Every stamp is in milliseconds since `start()`, which the
route calls when the call goes live, on the clock it is given (the server's monotonic clock).
A `TurnTrace` holds one reply's stamps, in the order they happen:

- `user_stopped_ms`: Vapi says the candidate stopped speaking;
- `request_ms`: Vapi's brain request for the reply arrives;
- `first_token_ms`: the brain yields the reply's first text;
- `first_chunk_ms`: the first text goes out to Vapi;
- `assistant_started_ms`: Vapi says it started speaking the reply;
- `first_audio_ms`: the first audible interviewer frame passes through the relay.

The browser's own two numbers, `browser_stop_to_audible_ms` and `interrupt_to_silence_ms`, are
durations it measured itself. `cut_in` marks a reply the candidate interrupted within
CUT_IN_WINDOW_MS of its start.

Vapi sends several requests per answer and speaks only the reply to the last one, so most traces
are thrown away with their drafts. A trace counts once the brain writes its reply down as an
interviewer turn and gives it that turn's idx. The opening line has no request, so Vapi starting
to speak it gets an untimed trace that no turn ever claims: its audio and interruptions land
there instead of on a reply they do not belong to, and it is never measured (Review Focus 3).

Stamps that come before `start()` are ignored, so nothing is measured from an undefined origin.
"""

import math
import sys
from array import array
from collections.abc import Callable
from dataclasses import dataclass

# An interviewer frame this loud, as RMS on the int16 scale, is audible. Vapi streams silence
# between replies, so loudness is what finds the first frame of speech.
AUDIBLE_RMS = 300
# An interruption this soon after Vapi started speaking a reply marks it as a cut-in: the
# interviewer most likely started before the candidate had finished.
CUT_IN_WINDOW_MS = 1500


@dataclass
class TurnTrace:
    """One reply's stamps, in ms since the call went live. Each is None until it is stamped."""

    turn_idx: int | None = None  # the interviewer turn this reply became, once it is written down
    user_stopped_ms: float | None = None
    request_ms: float | None = None
    first_token_ms: float | None = None
    first_chunk_ms: float | None = None
    assistant_started_ms: float | None = None
    first_audio_ms: float | None = None
    browser_stop_to_audible_ms: float | None = None
    interrupt_to_silence_ms: float | None = None
    cut_in: bool = False


def _rms(frame: bytes) -> float:
    """The RMS of s16le mono PCM. A trailing odd byte is ignored, and an empty frame is 0."""
    samples = array("h")
    samples.frombytes(frame[: len(frame) - len(frame) % 2])
    if not samples:
        return 0.0
    if sys.byteorder == "big":
        samples.byteswap()  # the audio is little-endian; array reads in the machine's order
    return math.sqrt(math.sumprod(samples, samples) / len(samples))


class CallTracer:
    """The traces of one call. `clock` is in seconds, like time.monotonic."""

    def __init__(self, clock: Callable[[], float]) -> None:
        self._clock = clock
        self._t0: float | None = None
        self._user_stopped_ms: float | None = None  # the latest one no request has taken yet
        self._current: TurnTrace | None = None  # the reply Vapi is speaking, or spoke last
        self._spoken: list[TurnTrace] = []  # the timed replies Vapi started speaking, in order

    def start(self) -> None:
        """The call is live: every stamp from now on is measured from here."""
        self._t0 = self._clock()

    def _now_ms(self) -> float | None:
        if self._t0 is None:
            return None
        return (self._clock() - self._t0) * 1000

    def user_stopped(self) -> None:
        now = self._now_ms()
        if now is not None:
            self._user_stopped_ms = now

    def new_trace(self) -> TurnTrace:
        """A trace for a brain request that just arrived. It takes the newest candidate stop no
        earlier request took, so one stop is never counted for two replies."""
        trace = TurnTrace(user_stopped_ms=self._user_stopped_ms, request_ms=self._now_ms())
        self._user_stopped_ms = None
        return trace

    def first_token(self, trace: TurnTrace) -> None:
        if trace.first_token_ms is None:
            trace.first_token_ms = self._now_ms()

    def first_chunk(self, trace: TurnTrace) -> None:
        if trace.first_chunk_ms is None:
            trace.first_chunk_ms = self._now_ms()

    def assistant_started(self, trace: TurnTrace | None) -> None:
        """Vapi started speaking `trace`'s reply, or, with None, a line no draft was committed
        for, such as the opening line. That one gets an untimed trace that is never measured, so
        the audio and interruptions that follow are not put down to the reply before it."""
        now = self._now_ms()
        if now is None:
            return
        if trace is None:
            trace = TurnTrace()
        else:
            self._spoken.append(trace)
        if trace.assistant_started_ms is None:
            trace.assistant_started_ms = now
        self._current = trace

    def on_audio(self, direction: str, frame: bytes) -> None:
        """The relay's audio tap. The first audible interviewer frame after the current reply
        started is its first audio. Once that is found nothing is measured until the next reply,
        so the steady-state cost of a frame is a few comparisons."""
        turn = self._current
        if (
            direction != "interviewer"
            or turn is None
            or turn.assistant_started_ms is None
            or turn.first_audio_ms is not None
        ):
            return
        if _rms(frame) >= AUDIBLE_RMS:
            turn.first_audio_ms = self._now_ms()

    def user_interrupted(self) -> None:
        """The candidate talked over the current reply. Within CUT_IN_WINDOW_MS of its start,
        that is a cut-in."""
        now = self._now_ms()
        turn = self._current
        if now is None or turn is None or turn.assistant_started_ms is None:
            return
        if now - turn.assistant_started_ms <= CUT_IN_WINDOW_MS:
            turn.cut_in = True

    def committed(self) -> list[TurnTrace]:
        """The traces of the replies that became interviewer turns, in the order spoken."""
        return [trace for trace in self._spoken if trace.turn_idx is not None]
