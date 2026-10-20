import asyncio
import json
from types import SimpleNamespace

import anthropic
import httpx
import httpx2
import pytest

from mockingbird.brain import (
    FALLBACK_LINE,
    MOVE_INSTRUCTION,
    STABLE_SYSTEM,
    WRAP_UP_LINE,
    Brain,
)
from mockingbird.claude import AnthropicStreamer, BrainError, make_anthropic_client
from mockingbird.models import Turn
from mockingbird.plan import DEMO, FULL, InterviewPlan, MoveKind, PlanConfig, Question

CONNECT_NOTE = {"role": "user", "content": "(The call has connected.)"}
LONG_ANSWER = "I led the migration " + "and kept every team informed " * 6  # well over 25 words
HANG = object()  # a scripted step that never finishes, like a slow Claude reply


def qs(n: int) -> list[Question]:
    return [Question(f"q{i}", f"Question number {i}.", "ownership") for i in range(n)]


class FakeClock:
    """A callable clock the test moves by hand. It starts far from zero so that a brain which
    forgot to subtract its start time would get every time check wrong."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeStreamer:
    """Plays one script per call. A script is a list of steps: a text chunk to yield, an
    exception to raise, or HANG to wait forever.

    It stores the `messages` list exactly as it was handed over, not a copy. If the brain passed
    its live history, later appends would show up here and the tests would catch it.
    """

    def __init__(self, *scripts: list) -> None:
        self._scripts = list(scripts)
        self.calls: list[dict] = []
        self.closed = 0  # how many streams were closed before they finished

    def stream(self, *, system, messages):
        self.calls.append({"system": system, "messages": messages})
        script = self._scripts.pop(0) if self._scripts else ["Okay."]
        return self._play(script)

    async def _play(self, script):
        try:
            for step in script:
                if step is HANG:
                    await asyncio.Event().wait()
                elif isinstance(step, Exception):
                    raise step
                else:
                    yield step
        except GeneratorExit:
            self.closed += 1
            raise


def started(plan_config: PlanConfig, streamer: FakeStreamer, *, questions=3):
    clock = FakeClock()
    brain = Brain(InterviewPlan(plan_config, qs(questions)), streamer, clock)
    brain.start()
    return brain, clock


async def say(brain: Brain, text: str) -> list[str]:
    return [chunk async for chunk in brain.reply(text)]


def assert_history_valid(history: list[dict]) -> None:
    """The rules from the global constraints, checked on a settled history."""
    assert history[0]["role"] == "user"
    assert history[-1]["role"] == "assistant"
    for i, message in enumerate(history):
        if message["role"] == "system":
            assert history[i - 1]["role"] == "user"
            assert history[i + 1]["role"] == "assistant"
    # With the system messages set aside, user and assistant take strict turns.
    talk = [m["role"] for m in history if m["role"] != "system"]
    assert talk == ["user", "assistant"] * (len(talk) // 2)
    assert all(m["content"].strip() for m in history)


def roles(history: list[dict]) -> str:
    """The roles in order, as one string, so a test can state the whole shape at a glance."""
    return " ".join(m["role"] for m in history)


async def wait_for(condition) -> None:
    for _ in range(200):
        if condition():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition never became true")


# --- start --------------------------------------------------------------------


def test_start_returns_opening_and_seeds_history_and_turns():
    plan = InterviewPlan(DEMO, qs(1))
    brain = Brain(plan, FakeStreamer(), FakeClock())
    opening = brain.start()
    assert opening == InterviewPlan(DEMO, qs(1)).opening_line()
    assert brain.history == [CONNECT_NOTE, {"role": "assistant", "content": opening}]
    assert brain.turns == [Turn(0, "interviewer", opening, 0)]
    assert not brain.finished


async def test_reply_before_start_and_second_start_are_errors():
    brain = Brain(InterviewPlan(DEMO, qs(1)), FakeStreamer(), FakeClock())
    with pytest.raises(RuntimeError):
        await say(brain, "hello")
    brain.start()
    with pytest.raises(RuntimeError):
        brain.start()


# --- what Claude is sent ------------------------------------------------------


async def test_reply_sends_cached_system_and_trailing_move_instruction():
    streamer = FakeStreamer(["Tell me more about the migration."])
    brain, _ = started(DEMO, streamer)
    await say(brain, "I led the migration.")

    call = streamer.calls[0]
    assert call["system"] == [
        {"type": "text", "text": STABLE_SYSTEM, "cache_control": {"type": "ephemeral"}}
    ]
    messages = call["messages"]
    assert messages[0] == CONNECT_NOTE
    assert messages[1]["role"] == "assistant"
    assert messages[-2] == {"role": "user", "content": "I led the migration."}
    assert messages[-1] == {
        "role": "system",
        "content": MOVE_INSTRUCTION[(MoveKind.FOLLOW_UP, "expand")],
    }
    assert len(messages) == 4  # nothing after the instruction: no assistant text yet


async def test_short_answer_gets_expand_and_long_answer_gets_weakest_part():
    streamer = FakeStreamer(["One."], ["Two."])
    brain, _ = started(DEMO, streamer)
    await say(brain, "I led the migration.")  # 4 words, under the 25 word threshold
    await say(brain, LONG_ANSWER)
    assert streamer.calls[0]["messages"][-1]["content"] == (
        "Ask one short follow-up that asks them to say more about what they just described."
    )
    assert streamer.calls[1]["messages"][-1]["content"] == (
        "Ask one short follow-up about whichever part of their answer was thinnest: "
        "the situation, their task, the actions they took, or the result."
    )


def words(n: int) -> str:
    """n words that are each long, so the text is far over 25 characters even when n is small."""
    return " ".join(["migration"] * n)


@pytest.mark.parametrize(
    ("answer", "probe"),
    [
        ("", "expand"),
        ("a b c d e", "expand"),  # 5 words, 9 characters
        (words(24), "expand"),  # 24 words but 239 characters: counting characters would say 25+
        (words(25), "weakest_part"),  # exactly the threshold: the plan's check is a strict <
        (words(26), "weakest_part"),
        ("\n".join(["migration"] * 24), "expand"),  # newlines separate words like spaces do
        ("  ".join(["migration"] * 24), "expand"),  # runs of spaces are not extra words
    ],
    ids=["empty", "five-short", "24-long", "25-long", "26-long", "24-newlines", "24-double-spaces"],
)
async def test_the_plan_gets_the_word_count_not_the_character_count(answer, probe):
    streamer = FakeStreamer(["Go on."])
    brain, _ = started(DEMO, streamer)
    await say(brain, answer)
    assert (
        streamer.calls[0]["messages"][-1]["content"]
        == (MOVE_INSTRUCTION[(MoveKind.FOLLOW_UP, probe)])
    )


async def test_ask_question_instruction_carries_the_question_text():
    streamer = FakeStreamer(["a"], ["b"], ["Thanks. Now, question number one."])
    brain, _ = started(FULL, streamer)
    for _ in range(3):
        await say(brain, LONG_ANSWER)
    assert streamer.calls[2]["messages"][-1] == {
        "role": "system",
        "content": (
            "In a few words acknowledge their answer, then ask exactly this question: "
            "Question number 1."
        ),
    }


async def test_streamer_gets_a_snapshot_not_the_live_history():
    streamer = FakeStreamer(["Go on."])
    brain, _ = started(DEMO, streamer)
    await say(brain, "I led the migration.")
    # The brain appended an assistant message after the call. The recorded list must not show it.
    assert streamer.calls[0]["messages"][-1]["role"] == "system"
    assert brain.history[-1] == {"role": "assistant", "content": "Go on."}


# --- history ------------------------------------------------------------------


async def test_history_alternates_validly_after_three_turns():
    streamer = FakeStreamer(["Tell me more."], ["What was your part in it?"])
    brain, clock = started(DEMO, streamer)
    clock.advance(30)
    assert await say(brain, "I led the migration.") == ["Tell me more."]
    assert_history_valid(brain.history)
    clock.advance(30)
    assert await say(brain, LONG_ANSWER) == ["What was your part in it?"]
    assert_history_valid(brain.history)
    clock.advance(200)  # 260 s in: inside the 40 s wrap-up reserve
    assert await say(brain, "We shipped it on time.") == [WRAP_UP_LINE]
    assert_history_valid(brain.history)

    # connected note + opening line, two follow-ups, then a wrap-up with no instruction
    # because Claude is not called for it
    assert roles(brain.history) == (
        "user assistant user system assistant user system assistant user assistant"
    )


async def test_history_property_is_a_copy():
    brain, _ = started(DEMO, FakeStreamer())
    brain.history.append({"role": "user", "content": "tamper"})
    brain.history[0]["content"] = "tamper"
    brain.turns.clear()
    assert brain.history[0] == CONNECT_NOTE
    assert len(brain.history) == 2
    assert len(brain.turns) == 1


# --- Claude fails -------------------------------------------------------------


async def test_streamer_error_yields_fallback_line_and_interview_continues():
    streamer = FakeStreamer([BrainError("overloaded")], ["What was the result?"])
    brain, _ = started(DEMO, streamer)
    out = await say(brain, "I led the migration.")
    assert out == ["Could you walk me through that in a bit more detail?"]
    assert_history_valid(brain.history)
    assert brain.history[-1] == {"role": "assistant", "content": out[0]}

    assert await say(brain, LONG_ANSWER) == ["What was the result?"]
    assert_history_valid(brain.history)
    assert [t.speaker for t in brain.turns][-2:] == ["candidate", "interviewer"]


async def test_streamer_error_on_new_question_still_asks_the_question():
    streamer = FakeStreamer(["a"], ["b"], [BrainError("timeout")])
    brain, _ = started(FULL, streamer)
    for _ in range(2):
        await say(brain, LONG_ANSWER)
    assert await say(brain, LONG_ANSWER) == ["Thanks. Question number 1."]
    assert brain.history[-1]["content"] == "Thanks. Question number 1."


async def test_error_after_partial_text_appends_the_fallback_to_the_partial_line():
    streamer = FakeStreamer(["So tell me", BrainError("connection dropped")])
    brain, _ = started(DEMO, streamer)
    out = await say(brain, "I led the migration.")
    assert out == [
        "So tell me",
        " Could you walk me through that in a bit more detail?",
    ]
    assert brain.history[-1]["content"] == "".join(out)
    assert brain.turns[-1].text == "".join(out)
    assert not brain.turns[-1].interrupted


async def test_empty_completion_is_treated_as_a_failure():
    streamer = FakeStreamer([], ["  "])
    brain, _ = started(DEMO, streamer)
    fallback = FALLBACK_LINE[MoveKind.FOLLOW_UP]
    assert await say(brain, "I led the migration.") == [fallback]  # no text at all
    assert (await say(brain, "We shipped it."))[-1] == fallback  # whitespace only
    assert brain.history[-1]["content"].strip() == fallback
    assert_history_valid(brain.history)


BLANKS = ["", "   ", "\n\t "]


@pytest.mark.parametrize("blank", BLANKS, ids=["empty", "spaces", "whitespace-mix"])
async def test_blank_candidate_text_never_puts_empty_content_in_the_history(blank):
    streamer = FakeStreamer(["Sorry, could you say that again?"], ["What was the result?"])
    brain, _ = started(DEMO, streamer)

    assert await say(brain, blank) == ["Sorry, could you say that again?"]
    assert_history_valid(brain.history)
    first = streamer.calls[0]["messages"]
    assert first[-2] == {"role": "user", "content": "(inaudible)"}
    # No words came through, so the plan sees an answer of 0 words.
    assert first[-1]["content"] == MOVE_INSTRUCTION[(MoveKind.FOLLOW_UP, "expand")]

    # The interview carries on, and no later call carries an empty message either.
    assert await say(brain, LONG_ANSWER) == ["What was the result?"]
    assert_history_valid(brain.history)
    for call in streamer.calls:
        assert all(m["content"].strip() for m in call["messages"])

    # The transcript keeps what the candidate side actually sent, so a silent call can still
    # be told apart from an answered one.
    assert brain.turns[1] == Turn(1, "candidate", blank, 0)


async def test_blank_candidate_text_at_the_wrap_up_keeps_the_history_valid():
    brain, clock = started(DEMO, FakeStreamer())
    clock.advance(290)
    assert await say(brain, "  ") == [WRAP_UP_LINE]
    assert_history_valid(brain.history)
    assert brain.history[-2] == {"role": "user", "content": "(inaudible)"}


async def test_an_unexpected_streamer_exception_still_speaks_the_fallback():
    # Whatever the streamer raises, the candidate hears the fixed line and the call goes on.
    streamer = FakeStreamer(
        ["So tell me", RuntimeError("bug in the streamer")], [ValueError("another")], ["Next."]
    )
    brain, _ = started(DEMO, streamer)
    fallback = FALLBACK_LINE[MoveKind.FOLLOW_UP]

    assert await say(brain, "I led the migration.") == ["So tell me", " " + fallback]
    assert_history_valid(brain.history)
    assert await say(brain, LONG_ANSWER) == [fallback]
    assert_history_valid(brain.history)
    assert not any(t.interrupted for t in brain.turns)
    assert brain.history[-1] == {"role": "assistant", "content": fallback}


async def test_an_exception_thrown_in_by_the_consumer_is_not_turned_into_a_fallback():
    streamer = FakeStreamer(["Tell me", " more."])
    brain, _ = started(DEMO, streamer)
    chunks = brain.reply("I led the migration.")
    assert await anext(chunks) == "Tell me"

    with pytest.raises(ValueError, match="consumer failed"):
        await chunks.athrow(ValueError("consumer failed"))

    assert streamer.closed == 1
    assert brain.history[-1] == {"role": "assistant", "content": "Tell me"}
    assert_history_valid(brain.history)
    assert brain.turns[-1].interrupted


# --- wrap-up and done ---------------------------------------------------------


async def test_wrap_up_uses_fixed_line_without_calling_claude():
    streamer = FakeStreamer()
    brain, clock = started(DEMO, streamer)
    clock.advance(DEMO.total_s - DEMO.wrap_up_reserve_s)  # exactly at the reserve
    assert await say(brain, "That is how it went.") == [WRAP_UP_LINE]
    assert streamer.calls == []
    assert brain.finished
    assert WRAP_UP_LINE == (
        "Thanks, that's all the time we have. Your report will be ready in about a minute."
    )


async def test_done_after_wrap_up_returns_empty():
    streamer = FakeStreamer()
    brain, clock = started(DEMO, streamer)
    clock.advance(280)
    await say(brain, "Done.")
    history_before = brain.history
    turns_before = brain.turns
    assert await say(brain, "Wait, one more thing.") == []
    assert streamer.calls == []
    assert brain.history == history_before
    assert brain.turns == turns_before
    assert brain.finished


# --- cancellation and early exit ---------------------------------------------


async def test_cancelled_reply_records_partial_text_and_keeps_history_valid():
    streamer = FakeStreamer(["Tell me", HANG])
    brain, _ = started(DEMO, streamer)
    heard: list[str] = []

    async def consume():
        async for chunk in brain.reply("I led the migration."):
            heard.append(chunk)

    task = asyncio.create_task(consume())
    await wait_for(lambda: heard)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert heard == ["Tell me"]
    assert brain.history[-1] == {"role": "assistant", "content": "Tell me"}
    assert_history_valid(brain.history)
    assert [t.speaker for t in brain.turns] == ["interviewer", "candidate", "interviewer"]
    assert brain.turns[-1] == Turn(2, "interviewer", "Tell me", 0, interrupted=True)


async def test_cancelled_reply_with_no_text_records_cut_off():
    streamer = FakeStreamer([HANG])
    brain, _ = started(DEMO, streamer)
    task = asyncio.create_task(say(brain, "I led the migration."))
    await wait_for(lambda: streamer.calls)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert brain.history[-1] == {"role": "assistant", "content": "(cut off)"}
    assert_history_valid(brain.history)
    assert brain.turns[-1] == Turn(2, "interviewer", "(cut off)", 0, interrupted=True)


async def test_closing_the_reply_early_records_partial_text_and_closes_claude_stream():
    streamer = FakeStreamer(["Tell me", " more.", " Please."])
    brain, _ = started(DEMO, streamer)
    chunks = brain.reply("I led the migration.")
    assert await anext(chunks) == "Tell me"
    await chunks.aclose()  # the consumer stopped listening

    assert streamer.closed == 1  # the Claude stream was shut, not left to the garbage collector
    assert brain.history[-1] == {"role": "assistant", "content": "Tell me"}
    assert_history_valid(brain.history)
    interviewer_turns = [t for t in brain.turns if t.speaker == "interviewer"]
    assert [t.idx for t in interviewer_turns] == [0, 2]
    assert interviewer_turns[-1].interrupted
    assert interviewer_turns[-1].text == "Tell me"


async def test_a_cancelled_reply_still_uses_up_its_move():
    streamer = FakeStreamer([HANG], ["Second follow-up."])
    brain, _ = started(DEMO, streamer)
    task = asyncio.create_task(say(brain, "I led the migration."))
    await wait_for(lambda: streamer.calls)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # DEMO allows two follow-ups. The cancelled one counted, so one is left, then the wrap-up.
    assert await say(brain, LONG_ANSWER) == ["Second follow-up."]
    assert await say(brain, LONG_ANSWER) == [WRAP_UP_LINE]
    assert brain.finished
    assert_history_valid(brain.history)


async def test_a_new_reply_waits_for_the_cancelled_one_to_settle_its_history():
    streamer = FakeStreamer([HANG], ["Second follow-up."])
    brain, _ = started(DEMO, streamer)
    first = asyncio.create_task(say(brain, "I led the migration."))
    await wait_for(lambda: len(streamer.calls) == 1)

    # The second request arrives before the first has even been told to stop.
    second = asyncio.create_task(say(brain, LONG_ANSWER))
    await asyncio.sleep(0)
    first.cancel()
    assert await second == ["Second follow-up."]
    with pytest.raises(asyncio.CancelledError):
        await first

    assert_history_valid(brain.history)
    # opening, then the cancelled reply (closed off), then the reply that waited
    assert roles(brain.history) == "user assistant user system assistant user system assistant"
    assert brain.history[4]["content"] == "(cut off)"


# --- turns --------------------------------------------------------------------


async def test_turns_record_speaker_and_question_idx():
    streamer = FakeStreamer(["fu one"], ["fu two"], ["Thanks. Question number 1."], ["fu three"])
    brain, clock = started(FULL, streamer)
    for _ in range(4):
        clock.advance(60)
        await say(brain, LONG_ANSWER)

    turns = brain.turns
    assert [t.idx for t in turns] == list(range(9))
    assert [t.speaker for t in turns] == ["interviewer", "candidate"] * 4 + ["interviewer"]
    # The candidate answers the question in progress, so the answer that triggers the move to
    # question 1 still belongs to question 0. The interviewer line that asks question 1 is
    # question 1, and so is the answer to it.
    assert [t.question_idx for t in turns] == [0, 0, 0, 0, 0, 0, 1, 1, 1]
    assert turns[6].text == "Thanks. Question number 1."
    assert turns[1].text == LONG_ANSWER
    assert not any(t.interrupted for t in turns)


async def test_wrap_up_turn_belongs_to_the_question_in_progress():
    brain, clock = started(DEMO, FakeStreamer())
    clock.advance(290)
    await say(brain, "Okay.")
    turns = brain.turns
    assert [(t.speaker, t.question_idx) for t in turns] == [
        ("interviewer", 0),
        ("candidate", 0),
        ("interviewer", 0),
    ]
    assert turns[-1].text == WRAP_UP_LINE


# --- the prompt ---------------------------------------------------------------


def test_stable_system_is_long_enough_to_cache_and_states_every_rule():
    # Haiku 5.5 only caches a prefix of 512 tokens or more. English prose runs above one token
    # per word, so 600 words is comfortably past it.
    assert len(STABLE_SYSTEM.split()) >= 600
    text = STABLE_SYSTEM.lower()
    for rule in (
        "one question at a time",
        "at most two follow-up",
        "at most two sentences",
        "no markdown",
        "give no feedback",
    ):
        assert rule in text
    for dash in ("—", "–"):  # em dash, en dash
        assert dash not in STABLE_SYSTEM
    # The brain writes this where a blank transcript would have been, so the prompt must say
    # what to do with it.
    assert "(inaudible)" in STABLE_SYSTEM


def test_fixed_lines_have_no_dashes_and_fallbacks_match_the_spec():
    assert (
        FALLBACK_LINE[MoveKind.FOLLOW_UP] == "Could you walk me through that in a bit more detail?"
    )
    assert FALLBACK_LINE[MoveKind.ASK_QUESTION] == "Thanks. {question.text}"
    for line in (WRAP_UP_LINE, *FALLBACK_LINE.values(), *MOVE_INSTRUCTION.values()):
        assert "—" not in line


# --- the Anthropic streamer ---------------------------------------------------


class FakeAnthropicStream:
    """Stands in for the object `async with client.messages.stream(...)` gives back."""

    def __init__(self, chunks, *, stop_reason="end_turn", raises=None):
        self._chunks = chunks
        self._stop_reason = stop_reason
        self._raises = raises
        self.text_stream = self._text()

    async def _text(self):
        for chunk in self._chunks:
            yield chunk
        if self._raises:
            raise self._raises

    async def get_final_message(self):
        return SimpleNamespace(stop_reason=self._stop_reason)


class FakeAnthropicClient:
    """Records the kwargs of messages.stream(**kwargs) and plays one scripted stream."""

    def __init__(self, stream: FakeAnthropicStream | None = None, open_error=None):
        self.requests: list[dict] = []
        self.entered = 0
        self.exited = 0
        self._stream = stream
        self._open_error = open_error
        self.messages = SimpleNamespace(stream=self._open)

    def _open(self, **kwargs):
        self.requests.append(kwargs)
        return self

    async def __aenter__(self):
        self.entered += 1
        if self._open_error:
            raise self._open_error
        return self._stream

    async def __aexit__(self, *exc_info):
        self.exited += 1
        return False


async def test_anthropic_streamer_request_shape():
    stream = FakeAnthropicStream(["Tell me ", "more."])
    client = FakeAnthropicClient(stream)
    system = [{"type": "text", "text": "rules", "cache_control": {"type": "ephemeral"}}]
    messages = [
        {"role": "user", "content": "(The call has connected.)"},
        {"role": "assistant", "content": "Hi."},
        {"role": "user", "content": "I led the migration."},
        {"role": "system", "content": "Ask one short follow-up."},
    ]

    chunks = [c async for c in AnthropicStreamer(client).stream(system=system, messages=messages)]

    assert chunks == ["Tell me ", "more."]
    assert client.requests == [
        {
            "model": "claude-haiku-5-5",
            "max_tokens": 200,
            "thinking": {"type": "disabled"},
            "output_config": {"effort": "low"},
            "system": system,
            "messages": messages,
        }
    ]
    assert client.entered == client.exited == 1


def _http_error(cls):
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    if cls is anthropic.APIStatusError:
        return cls("overloaded", response=httpx.Response(529, request=request), body=None)
    return cls(request=request)


# The three SDK error classes, raised by a fake stream. The real SDK only raises
# APIStatusError mid-stream (from an `error` event); a stalled or dropped body comes out as the
# HTTP library's own error, which the real-SDK tests below cover.
@pytest.mark.parametrize(
    "error_class",
    [anthropic.APIStatusError, anthropic.APIConnectionError, anthropic.APITimeoutError],
)
@pytest.mark.parametrize("when", ["opening", "mid_stream"])
async def test_anthropic_errors_become_brain_error(error_class, when):
    error = _http_error(error_class)
    if when == "opening":
        client = FakeAnthropicClient(open_error=error)
    else:
        client = FakeAnthropicClient(FakeAnthropicStream(["Tell"], raises=error))

    got: list[str] = []
    with pytest.raises(BrainError) as raised:
        async for chunk in AnthropicStreamer(client).stream(system=[], messages=[]):
            got.append(chunk)

    assert raised.value.__cause__ is error
    assert got == (["Tell"] if when == "mid_stream" else [])
    assert client.entered == 1
    assert client.exited == (1 if when == "mid_stream" else 0)  # never entered, so never exited


def sse(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


def sse_start(text: str) -> list[bytes]:
    """The events of a stream up to and including its first text chunk."""
    message = {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-haiku-5-5",
        "content": [],
        "stop_reason": None,
        "stop_sequence": None,
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
    return [
        sse("message_start", {"type": "message_start", "message": message}),
        sse(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
        ),
        sse(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": text},
            },
        ),
    ]


def sse_finish(stop_reason: str) -> list[bytes]:
    return [
        sse("content_block_stop", {"type": "content_block_stop", "index": 0}),
        sse(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                "usage": {"output_tokens": 2},
            },
        ),
        sse("message_stop", {"type": "message_stop"}),
    ]


class Body(httpx2.AsyncByteStream):
    """A response body that sends some bytes and then, if given an error, fails. That is what a
    stalled or dropped connection looks like after the response headers have arrived."""

    def __init__(self, parts: list[bytes], error: Exception | None = None) -> None:
        self._parts = parts
        self._error = error

    async def __aiter__(self):
        for part in self._parts:
            yield part
        if self._error is not None:
            raise self._error


def real_sdk_streamer(body: Body) -> tuple[AnthropicStreamer, list[httpx2.Request]]:
    """An AnthropicStreamer on the real SDK, whose HTTP layer answers every request with
    `body`. The SDK's own error handling is real: only the network is replaced."""
    requests: list[httpx2.Request] = []

    def answer(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, stream=body)

    client = anthropic.AsyncAnthropic(
        api_key="test-key",
        max_retries=0,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(answer)),
    )
    return AnthropicStreamer(client), requests


