"""Server-side latency tracing: when each hop of an interviewer reply happened.

One `CallTracer` runs per live call. Every stamp is in milliseconds since `start()`, which the
route calls when the call goes live, on the clock it is given (the server's monotonic clock).
A `TurnTrace` holds one reply's stamps, in the order they happen:

- `last_candidate_audio_ms`: the relay passes the candidate's last frame of speech before the
  request, by the same energy test the browser uses to hear speech;
- `user_stopped_ms`: Vapi says the candidate stopped speaking. Vapi says so only once it has
  decided the answer is over, often just after it asked for the reply;
- `request_ms`: Vapi's brain request for the reply arrives;
- `first_token_ms`: the brain yields Claude's first text of the reply. A reply that starts with
  a line the brain says itself (the wrap-up, or a fallback when Claude wrote nothing) has none;
- `first_chunk_ms`: the first text goes out to Vapi;
- `assistant_started_ms`: Vapi says it started speaking the reply;
- `first_audio_ms`: the first audible interviewer frame passes through the relay.

The browser's own two numbers, `browser_stop_to_audible_ms` and `interrupt_to_silence_ms`, are
durations it measured itself and sent on the call's socket (`browser_timing`). They land on the
reply Vapi is speaking when they arrive. The browser is not trusted: only a number of ms from 0
to MAX_BROWSER_MS is kept, the first one for each field of a reply, and anything else is ignored
(Review Focus 2). `cut_in` marks a reply the candidate interrupted within CUT_IN_WINDOW_MS of its
start.

Vapi sends several requests per answer and speaks only the reply to the last one, so most traces
are thrown away with their drafts. A trace counts once the brain writes its reply down as an
interviewer turn and gives it that turn's idx. The opening line has no request, so Vapi starting
to speak it gets an untimed trace that no turn ever claims: its audio and interruptions land
there instead of on a reply they do not belong to, and it is never measured (Review Focus 3).

The time from the candidate's last speech to the request is Vapi's wait to decide the answer
is over plus its speech to text, so it is the only server hop that holds them. Like the stop,
the candidate's latest speech is kept until Vapi next starts speaking a line, and then forgotten,
so a reply with no candidate speech since the line before it has no such stamp.

The candidate's latest stop is pending until Vapi next starts speaking a line. Every draft of
the answer gets a copy of it. Vapi can say the candidate stopped only after the request for the
reply came in, so the reply it speaks takes the pending stop then if it has none. Then the stop
is cleared, so it can never be put down to the reply to a later answer.

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
# A candidate frame this loud, as RMS on the int16 scale, is speech: the browser's own speech
# threshold, 0.02 on the -1 to 1 scale (web/src/audio/vad.ts), times 32768.
SPEECH_RMS = 655
# Only this much of a candidate frame is measured: one 20 ms frame, all the page ever sends. The
# page is not trusted, and a bigger frame must not cost the event loop more.
_SPEECH_RMS_BYTES = 640
# An interruption this soon after Vapi started speaking a reply marks it as a cut-in: the
# interviewer most likely started before the candidate had finished.
CUT_IN_WINDOW_MS = 1500
# The longest browser timing kept. Anything longer is not a measurement of one turn.
MAX_BROWSER_MS = 60000
# The browser's name for each of its numbers, and the trace field it goes into.
_BROWSER_FIELDS = {
    "stop_to_audible_ms": "browser_stop_to_audible_ms",
    "interrupt_to_silence_ms": "interrupt_to_silence_ms",
}


@dataclass
class TurnTrace:
    """One reply's stamps, in ms since the call went live. Each is None until it is stamped."""

    turn_idx: int | None = None  # the interviewer turn this reply became, once it is written down
    last_candidate_audio_ms: float | None = None
    user_stopped_ms: float | None = None
    request_ms: float | None = None
    first_token_ms: float | None = None
    first_chunk_ms: float | None = None
    assistant_started_ms: float | None = None
    first_audio_ms: float | None = None
    browser_stop_to_audible_ms: float | None = None
    interrupt_to_silence_ms: float | None = None
    cut_in: bool = False


