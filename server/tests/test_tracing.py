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


def test_a_request_takes_the_candidates_last_speech_before_it(tracer, clock):
    # The time from it to the request is Vapi deciding the answer is over, plus its speech to
    # text: the part of the wait no other hop holds.
    clock.advance(1.0)
    tracer.on_audio("candidate", LOUD)
    clock.advance(0.02)
    tracer.on_audio("candidate", pcm(-655))  # at the threshold: the browser's 0.02, as int16
    clock.advance(0.02)
    tracer.on_audio("candidate", pcm(654))  # just below it: a quiet room, not speech
    tracer.on_audio("candidate", SILENT)
    tracer.on_audio("interviewer", LOUD)  # the interviewer is never the candidate
    clock.advance(0.66)
    first = tracer.new_trace()
    clock.advance(0.3)
    second = tracer.new_trace()  # Vapi asks again for the same answer

    assert first.last_candidate_audio_ms == second.last_candidate_audio_ms
    assert first.last_candidate_audio_ms == pytest.approx(1020)
    assert second.request_ms - second.last_candidate_audio_ms == pytest.approx(980)


def test_the_candidates_speech_is_forgotten_once_vapi_speaks_a_line(tracer, clock):
    tracer.on_audio("candidate", LOUD)
    reply = tracer.new_trace()
    tracer.assistant_started(reply)
    clock.advance(5.0)
    # No speech since: the next reply has nothing to measure from, rather than the old answer.
    assert tracer.new_trace().last_candidate_audio_ms is None

    tracer.on_audio("candidate", LOUD)
    tracer.assistant_started(None)  # an untimed line clears it too
    assert tracer.new_trace().last_candidate_audio_ms is None


def test_only_one_frames_worth_of_a_candidate_frame_is_measured(tracer, monkeypatch):
    # The page sends 640-byte frames. A bigger one from a broken or hostile page must not make
    # the event loop work out the RMS of all of it.
    measured = []
    real = tracing._rms
    monkeypatch.setattr(tracing, "_rms", lambda frame: measured.append(len(frame)) or real(frame))
    tracer.on_audio("candidate", LOUD * 1000)
    assert measured == [640]
    assert tracer.new_trace().last_candidate_audio_ms == 0.0


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


@pytest.mark.parametrize(("talk_over_ms", "cut_in"), [(0, True), (1500, True), (1500.5, False)])
def test_the_browsers_talk_over_decides_the_cut_in_over_vapis_stand_in(
    tracer, clock, talk_over_ms, cut_in
):
    # Vapi says user-interrupted 0.3 s into the reply as it started it, which stands in for a
    # cut-in. Then the browser, which heard the reply start later than Vapi started it, says how
    # far into what the candidate heard the cut-in came, and that is what counts.
    trace = spoken_reply(tracer)
    clock.advance(0.3)
    tracer.user_interrupted()
    assert trace.cut_in is True
    tracer.browser_timing({"type": "timing", "talk_over_ms": talk_over_ms})
    assert (trace.talk_over_ms, trace.cut_in) == (talk_over_ms, cut_in)


def test_vapis_stand_in_never_overrules_the_browsers_talk_over(tracer, clock):
    trace = spoken_reply(tracer)
    tracer.browser_timing({"type": "timing", "talk_over_ms": 2400})
    clock.advance(0.5)
    tracer.user_interrupted()  # a second interruption, inside Vapi's window
    assert trace.cut_in is False


def test_the_first_talk_over_wins_and_a_bad_one_leaves_the_stand_in(tracer, clock):
    trace = spoken_reply(tracer)
    tracer.user_interrupted()
    tracer.browser_timing({"type": "timing", "talk_over_ms": -5})
    tracer.browser_timing({"type": "timing", "talk_over_ms": "300"})
    assert (trace.talk_over_ms, trace.cut_in) == (None, True)
    tracer.browser_timing({"type": "timing", "talk_over_ms": 2400})
    tracer.browser_timing({"type": "timing", "talk_over_ms": 100})
    assert (trace.talk_over_ms, trace.cut_in) == (2400, False)


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


# --- the browser's own numbers (Review Focus 2) ---


def spoken_reply(tracer: CallTracer) -> TurnTrace:
    """A reply Vapi started speaking, so it is the current trace."""
    trace = tracer.new_trace()
    tracer.assistant_started(trace)
    return trace