async def test_real_sdk_streamer_sends_the_request_and_yields_the_text():
    streamer, requests = real_sdk_streamer(Body(sse_start("Tell me") + sse_finish("end_turn")))
    system = [{"type": "text", "text": "rules", "cache_control": {"type": "ephemeral"}}]
    messages = [{"role": "user", "content": "hi"}]

    assert [c async for c in streamer.stream(system=system, messages=messages)] == ["Tell me"]

    body = json.loads(requests[0].content)
    assert body["model"] == "claude-haiku-5-5"
    assert body["max_tokens"] == 200
    assert body["thinking"] == {"type": "disabled"}
    assert body["output_config"] == {"effort": "low"}
    assert body["stream"] is True
    assert body["system"] == system
    assert body["messages"] == messages


@pytest.mark.parametrize(
    "error",
    [
        httpx2.ReadTimeout("no bytes for 10 s"),
        httpx2.RemoteProtocolError("peer closed the connection mid-body"),
        httpx2.ReadError("connection reset"),
    ],
    ids=lambda error: type(error).__name__,
)
async def test_real_sdk_connection_failure_after_the_headers_becomes_brain_error(error):
    # The SDK wraps errors only while it sends the request. Once the headers are in, a stalled
    # or dropped body reaches the caller as the raw HTTP-library error, so the streamer has to
    # map it. Without that mapping the brain never gets a BrainError and never speaks the
    # fallback line (Review Focus 3).
    streamer, _ = real_sdk_streamer(Body(sse_start("Tell me"), error))

    got: list[str] = []
    with pytest.raises(BrainError) as raised:
        async for chunk in streamer.stream(system=[], messages=[]):
            got.append(chunk)

    assert got == ["Tell me"]
    assert raised.value.__cause__ is error


