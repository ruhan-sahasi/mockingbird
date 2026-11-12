"""CallTracer on a fake clock: each hop lands in ms since the call went live, and an event that
cannot be tied to a spoken reply is never counted against one."""

from array import array

import pytest

from mockingbird import tracing
from mockingbird.tracing import CallTracer, TurnTrace


class FakeClock:
    """Seconds, moved by hand. It starts far from zero so a tracer that forgot to subtract its
    start time would get every stamp wrong."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def pcm(value: int, samples: int = 320) -> bytes:
    """One 20 ms frame (320 samples) whose every sample is `value`, so its RMS is |value|."""
    return array("h", [value] * samples).tobytes()


LOUD = pcm(5000)
SILENT = pcm(0)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def tracer(clock) -> CallTracer:
    t = CallTracer(clock)
    t.start()
    return t


def test_each_stamp_lands_in_ms_since_start(tracer, clock):
    clock.advance(1.0)
    tracer.user_stopped()
    clock.advance(0.25)
    trace = tracer.new_trace()
    clock.advance(0.5)
    tracer.first_token(trace)
    clock.advance(0.125)
    tracer.first_chunk(trace)
    clock.advance(0.375)
    tracer.assistant_started(trace)
    clock.advance(0.25)
    tracer.on_audio("interviewer", LOUD)

    assert trace == TurnTrace(
        turn_idx=None,
        user_stopped_ms=1000.0,
        request_ms=1250.0,
        first_token_ms=1750.0,
        first_chunk_ms=1875.0,
        assistant_started_ms=2250.0,
        first_audio_ms=2500.0,
    )


def test_a_first_stamp_is_kept_when_it_comes_again(tracer, clock):
    trace = tracer.new_trace()
    clock.advance(0.125)
    tracer.first_token(trace)
    tracer.first_chunk(trace)
    tracer.assistant_started(trace)
    tracer.on_audio("interviewer", LOUD)
    clock.advance(0.125)
    tracer.first_token(trace)
    tracer.first_chunk(trace)
    tracer.on_audio("interviewer", LOUD)

    assert trace.first_token_ms == trace.first_chunk_ms == trace.first_audio_ms == 125.0


def test_first_audio_ignores_frames_before_assistant_started_and_silent_frames(tracer, clock):
    trace = tracer.new_trace()
    clock.advance(0.25)
    tracer.on_audio("interviewer", LOUD)  # before the reply started: the last reply's tail
    clock.advance(0.25)
    tracer.assistant_started(trace)
    clock.advance(0.25)
    tracer.on_audio("interviewer", SILENT)
    tracer.on_audio("interviewer", pcm(299))  # just below the threshold
    tracer.on_audio("interviewer", pcm(-299))
    tracer.on_audio("candidate", LOUD)  # the candidate's mic never counts
    assert trace.first_audio_ms is None

    clock.advance(0.25)
    tracer.on_audio("interviewer", pcm(-300))  # at the threshold, on the int16 scale
    assert trace.first_audio_ms == 1000.0


@pytest.mark.parametrize("frame", [b"", b"\x01", SILENT[:-1], SILENT + b"\x7f"])
def test_a_short_empty_or_odd_frame_never_raises_and_is_not_audible(tracer, frame):
    trace = tracer.new_trace()
    tracer.assistant_started(trace)
    tracer.on_audio("interviewer", frame)
    assert trace.first_audio_ms is None


def test_a_trailing_odd_byte_is_ignored_and_the_rest_still_counts(tracer, clock):
    trace = tracer.new_trace()
    tracer.assistant_started(trace)
    clock.advance(0.25)
    tracer.on_audio("interviewer", LOUD + b"\x00")
    assert trace.first_audio_ms == 250.0


def test_the_rms_is_only_worked_out_until_the_turn_has_its_first_audio(tracer, monkeypatch):
    measured = []
    real = tracing._rms
    monkeypatch.setattr(tracing, "_rms", lambda frame: measured.append(frame) or real(frame))

    tracer.on_audio("interviewer", LOUD)  # nothing is being spoken yet
    trace = tracer.new_trace()
    tracer.assistant_started(trace)
    tracer.on_audio("interviewer", SILENT)
    tracer.on_audio("interviewer", LOUD)
    for _ in range(50):
        tracer.on_audio("interviewer", LOUD)

    assert len(measured) == 2


@pytest.mark.parametrize(("after_s", "cut_in"), [(1.499, True), (1.501, False)])
def test_an_interruption_within_1500_ms_of_the_start_is_a_cut_in(tracer, clock, after_s, cut_in):
    trace = tracer.new_trace()
    clock.advance(0.5)
    tracer.assistant_started(trace)
    clock.advance(after_s)
    tracer.user_interrupted()
    assert trace.cut_in is cut_in


def test_the_opening_lines_untimed_trace_never_appears_in_committed(tracer, clock):
    # Vapi speaks the opening line itself, so no request was traced for it. Its events attach to
    # an untimed trace that no turn ever claims (Review Focus 3).
    tracer.assistant_started(None)
    clock.advance(0.1)
    tracer.on_audio("interviewer", LOUD)
    tracer.user_interrupted()
    assert tracer.committed() == []

    trace = tracer.new_trace()
    tracer.assistant_started(trace)
    trace.turn_idx = 2  # the brain gives it the interviewer turn's idx once it is written down
    assert tracer.committed() == [trace]


def test_events_after_an_untimed_start_never_land_on_the_reply_before_it(tracer, clock):
    trace = tracer.new_trace()
    tracer.assistant_started(trace)
    trace.turn_idx = 2
    clock.advance(0.1)
    tracer.assistant_started(None)  # Vapi spoke something no draft of ours was committed for
    tracer.on_audio("interviewer", LOUD)
    tracer.user_interrupted()

    assert trace.first_audio_ms is None
    assert trace.cut_in is False
    assert tracer.committed() == [trace]


def test_a_reply_that_was_started_but_never_written_down_is_not_committed(tracer):
    spoken = tracer.new_trace()
    tracer.assistant_started(spoken)
    tracer.new_trace()  # a draft that was dropped
    assert tracer.committed() == []


def test_one_stop_is_copied_into_every_draft_of_the_same_answer(tracer, clock):
    # Vapi sends several requests per answer and speaks only the reply to the last one.
    clock.advance(0.25)
    tracer.user_stopped()
    drafts = []
    for _ in range(3):
        clock.advance(0.125)
        drafts.append(tracer.new_trace())
    assert [d.user_stopped_ms for d in drafts] == [250.0, 250.0, 250.0]

    tracer.assistant_started(drafts[-1])
    for draft in drafts:
        draft.turn_idx = 2  # only the brain sets it, and only for the spoken one: a stress test
    assert tracer.committed() == [drafts[-1]]  # still only the reply Vapi spoke


def test_a_stop_that_comes_after_the_request_goes_to_the_reply_vapi_then_speaks(tracer, clock):
    # The order of the recorded call (fixtures/vapi/ws_text_frames.jsonl): the request for a
    # reply came in 6 ms before Vapi said the candidate stopped, and the reply started at 538 ms.
    clock.advance(17.227)
    trace = tracer.new_trace()
    assert trace.user_stopped_ms is None
    clock.advance(0.006)
    tracer.user_stopped()
    clock.advance(0.532)
    tracer.assistant_started(trace)
    assert trace.user_stopped_ms == pytest.approx(17233.0)

    # The candidate's next answer, served by a lone request: the old stop is gone, so this reply
    # has no measurable start and is left out, never measured from seconds ago (Review Focus 3).
    clock.advance(5.0)
    lone = tracer.new_trace()
    tracer.assistant_started(lone)
    assert lone.user_stopped_ms is None


def test_an_untimed_start_clears_a_pending_stop(tracer, clock):
    clock.advance(0.25)
    tracer.user_stopped()
    tracer.assistant_started(None)  # a line no draft was committed for, such as the opening one
    clock.advance(0.25)
    assert tracer.new_trace().user_stopped_ms is None


def test_the_latest_stop_wins(tracer, clock):
    trace = tracer.new_trace()
    clock.advance(0.125)
    tracer.user_stopped()
    clock.advance(0.125)
    tracer.user_stopped()
    tracer.assistant_started(trace)
    assert trace.user_stopped_ms == 250.0

    clock.advance(0.125)
    tracer.user_stopped()
    clock.advance(0.125)
    tracer.user_stopped()
    assert tracer.new_trace().user_stopped_ms == 500.0


def test_a_reply_keeps_the_stop_it_was_drafted_with(tracer, clock):
    clock.advance(0.125)
    tracer.user_stopped()
    trace = tracer.new_trace()
    clock.advance(0.125)
    tracer.user_stopped()
    tracer.assistant_started(trace)
    assert trace.user_stopped_ms == 125.0
    assert tracer.new_trace().user_stopped_ms is None  # and the later stop was cleared with it


def test_stamps_before_start_are_ignored(clock):
    tracer = CallTracer(clock)
    tracer.user_stopped()
    trace = tracer.new_trace()
    tracer.first_token(trace)
    tracer.first_chunk(trace)
    tracer.assistant_started(trace)
    tracer.on_audio("interviewer", LOUD)
    tracer.user_interrupted()
    assert trace == TurnTrace()

    clock.advance(1.0)
    tracer.start()
    after = tracer.new_trace()
    assert after.user_stopped_ms is None  # the stop before start() was never kept
    assert after.request_ms == 0.0