def _browser_ms(value: object) -> float | None:
    """`value` as ms if it is a number from 0 to MAX_BROWSER_MS, and None otherwise. A bool is
    not a number here. NaN fails every comparison, and an int too big for a float is compared as
    an int, so neither needs a case of its own."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if not 0 <= value <= MAX_BROWSER_MS:
        return None
    return float(value)


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
        self._user_stopped_ms: float | None = None  # the latest stop since Vapi last spoke a line
        self._candidate_spoke_ms: float | None = None  # the latest speech since Vapi last spoke
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
        """The candidate stopped speaking. This stop is pending until Vapi next starts speaking a
        line, and a later stop before then replaces it: the latest one wins."""
        now = self._now_ms()
        if now is not None:
            self._user_stopped_ms = now

    def new_trace(self) -> TurnTrace:
        """A trace for a brain request that just arrived, with a copy of the pending candidate
        stop. Vapi sends several requests per answer, so every draft of one answer gets the same
        stop, and the stop stays pending: it is cleared only when Vapi starts speaking a line. The
        candidate's latest speech is copied the same way."""
        return TurnTrace(
            last_candidate_audio_ms=self._candidate_spoke_ms,
            user_stopped_ms=self._user_stopped_ms,
            request_ms=self._now_ms(),
        )

    def first_token(self, trace: TurnTrace) -> None:
        if trace.first_token_ms is None:
            trace.first_token_ms = self._now_ms()

    def first_chunk(self, trace: TurnTrace) -> None:
        if trace.first_chunk_ms is None:
            trace.first_chunk_ms = self._now_ms()

    def assistant_started(self, trace: TurnTrace | None) -> None:
        """Vapi started speaking `trace`'s reply, or, with None, a line no draft was committed
        for, such as the opening line. That one gets an untimed trace that is never measured, so
        the audio and interruptions that follow are not put down to the reply before it.

        Vapi can send a reply's request before it says the candidate stopped, so a reply drafted
        with no stop takes the pending one here. Either way the pending stop is then cleared: it
        belongs to the answer this line replies to, and must never be put down to the reply to a
        later answer, which would measure that reply from seconds too early (Review Focus 3)."""
        now = self._now_ms()
        if now is None:
            return
        pending, self._user_stopped_ms = self._user_stopped_ms, None
        self._candidate_spoke_ms = None
        if trace is None:
            trace = TurnTrace()
        else:
            if trace.user_stopped_ms is None:
                trace.user_stopped_ms = pending
            self._spoken.append(trace)
        if trace.assistant_started_ms is None:
            trace.assistant_started_ms = now
        self._current = trace

    def on_audio(self, direction: str, frame: bytes) -> None:
        """The relay's audio tap. A candidate frame of speech is the candidate's latest speech.
        The first audible interviewer frame after the current reply started is its first audio.
        Once that is found no interviewer frame is measured until the next reply, so the
        steady-state cost of one is a few comparisons."""
        if direction == "candidate":
            if _rms(frame[:_SPEECH_RMS_BYTES]) >= SPEECH_RMS:
                self._candidate_spoke_ms = self._now_ms()
            return
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

    def browser_timing(self, msg: dict) -> None:
        """The browser's timing message: its numbers go onto the reply being spoken. A number
        that is missing or not 0 to MAX_BROWSER_MS ms is ignored, and so is one the reply already
        has: the first report wins. With no reply spoken yet there is nothing to put it on."""
        turn = self._current
        if turn is None:
            return
        for key, field in _BROWSER_FIELDS.items():
            ms = _browser_ms(msg.get(key))
            if ms is not None and getattr(turn, field) is None:
                setattr(turn, field, ms)

    def committed(self) -> list[TurnTrace]:
        """The traces of the replies that became interviewer turns, in the order spoken."""
        return [trace for trace in self._spoken if trace.turn_idx is not None]
