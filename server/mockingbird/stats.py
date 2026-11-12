"""The numbers /api/stats reports: percentiles over the latency traces of many calls.

Each number comes from the turns that measured it, and a turn missing a stamp is left out of
that number. It never counts as 0 ms, which would flatter every percentile (Review Focus 3). So
each spread says how many turns it is over, as `n`.

- `stop_to_audible_ms` is the headline: the browser's own measure, from the candidate's end of
  speech to the first audible interviewer sample. The server's stamps break it down into hops,
  but never stand in for it.
- `hops_ms` is the median of each server hop, worked out within each turn from its two stamps.
- `cut_in_rate` is the share of spoken replies the candidate talked over within 1.5 s of their
  start (tracing.py's CUT_IN_WINDOW_MS).
- `interrupt_to_silence_ms` is the browser's measure of how long the interviewer keeps sounding
  after the candidate talks over it. Ducking the interviewer audio in the browser is not built,
  and `consider_ducking` says when its p95 is long enough that it should be.
"""

import math

from mockingbird.tracing import TurnTrace

# Above this p95 of interrupt to silence, the interviewer audibly talks over the candidate.
DUCKING_P95_MS = 300
# Each server hop, by name, as the two stamps of a turn it runs between.
HOPS = {
    "last_candidate_audio_to_request": ("last_candidate_audio_ms", "request_ms"),
    "request_to_first_token": ("request_ms", "first_token_ms"),
    "first_token_to_first_chunk": ("first_token_ms", "first_chunk_ms"),
    "first_chunk_to_assistant_started": ("first_chunk_ms", "assistant_started_ms"),
    "assistant_started_to_first_audio": ("assistant_started_ms", "first_audio_ms"),
}


def percentile(values: list[float], p: float) -> float | None:
    """The nearest-rank `p`th percentile, with `p` from 0 to 100: the smallest value that at
    least p% of the values are at or below. It is always one of the values. None for no values.
    """
    if not values:
        return None
    ordered = sorted(values)
    # p * n first, then / 100: p / 100 * n drifts (7 / 100 * 100 is 7.000000000000001).
    rank = math.ceil(p * len(ordered) / 100)
    return ordered[max(rank, 1) - 1]


def _spread(values: list[float]) -> dict:
    return {"p50": percentile(values, 50), "p95": percentile(values, 95), "n": len(values)}


def _measured(traces: list[TurnTrace], field: str) -> list[float]:
    """The turns' values of `field`, leaving out every turn that did not measure it."""
    return [value for trace in traces if (value := getattr(trace, field)) is not None]


def _hop(traces: list[TurnTrace], start: str, end: str) -> list[float]:
    """Each turn's time from its `start` stamp to its `end` stamp, for the turns that have both."""
    return [
        b - a
        for trace in traces
        if (a := getattr(trace, start)) is not None and (b := getattr(trace, end)) is not None
    ]


def summarize_traces(traces: list[TurnTrace]) -> dict:
    """What /api/stats answers for these traces: numbers about all of them together, and
    nothing that points at one interview."""
    started = [trace for trace in traces if trace.assistant_started_ms is not None]
    interrupt = _spread(_measured(traces, "interrupt_to_silence_ms"))
    return {
        "turns": len(traces),
        "stop_to_audible_ms": _spread(_measured(traces, "browser_stop_to_audible_ms")),
        "hops_ms": {
            name: percentile(_hop(traces, start, end), 50) for name, (start, end) in HOPS.items()
        },
        "cut_in_rate": sum(trace.cut_in for trace in started) / len(started) if started else None,
        "interrupt_to_silence_ms": interrupt,
        "consider_ducking": interrupt["p95"] is not None and interrupt["p95"] > DUCKING_P95_MS,
    }