def browser_numbers(trace: TurnTrace) -> tuple:
    return trace.browser_stop_to_audible_ms, trace.interrupt_to_silence_ms


def test_browser_timing_lands_on_the_reply_being_spoken(tracer):
    earlier = spoken_reply(tracer)
    trace = spoken_reply(tracer)
    tracer.browser_timing(
        {"type": "timing", "stop_to_audible_ms": 1420, "interrupt_to_silence_ms": 180.5}
    )
    assert browser_numbers(trace) == (1420.0, 180.5)
    assert browser_numbers(earlier) == (None, None)


def test_browser_timing_takes_each_number_on_its_own(tracer):
    trace = spoken_reply(tracer)
    tracer.browser_timing({"type": "timing", "stop_to_audible_ms": 1420})
    assert browser_numbers(trace) == (1420.0, None)
    tracer.browser_timing({"type": "timing", "interrupt_to_silence_ms": 180})
    assert browser_numbers(trace) == (1420.0, 180.0)


@pytest.mark.parametrize("value", [0, 0.0, 60000, 60000.0, 1, 59999.9])
def test_browser_timing_takes_0_to_60000_ms_inclusive(tracer, value):
    trace = spoken_reply(tracer)
    tracer.browser_timing({"stop_to_audible_ms": value, "interrupt_to_silence_ms": value})
    assert browser_numbers(trace) == (value, value)
    assert all(type(n) is float for n in browser_numbers(trace))


@pytest.mark.parametrize(
    "value",
    [
        -5,
        -0.001,
        60000.001,
        1e9,
        10**400,  # an int too big for a float: compared, never converted
        "300",
        None,
        True,
        False,
        [300],
        {"ms": 300},
        float("nan"),
        float("inf"),
        float("-inf"),
    ],
)
def test_browser_timing_ignores_a_value_that_is_not_0_to_60000_ms(tracer, value):
    trace = spoken_reply(tracer)
    tracer.browser_timing({"stop_to_audible_ms": value, "interrupt_to_silence_ms": value})
    assert browser_numbers(trace) == (None, None)


@pytest.mark.parametrize(
    "message",
    [
        {},
        {"type": "timing"},
        {"type": "timing", "browser_stop_to_audible_ms": 300},  # the trace's name, not the wire's
        {"type": "timing", "user_stopped_ms": 300, "cut_in": True, "turn_idx": 0},
        {"type": "timing", "STOP_TO_AUDIBLE_MS": 300},
    ],
)
def test_browser_timing_ignores_missing_and_other_keys(tracer, message):
    trace = spoken_reply(tracer)
    before = TurnTrace(**vars(trace))
    tracer.browser_timing(message)
    assert trace == before


def test_a_bad_number_does_not_stop_the_good_one_beside_it(tracer):
    trace = spoken_reply(tracer)
    tracer.browser_timing({"stop_to_audible_ms": -5, "interrupt_to_silence_ms": 180})
    assert browser_numbers(trace) == (None, 180.0)


def test_the_first_valid_number_for_a_reply_wins(tracer):
    trace = spoken_reply(tracer)
    tracer.browser_timing({"stop_to_audible_ms": "soon"})
    tracer.browser_timing({"stop_to_audible_ms": 1420, "interrupt_to_silence_ms": 180})
    tracer.browser_timing({"stop_to_audible_ms": 900, "interrupt_to_silence_ms": 90})
    assert browser_numbers(trace) == (1420.0, 180.0)


def test_browser_timing_with_no_reply_spoken_yet_is_ignored(clock):
    before_start = CallTracer(clock)
    before_start.browser_timing({"stop_to_audible_ms": 1420})  # before start(): never raises

    tracer = CallTracer(clock)
    tracer.start()
    tracer.browser_timing({"stop_to_audible_ms": 1420})
    trace = spoken_reply(tracer)
    assert browser_numbers(trace) == (None, None)  # it was not kept for the next reply either


def test_browser_timing_for_the_opening_line_is_never_counted(tracer):
    tracer.assistant_started(None)
    tracer.browser_timing({"stop_to_audible_ms": 1420, "interrupt_to_silence_ms": 180})
    assert tracer.committed() == []

    trace = spoken_reply(tracer)
    trace.turn_idx = 2
    assert browser_numbers(trace) == (None, None)
