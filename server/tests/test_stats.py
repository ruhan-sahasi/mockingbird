"""percentile and summarize_traces: the numbers /api/stats and the stats page show. A turn with no
measured value is left out of every percentile, never counted as 0 ms (Review Focus 3)."""

import pytest

from mockingbird.stats import percentile, summarize_traces
from mockingbird.tracing import TurnTrace

HOPS = {
    "last_candidate_audio_to_request": ("last_candidate_audio_ms", "request_ms"),
    "request_to_first_token": ("request_ms", "first_token_ms"),
    "first_token_to_first_chunk": ("first_token_ms", "first_chunk_ms"),
    "first_chunk_to_assistant_started": ("first_chunk_ms", "assistant_started_ms"),
    "assistant_started_to_first_audio": ("assistant_started_ms", "first_audio_ms"),
}


def spoken(**stamps) -> TurnTrace:
    """A reply Vapi started speaking, with whatever else the test stamps on it."""
    return TurnTrace(**{"assistant_started_ms": 5000.0, **stamps})


# --- percentile ----------------------------------------------------------------------------------


def test_the_percentile_of_nothing_is_none():
    assert percentile([], 50) is None


def test_the_percentile_is_the_nearest_rank_of_the_sorted_values():
    assert percentile([1, 2, 3, 4], 50) == 2
    assert percentile([1, 2, 3, 4], 95) == 4
    assert percentile([4, 1, 3, 2], 50) == 2  # sorted first
    assert percentile([7.5], 95) == 7.5


def test_the_rank_is_worked_out_without_float_drift():
    # 7 / 100 * 100 is 7.000000000000001 in floats, which would round up to rank 8.
    assert percentile([float(v) for v in range(1, 101)], 7) == 7.0
    assert percentile([float(v) for v in range(1, 21)], 95) == 19.0


# --- summarize_traces ----------------------------------------------------------------------------


def test_with_no_traces_every_number_is_empty_and_ducking_is_not_suggested():
    assert summarize_traces([]) == {
        "turns": 0,
        "stop_to_audible_ms": {"p50": None, "p95": None, "n": 0},
        "hops_ms": {hop: None for hop in HOPS},
        "cut_in_rate": None,
        "interrupt_to_silence_ms": {"p50": None, "p95": None, "n": 0},
        "consider_ducking": False,
    }


def test_a_turn_with_no_stop_to_audible_is_left_out_not_counted_as_0_ms():
    traces = [
        spoken(browser_stop_to_audible_ms=900.0),
        spoken(),  # the browser never reported this one
        spoken(browser_stop_to_audible_ms=1100.0),
        spoken(),
    ]
    summary = summarize_traces(traces)
    assert summary["turns"] == 4
    assert summary["stop_to_audible_ms"] == {"p50": 900.0, "p95": 1100.0, "n": 2}


def test_stop_to_audible_comes_from_the_browser_never_from_the_server_stamps():
    # The server saw the candidate stop and the first audio go by, but the browser sent nothing.
    trace = spoken(user_stopped_ms=1000.0, first_audio_ms=2500.0)
    assert summarize_traces([trace])["stop_to_audible_ms"] == {"p50": None, "p95": None, "n": 0}


def test_interrupt_to_silence_skips_turns_that_were_not_interrupted():
    traces = [spoken(interrupt_to_silence_ms=ms) for ms in (120.0, None, 250.0, 180.0, None)]
    assert summarize_traces(traces)["interrupt_to_silence_ms"] == {
        "p50": 180.0,
        "p95": 250.0,
        "n": 3,
    }


def test_each_hop_median_comes_from_the_differences_within_each_turn():
    traces = [
        spoken(
            last_candidate_audio_ms=start - wait,
            request_ms=start,
            first_token_ms=start + token,
            first_chunk_ms=start + token + 5,
            assistant_started_ms=start + token + 5 + speak,
            first_audio_ms=start + token + 5 + speak + audio,
        )
        for wait, start, token, speak, audio in (
            (900, 1000, 400, 500, 90),
            (700, 8000, 600, 300, 120),
            (1200, 20000, 500, 700, 60),
        )
    ]
    assert summarize_traces(traces)["hops_ms"] == {
        "last_candidate_audio_to_request": 900.0,
        "request_to_first_token": 500.0,
        "first_token_to_first_chunk": 5.0,
        "first_chunk_to_assistant_started": 500.0,
        "assistant_started_to_first_audio": 90.0,
    }


@pytest.mark.parametrize("hop", HOPS)
def test_a_hop_missing_either_stamp_is_left_out_never_counted_as_0_ms(hop):
    start, end = HOPS[hop]
    # One turn has only the start of the hop, the other only its end.
    traces = [
        TurnTrace(**{start: 1000.0, end: None}),
        TurnTrace(**{start: None, end: 1500.0}),
    ]
    assert summarize_traces(traces)["hops_ms"][hop] is None


def test_a_hop_median_ignores_the_turns_missing_a_stamp_among_complete_ones():
    traces = [
        spoken(request_ms=100.0, first_token_ms=500.0),  # 400 ms
        spoken(request_ms=None, first_token_ms=300.0),
        spoken(request_ms=None, first_token_ms=900.0),
    ]
    # Counting a missing request as 0 ms would make the median 300.
    assert summarize_traces(traces)["hops_ms"]["request_to_first_token"] == 400.0


def test_the_cut_in_rate_is_over_the_replies_vapi_started_speaking():
    traces = [
        spoken(cut_in=True),
        spoken(),
        spoken(),
        spoken(),
        TurnTrace(assistant_started_ms=None),  # never started, so not in the rate
    ]
    summary = summarize_traces(traces)
    assert summary["turns"] == 5
    assert summary["cut_in_rate"] == 0.25


def test_with_no_reply_vapi_started_speaking_the_cut_in_rate_is_none():
    assert summarize_traces([TurnTrace(), TurnTrace()])["cut_in_rate"] is None


@pytest.mark.parametrize(
    ("values", "p95", "duck"),
    [
        ([301.0], 301.0, True),
        ([300.0], 300.0, False),
        # The p95 of 20 values is the 19th, so one slow interruption in 20 is not enough.
        ([100.0] * 19 + [301.0], 100.0, False),
        ([100.0] * 18 + [301.0, 301.0], 301.0, True),
    ],
)
def test_ducking_is_suggested_only_when_interrupt_to_silence_p95_is_over_300_ms(values, p95, duck):
    # A turn with no interruption is in every call, and must not pull the p95 down.
    traces = [spoken(interrupt_to_silence_ms=ms) for ms in values] + [spoken()]
    summary = summarize_traces(traces)
    assert summary["interrupt_to_silence_ms"]["p95"] == p95
    assert summary["consider_ducking"] is duck