async def test_brain_speaks_the_fallback_when_the_real_sdk_connection_drops_mid_reply():
    streamer, _ = real_sdk_streamer(
        Body(sse_start("So tell me"), httpx2.ReadTimeout("no bytes for 10 s"))
    )
    brain, _ = started(DEMO, streamer)

    out = await say(brain, "I led the migration.")

    assert out == ["So tell me", " " + FALLBACK_LINE[MoveKind.FOLLOW_UP]]
    assert_history_valid(brain.history)


async def test_anthropic_refusal_becomes_brain_error():
    client = FakeAnthropicClient(FakeAnthropicStream([], stop_reason="refusal"))
    with pytest.raises(BrainError, match="refus"):
        async for _ in AnthropicStreamer(client).stream(system=[], messages=[]):
            pass


async def test_anthropic_streamer_closes_the_request_when_the_consumer_stops():
    client = FakeAnthropicClient(FakeAnthropicStream(["a", "b", "c"]))
    chunks = AnthropicStreamer(client).stream(system=[], messages=[])
    assert await anext(chunks) == "a"
    await chunks.aclose()
    assert client.exited == 1


def test_make_anthropic_client_sets_timeout_and_one_retry():
    client = make_anthropic_client("test-key")
    assert client.timeout == anthropic.Timeout(10.0, connect=2.0)
    assert client.max_retries == 1
    assert client.api_key == "test-key"
