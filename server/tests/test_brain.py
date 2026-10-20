import asyncio
import copy
import json
import random
import re
from collections import Counter
from dataclasses import dataclass
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
from mockingbird.plan import DEMO, FULL, InterviewPlan, Move, MoveKind, PlanConfig, Question

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
    exception to raise, HANG to wait forever, or an asyncio.Event to wait for the test to set.

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
                elif isinstance(step, asyncio.Event):
                    await step.wait()
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


async def hear(brain: Brain, text: str, spoken: str | None = None) -> list[str]:
    """Draft a reply and take all of it. Vapi has not started speaking it, so it is not real."""
    return [chunk async for chunk in brain.draft(text, spoken)]


async def say(brain: Brain, text: str, spoken: str | None = None) -> list[str]:
    """One whole turn: draft the reply, take all of it, and Vapi starts speaking it."""
    chunks = await hear(brain, text, spoken)
    brain.commit_spoken()
    return chunks


def settled(brain: Brain) -> list[dict]:
    """The history once the last spoken reply is final, as it is when the call ends. The brain
    appends a reply only when the next draft or finalize() settles it."""
    brain.finalize()
    return brain.history


def start_draft(
    brain: Brain, text: str, spoken: str | None = None
) -> tuple[asyncio.Task, list[str]]:
    """Run a draft in a task, like the route does, and collect the chunks it produces."""
    heard: list[str] = []

    async def consume() -> None:
        async for chunk in brain.draft(text, spoken):
            heard.append(chunk)

    return asyncio.create_task(consume()), heard


async def cancel(task: asyncio.Task) -> None:
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


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


async def test_draft_before_start_and_second_start_are_errors():
    brain = Brain(InterviewPlan(DEMO, qs(1)), FakeStreamer(), FakeClock())
    with pytest.raises(RuntimeError):
        await hear(brain, "hello")
    brain.start()
    with pytest.raises(RuntimeError):
        brain.start()


# --- what Claude is sent ------------------------------------------------------


async def test_draft_sends_cached_system_and_trailing_move_instruction():
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
    brain.finalize()
    # The brain appended the reply after the call. The recorded list must not show it.
    assert streamer.calls[0]["messages"][-1]["role"] == "system"
    assert len(streamer.calls[0]["messages"]) == 4
    assert brain.history[-1] == {"role": "assistant", "content": "Go on."}


async def test_a_later_draft_is_built_on_the_spoken_words_not_the_generated_ones():
    streamer = FakeStreamer(["Thanks for that. Could you say more?"], ["What was the result?"])
    brain, _ = started(DEMO, streamer)
    await say(brain, "I led the migration.")
    await hear(brain, "Sorry to cut in.", "Thanks for that.")

    sent = streamer.calls[1]["messages"]
    assert [m["role"] for m in sent] == [
        "user",
        "assistant",
        "user",
        "system",
        "assistant",
        "user",
        "system",
    ]
    assert sent[4] == {"role": "assistant", "content": "Thanks for that."}


# --- history ------------------------------------------------------------------


async def test_history_alternates_validly_after_three_turns():
    streamer = FakeStreamer(["Tell me more."], ["What was your part in it?"])
    brain, clock = started(DEMO, streamer)
    clock.advance(30)
    assert await say(brain, "I led the migration.") == ["Tell me more."]
    assert_history_valid(settled(brain))
    clock.advance(30)
    assert await say(brain, LONG_ANSWER) == ["What was your part in it?"]
    assert_history_valid(settled(brain))
    clock.advance(200)  # 260 s in: inside the 40 s wrap-up reserve
    assert await say(brain, "We shipped it on time.") == [WRAP_UP_LINE]
    assert_history_valid(settled(brain))

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


# --- drafts are not real until Vapi speaks them ---------------------------------


async def test_four_drafts_for_one_answer_then_one_commit_advance_the_plan_once():
    # Vapi sends a request each time its transcript of the answer grows, and speaks only the
    # reply to the last one.
    streamer = FakeStreamer(
        ["Draft one."], ["Draft two."], ["Draft three."], ["Draft four."], ["Second follow-up."]
    )
    brain, _ = started(DEMO, streamer)
    history_before, turns_before = brain.history, brain.turns
    answer = LONG_ANSWER.split()
    growing = [" ".join(answer[:n]) for n in (3, 8, 20, len(answer))]  # only the last is 25+ words
    for text in growing:
        assert len(await hear(brain, text)) == 1

    # Four drafts so far, and none of them is real.
    assert brain.history == history_before
    assert brain.turns == turns_before

    assert brain.commit_spoken() is True
    assert brain.commit_spoken() is False  # the draft is spoken now, so nothing is pending

    history = settled(brain)
    assert roles(history) == "user assistant user system assistant"
    assert history[2:] == [
        {"role": "user", "content": growing[-1]},
        {"role": "system", "content": MOVE_INSTRUCTION[(MoveKind.FOLLOW_UP, "weakest_part")]},
        {"role": "assistant", "content": "Draft four."},
    ]
    assert [t.speaker for t in brain.turns] == ["interviewer", "candidate", "interviewer"]
    assert brain.turns[1].text == growing[-1]

    # Every draft was built on the same plan and the same history. The plan had not moved when
    # the later ones were drafted, so each got the first follow-up.
    assert [len(call["messages"]) for call in streamer.calls[:4]] == [4, 4, 4, 4]
    assert [call["messages"][-1]["content"] for call in streamer.calls[:3]] == [
        MOVE_INSTRUCTION[(MoveKind.FOLLOW_UP, "expand")]
    ] * 3

    # One move was used, not four: the second follow-up is still there, then the wrap-up.
    assert await say(brain, LONG_ANSWER) == ["Second follow-up."]
    assert await say(brain, LONG_ANSWER) == [WRAP_UP_LINE]


async def test_a_draft_that_is_never_committed_changes_nothing():
    streamer = FakeStreamer(["First draft."], ["Second draft."])
    brain, _ = started(DEMO, streamer)
    history_before, turns_before = brain.history, brain.turns

    assert await hear(brain, "I led the migration.") == ["First draft."]
    assert brain.history == history_before
    assert brain.turns == turns_before
    assert not brain.finished

    # The call ends without Vapi ever speaking it. The reply leaves no trace in the history, and
    # only the candidate's answer is recorded, so it can still be graded.
    assert settled(brain) == history_before
    assert brain.turns == [*turns_before, Turn(1, "candidate", "I led the migration.", 0)]

    # The plan did not move either: the same text gets the same request again.
    assert await hear(brain, "I led the migration.") == ["Second draft."]
    assert streamer.calls[1]["messages"] == streamer.calls[0]["messages"]


async def test_commit_spoken_with_nothing_pending_returns_false():
    brain, _ = started(DEMO, FakeStreamer(["Tell me more."]))
    assert brain.commit_spoken() is False  # the opening line was spoken, and it is no draft
    assert len(brain.history) == 2
    assert len(brain.turns) == 1

    await hear(brain, "I led the migration.")
    assert brain.commit_spoken() is True
    assert brain.commit_spoken() is False  # a second "started speaking" for the same draft
    assert len(brain.turns) == 2  # and it did not record the candidate twice


async def test_a_wrap_up_finishes_the_interview_only_once_it_is_spoken():
    streamer = FakeStreamer()
    brain, clock = started(DEMO, streamer)
    clock.advance(290)

    assert await hear(brain, "That is how it went.") == [WRAP_UP_LINE]
    assert streamer.calls == []
    assert not brain.finished  # Vapi has not spoken it yet

    # Vapi sent another request, so the first wrap-up was dropped. The new one is drafted
    # afresh, because the plan did not move.
    assert await hear(brain, "That is how it went, and more.") == [WRAP_UP_LINE]
    assert not brain.finished

    assert brain.commit_spoken() is True
    assert brain.finished


async def test_a_reply_after_the_spoken_wrap_up_is_empty_and_changes_nothing():
    streamer = FakeStreamer()
    brain, clock = started(DEMO, streamer)
    clock.advance(280)
    await say(brain, "Done.")
    history_before = settled(brain)
    turns_before = brain.turns
    assert roles(history_before) == "user assistant user assistant"

    assert await say(brain, "Wait, one more thing.") == []
    assert streamer.calls == []
    assert brain.history == history_before
    assert brain.turns == turns_before
    assert brain.finished
    assert brain.commit_spoken() is False  # an empty reply leaves nothing to commit


# --- what the history keeps ---------------------------------------------------


async def test_an_interrupted_reply_is_recorded_as_the_words_that_were_spoken():
    generated = "Thanks for that. Could you tell me more about the migration?"
    spoken = "Thanks for that."
    streamer = FakeStreamer([generated], ["What was the result?"])
    brain, _ = started(DEMO, streamer)
    await say(brain, "I led the migration.")

    # Vapi's next request carries only the words it got to say.
    assert await hear(brain, "Sorry to cut in. I also ran the rollout.", spoken) == [
        "What was the result?"
    ]

    assert roles(brain.history) == "user assistant user system assistant"
    assert brain.history[4] == {"role": "assistant", "content": spoken}
    assert brain.turns[2] == Turn(2, "interviewer", spoken, 0, interrupted=True)
    assert len(brain.turns) == 3  # the new draft is pending and adds no turn yet
    assert_history_valid(brain.history)


@pytest.mark.parametrize(
    "spoken",
    [
        None,
        "",
        "   ",
        "Understood. Thanks.",  # Vapi re-punctuates what it spoke, so this is the full reply
        "Understood. Thanks. Really.",  # and a rebuilt text can come out longer
        "...",  # nothing but punctuation holds no words, so it says nothing about what was spoken
    ],
    ids=["none", "empty", "blank", "same-length", "longer", "no-words"],
)
async def test_a_spoken_text_that_is_not_shorter_leaves_the_generated_text(spoken):
    generated = "Understood, thanks."
    streamer = FakeStreamer([generated], ["What was the result?"])
    brain, _ = started(DEMO, streamer)
    await say(brain, "I led the migration.")
    await hear(brain, "We shipped it on time.", spoken)

    assert brain.history[4] == {"role": "assistant", "content": generated}
    assert brain.turns[2] == Turn(2, "interviewer", generated, 0, interrupted=False)


@pytest.mark.parametrize(
    ("generated", "spoken"),
    [
        ("Got it — and what did you measure?", "Got it. And what did you measure?"),
        ("Understood, thanks.", "Understood thanks"),
        ('Walk me through the "rollout" plan.', "Walk me through the rollout plan."),
        ("Tell me  more   about that.", "Tell me more about that."),
        ("That is a twenty-five person team, right?", "That is a 25 person team right"),
        ("I am glad you did that.", "I'm glad you did that."),
    ],
    ids=[
        "dash-to-period",
        "dropped-punctuation",
        "quotes-removed",
        "whitespace",
        "number",
        "contraction",
    ],
)
async def test_a_rebuild_with_the_same_words_is_not_an_interruption(generated, spoken):
    # Vapi rebuilds the text from the words it said and changes punctuation even for a reply
    # that played to the end (docs/vapi-contract.md section 3). Each rebuild here has fewer
    # characters than ours but every word, so it must not read as a cut-off.
    assert len(spoken.strip()) < len(generated.strip())
    streamer = FakeStreamer([generated], ["What was the result?"])
    brain, _ = started(DEMO, streamer)
    await say(brain, "I led the migration.")
    await hear(brain, "We shipped it on time.", spoken)

    assert brain.history[4] == {"role": "assistant", "content": generated}
    assert brain.turns[2] == Turn(2, "interviewer", generated, 0, interrupted=False)


@pytest.mark.parametrize(
    ("generated", "spoken"),
    [
        ("Got it — and what did you measure?", "Got it. And what"),
        ("Tell me more about that.", "Tell me"),
        # Fewer words is what counts, even when the rebuild has more characters.
        ("Tell me more about that.", "Extraordinarily interesting migration"),
    ],
    ids=["re-punctuated", "plain", "longer-words"],
)
async def test_a_rebuild_with_fewer_words_is_an_interruption(generated, spoken):
    streamer = FakeStreamer([generated], ["What was the result?"])
    brain, _ = started(DEMO, streamer)
    await say(brain, "I led the migration.")
    await hear(brain, "Sorry to cut in.", spoken)

    assert brain.history[4] == {"role": "assistant", "content": spoken}
    assert brain.turns[2] == Turn(2, "interviewer", spoken, 0, interrupted=True)


async def test_a_spoken_text_with_no_committed_reply_is_ignored():
    # Nothing was committed, so the only reply Vapi could have spoken is the opening line.
    brain, _ = started(DEMO, FakeStreamer(["One."], ["Two."]))
    opening = brain.history[1]["content"]
    await hear(brain, "I led the migration.", "Hi, I'm Mock")
    await hear(brain, "I led the migration, and more.", "Hi")  # the first draft was not spoken
    assert brain.history == [CONNECT_NOTE, {"role": "assistant", "content": opening}]
    assert len(brain.turns) == 1


async def test_every_system_message_is_followed_by_an_assistant_message_after_any_mix():
    streamer = FakeStreamer(
        ["Tell me", HANG],
        ["Second."],
        [BrainError("overloaded")],
        ["Fourth."],
        ["Fifth."],
        ["Sixth."],
    )
    brain, _ = started(FULL, streamer)

    task, heard = start_draft(brain, "I led the migration.")
    await wait_for(lambda: heard)
    brain.commit_spoken()
    await cancel(task)  # committed, then dropped mid-line
    assert_history_valid(brain.history)

    await hear(brain, "Sorry to cut in.", "Tell")  # settles the dropped one
    assert_history_valid(brain.history)
    await hear(brain, "Sorry to cut in. I also ran it.", None)  # a draft that is dropped
    assert_history_valid(brain.history)
    await say(brain, "Sorry to cut in. I also ran it. And more.", None)
    assert_history_valid(brain.history)
    await hear(brain, LONG_ANSWER, None)
    assert_history_valid(brain.history)
    await say(brain, LONG_ANSWER, None)
    assert_history_valid(settled(brain))


# --- Claude fails -------------------------------------------------------------


async def test_streamer_error_yields_fallback_line_and_interview_continues():
    streamer = FakeStreamer([BrainError("overloaded")], ["What was the result?"])
    brain, _ = started(DEMO, streamer)
    out = await say(brain, "I led the migration.")
    assert out == ["Could you walk me through that in a bit more detail?"]
    assert_history_valid(settled(brain))
    assert brain.history[-1] == {"role": "assistant", "content": out[0]}

    assert await say(brain, LONG_ANSWER) == ["What was the result?"]
    assert_history_valid(settled(brain))
    assert [t.speaker for t in brain.turns][-2:] == ["candidate", "interviewer"]


async def test_streamer_error_on_new_question_still_asks_the_question():
    streamer = FakeStreamer(["a"], ["b"], [BrainError("timeout")])
    brain, _ = started(FULL, streamer)
    for _ in range(2):
        await say(brain, LONG_ANSWER)
    assert await say(brain, LONG_ANSWER) == ["Thanks. Question number 1."]
    assert settled(brain)[-1]["content"] == "Thanks. Question number 1."


async def test_error_after_partial_text_appends_the_fallback_to_the_partial_line():
    streamer = FakeStreamer(["So tell me", BrainError("connection dropped")])
    brain, _ = started(DEMO, streamer)
    out = await say(brain, "I led the migration.")
    assert out == [
        "So tell me",
        " Could you walk me through that in a bit more detail?",
    ]
    assert settled(brain)[-1]["content"] == "".join(out)
    assert brain.turns[-1].text == "".join(out)
    assert not brain.turns[-1].interrupted


async def test_empty_completion_is_treated_as_a_failure():
    streamer = FakeStreamer([], ["  "])
    brain, _ = started(DEMO, streamer)
    fallback = FALLBACK_LINE[MoveKind.FOLLOW_UP]
    assert await say(brain, "I led the migration.") == [fallback]  # no text at all
    assert (await say(brain, "We shipped it."))[-1] == fallback  # whitespace only
    assert settled(brain)[-1]["content"].strip() == fallback
    assert_history_valid(brain.history)


BLANKS = ["", "   ", "\n\t "]


@pytest.mark.parametrize("blank", BLANKS, ids=["empty", "spaces", "whitespace-mix"])
async def test_blank_candidate_text_never_puts_empty_content_in_the_history(blank):
    streamer = FakeStreamer(["Sorry, could you say that again?"], ["What was the result?"])
    brain, _ = started(DEMO, streamer)

    assert await say(brain, blank) == ["Sorry, could you say that again?"]
    assert_history_valid(settled(brain))
    first = streamer.calls[0]["messages"]
    assert first[-2] == {"role": "user", "content": "(inaudible)"}
    # No words came through, so the plan sees an answer of 0 words.
    assert first[-1]["content"] == MOVE_INSTRUCTION[(MoveKind.FOLLOW_UP, "expand")]

    # The interview carries on, and no later call carries an empty message either.
    assert await say(brain, LONG_ANSWER) == ["What was the result?"]
    assert_history_valid(settled(brain))
    for call in streamer.calls:
        assert all(m["content"].strip() for m in call["messages"])

    # The transcript keeps what the candidate side actually sent, so a silent call can still
    # be told apart from an answered one.
    assert brain.turns[1] == Turn(1, "candidate", blank, 0)


async def test_blank_candidate_text_at_the_wrap_up_keeps_the_history_valid():
    brain, clock = started(DEMO, FakeStreamer())
    clock.advance(290)
    assert await say(brain, "  ") == [WRAP_UP_LINE]
    assert_history_valid(settled(brain))
    assert brain.history[-2] == {"role": "user", "content": "(inaudible)"}


async def test_an_unexpected_streamer_exception_still_speaks_the_fallback():
    # Whatever the streamer raises, the candidate hears the fixed line and the call goes on.
    streamer = FakeStreamer(
        ["So tell me", RuntimeError("bug in the streamer")], [ValueError("another")], ["Next."]
    )
    brain, _ = started(DEMO, streamer)
    fallback = FALLBACK_LINE[MoveKind.FOLLOW_UP]

    assert await say(brain, "I led the migration.") == ["So tell me", " " + fallback]
    assert_history_valid(settled(brain))
    assert await say(brain, LONG_ANSWER) == [fallback]
    assert_history_valid(settled(brain))
    assert not any(t.interrupted for t in brain.turns)
    assert brain.history[-1] == {"role": "assistant", "content": fallback}


async def test_an_exception_thrown_in_by_the_consumer_is_not_turned_into_a_fallback():
    streamer = FakeStreamer(["Tell me", " more."])
    brain, _ = started(DEMO, streamer)
    chunks = brain.draft("I led the migration.", None)
    assert await anext(chunks) == "Tell me"
    assert brain.commit_spoken()  # Vapi started speaking after the first sentence

    with pytest.raises(ValueError, match="consumer failed"):
        await chunks.athrow(ValueError("consumer failed"))

    assert streamer.closed == 1
    assert settled(brain)[-1] == {"role": "assistant", "content": "Tell me"}
    assert_history_valid(brain.history)
    assert brain.turns[-1].interrupted


# --- wrap-up ------------------------------------------------------------------


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


# --- timing: Vapi speaks before the draft has finished ------------------------


async def test_commit_can_arrive_while_the_draft_is_still_streaming():
    # Vapi starts speaking after the first sentence, while Claude is still writing the rest.
    gate = asyncio.Event()
    streamer = FakeStreamer(["Thanks for that.", gate, " Could you say more?"])
    brain, _ = started(DEMO, streamer)
    task, heard = start_draft(brain, "I led the migration.")
    await wait_for(lambda: heard)

    assert brain.commit_spoken() is True
    assert not task.done()  # the draft is committed and still being written
    assert [t.speaker for t in brain.turns] == ["interviewer", "candidate"]

    gate.set()
    await task
    assert heard == ["Thanks for that.", " Could you say more?"]
    # The draft kept accumulating text after it was committed.
    assert settled(brain)[-1] == {
        "role": "assistant",
        "content": "Thanks for that. Could you say more?",
    }
    assert brain.turns[-1] == Turn(2, "interviewer", "Thanks for that. Could you say more?", 0)


@pytest.mark.parametrize(
    ("spoken", "recorded"),
    [("Thanks for", "Thanks for"), (None, "Thanks for that.")],
    ids=["spoken-text-known", "spoken-text-missing"],
)
async def test_a_committed_draft_that_is_cancelled_is_kept_and_finalized(spoken, recorded):
    # The candidate talked over the reply, and Vapi dropped the stream. It was spoken, so it
    # stays, and the next draft settles it.
    streamer = FakeStreamer(["Thanks for that.", HANG], ["What was the result?"])
    brain, _ = started(DEMO, streamer)
    task, heard = start_draft(brain, "I led the migration.")
    await wait_for(lambda: heard)
    assert brain.commit_spoken() is True
    await cancel(task)

    assert len(brain.history) == 2  # settled by the next draft, not by the cancel
    assert not brain.finished

    assert await hear(brain, "Sorry to cut in.", spoken) == ["What was the result?"]
    assert roles(brain.history) == "user assistant user system assistant"
    assert brain.history[4] == {"role": "assistant", "content": recorded}
    assert brain.turns[2] == Turn(2, "interviewer", recorded, 0, interrupted=True)
    assert streamer.calls[1]["messages"][4] == {"role": "assistant", "content": recorded}

    # The cancelled reply used its move: the draft that settled it is the second follow-up,
    # and after it only the wrap-up is left.
    assert brain.commit_spoken() is True
    assert await say(brain, LONG_ANSWER) == [WRAP_UP_LINE]


async def test_a_committed_draft_cancelled_before_any_text_is_recorded_as_cut_off():
    streamer = FakeStreamer([HANG], ["What was the result?"])
    brain, _ = started(DEMO, streamer)
    task = asyncio.create_task(say(brain, "I led the migration."))
    await wait_for(lambda: streamer.calls)
    assert brain.commit_spoken() is True
    await cancel(task)

    await hear(brain, "Sorry to cut in.", None)
    assert brain.history[4] == {"role": "assistant", "content": "(cut off)"}
    assert brain.turns[2] == Turn(2, "interviewer", "(cut off)", 0, interrupted=True)
    assert_history_valid(brain.history)


async def test_a_cancelled_draft_that_was_never_committed_leaves_no_trace():
    streamer = FakeStreamer(["Tell me", HANG], ["Second draft."])
    brain, _ = started(DEMO, streamer)
    history_before, turns_before = brain.history, brain.turns

    task, heard = start_draft(brain, "I led the migration.")
    await wait_for(lambda: heard)
    await cancel(task)

    assert brain.history == history_before
    assert brain.turns == turns_before
    assert brain.commit_spoken() is False  # the cancelled draft is gone, not pending
    assert not brain.finished

    # And it used no move: the next draft is the first follow-up again.
    assert await hear(brain, "I led the migration.") == ["Second draft."]
    assert streamer.calls[1]["messages"] == streamer.calls[0]["messages"]


async def test_closing_an_uncommitted_draft_early_closes_claude_stream_and_leaves_no_trace():
    streamer = FakeStreamer(["Tell me", " more.", " Please."])
    brain, _ = started(DEMO, streamer)
    history_before, turns_before = brain.history, brain.turns
    chunks = brain.draft("I led the migration.", None)
    assert await anext(chunks) == "Tell me"
    await chunks.aclose()  # the consumer stopped listening

    assert streamer.closed == 1  # the Claude stream was shut, not left to the garbage collector
    assert settled(brain) == history_before
    # The call ended on that answer, so it is kept; the reply that was never spoken is not.
    assert brain.turns == [*turns_before, Turn(1, "candidate", "I led the migration.", 0)]


async def test_closing_a_committed_draft_early_records_the_partial_text():
    streamer = FakeStreamer(["Tell me", " more.", " Please."])
    brain, _ = started(DEMO, streamer)
    chunks = brain.draft("I led the migration.", None)
    assert await anext(chunks) == "Tell me"
    assert brain.commit_spoken() is True
    await chunks.aclose()

    assert streamer.closed == 1
    assert settled(brain)[-1] == {"role": "assistant", "content": "Tell me"}
    assert_history_valid(brain.history)
    interviewer_turns = [t for t in brain.turns if t.speaker == "interviewer"]
    assert [t.idx for t in interviewer_turns] == [0, 2]
    assert interviewer_turns[-1] == Turn(2, "interviewer", "Tell me", 0, interrupted=True)


async def test_a_new_draft_started_before_the_old_one_is_cancelled_does_not_interleave():
    # The route should cancel the old task first. If it starts the new one first, the old one
    # is still alive when the new draft takes its first step, and that must be harmless.
    streamer = FakeStreamer(["Thanks for that.", HANG], ["What was the result?"])
    brain, _ = started(DEMO, streamer)
    first, heard = start_draft(brain, "I led the migration.")
    await wait_for(lambda: heard)
    assert brain.commit_spoken() is True

    second, second_heard = start_draft(brain, "Sorry to cut in.", "Thanks for")
    first.cancel()
    await second
    with pytest.raises(asyncio.CancelledError):
        await first

    assert second_heard == ["What was the result?"]
    assert roles(brain.history) == "user assistant user system assistant"
    assert brain.history[4] == {"role": "assistant", "content": "Thanks for"}
    assert_history_valid(brain.history)
    assert [t.speaker for t in brain.turns] == ["interviewer", "candidate", "interviewer"]


async def test_draft_settles_the_previous_reply_before_its_first_await():
    # Drive the generator by hand up to the first point where it has to wait. By then the
    # previous reply must be settled, the old draft dropped and the new one pending, because
    # a cancelled old task can only be cleaned up after that point.
    streamer = FakeStreamer(["Thanks for that."], [HANG])
    brain, _ = started(DEMO, streamer)
    await say(brain, "I led the migration.")
    assert len(brain.history) == 2

    step = brain.draft("Sorry to cut in.", "Thanks for").__anext__()
    step.send(None)  # runs until the generator first waits, here on Claude's stream

    assert brain.history[4] == {"role": "assistant", "content": "Thanks for"}
    assert streamer.calls[1]["messages"][4] == {"role": "assistant", "content": "Thanks for"}
    assert brain.commit_spoken() is True  # the new draft is already the pending one

    with pytest.raises(asyncio.CancelledError):
        step.throw(asyncio.CancelledError)


# --- finalize -----------------------------------------------------------------


async def test_finalize_settles_the_committed_draft_with_its_generated_text():
    brain, _ = started(DEMO, FakeStreamer(["Tell me more."]))
    await say(brain, "I led the migration.")
    assert len(brain.history) == 2

    brain.finalize()
    assert roles(brain.history) == "user assistant user system assistant"
    assert brain.history[-1] == {"role": "assistant", "content": "Tell me more."}
    assert brain.turns[-1] == Turn(2, "interviewer", "Tell me more.", 0, interrupted=False)

    brain.finalize()  # settling twice changes nothing
    assert len(brain.history) == 5
    assert len(brain.turns) == 3


async def test_finalize_with_a_reply_still_streaming_records_the_partial_text():
    streamer = FakeStreamer(["Tell me", HANG])
    brain, _ = started(DEMO, streamer)
    task, heard = start_draft(brain, "I led the migration.")
    await wait_for(lambda: heard)
    brain.commit_spoken()

    brain.finalize()  # the call ended mid-line
    assert brain.history[-1] == {"role": "assistant", "content": "Tell me"}
    assert brain.turns[-1].interrupted
    await cancel(task)
    assert brain.history[-1] == {"role": "assistant", "content": "Tell me"}  # no late changes


async def test_finalize_records_the_answer_of_a_draft_that_was_never_spoken():
    # The call ends (End, the cap, a dropped connection) before Vapi speaks the reply to the
    # newest answer. The answer is the candidate's words and is kept; the reply is dropped.
    brain, _ = started(DEMO, FakeStreamer(["Tell me more."]))
    await hear(brain, "I led the migration.")
    history_before, turns_before = brain.history, brain.turns

    brain.finalize()
    assert brain.commit_spoken() is False
    assert brain.history == history_before  # nothing more will be asked of Claude
    assert brain.turns == [*turns_before, Turn(1, "candidate", "I led the migration.", 0)]
    assert not brain.finished

    brain.finalize()  # finalizing twice records it once
    assert len(brain.turns) == 2


async def test_finalize_records_the_answer_whose_reply_was_still_being_written():
    # The route cancels a reply still streaming before it finalizes, and a cancelled draft is no
    # longer pending. Its answer is still the newest thing the candidate said.
    brain, _ = started(DEMO, FakeStreamer(["Tell me", HANG]))
    task, heard = start_draft(brain, "I led the migration.")
    await wait_for(lambda: heard)
    await cancel(task)

    brain.finalize()
    assert [(t.speaker, t.text, t.question_idx) for t in brain.turns][1:] == [
        ("candidate", "I led the migration.", 0)
    ]
    assert len(brain.history) == 2


async def test_finalize_records_only_the_newest_answer():
    # Vapi's requests carry the whole answer so far, so the newest one has all of it.
    brain, _ = started(DEMO, FakeStreamer(["One."], ["Two."], ["Three."]))
    for partial in ("I led", "I led the migration", "I led the migration and it shipped."):
        await hear(brain, partial)

    brain.finalize()
    assert [(t.speaker, t.text) for t in brain.turns][1:] == [
        ("candidate", "I led the migration and it shipped.")
    ]


@pytest.mark.parametrize("blank", ["", "   "])
async def test_finalize_records_no_answer_for_a_blank_request(blank):
    brain, _ = started(DEMO, FakeStreamer())
    await hear(brain, blank)
    brain.finalize()
    assert [t.speaker for t in brain.turns] == ["interviewer"]


async def test_finalize_writes_the_spoken_reply_before_the_answer_that_followed_it():
    # A reply was spoken, the candidate answered it, and the call ended before the interviewer
    # replied again. The turns keep the order things were said in.
    brain, clock = started(FULL, FakeStreamer(["Tell me more."], ["Never spoken."]))
    clock.advance(60)
    await say(brain, LONG_ANSWER)
    await hear(brain, "It shipped a week early.", "Tell me more.")

    brain.finalize()
    assert [(t.speaker, t.text, t.question_idx) for t in brain.turns] == [
        ("interviewer", brain.turns[0].text, 0),
        ("candidate", LONG_ANSWER, 0),
        ("interviewer", "Tell me more.", 0),
        ("candidate", "It shipped a week early.", 0),
    ]
    assert_history_valid(brain.history)
    assert [m["content"] for m in brain.history if m["role"] == "user"][1:] == [LONG_ANSWER]


async def test_an_answer_that_was_spoken_to_is_not_recorded_again_by_finalize():
    brain, _ = started(DEMO, FakeStreamer(["Tell me more."]))
    await say(brain, "I led the migration.")
    brain.finalize()
    assert [t.speaker for t in brain.turns] == ["interviewer", "candidate", "interviewer"]


async def test_finalize_with_nothing_to_settle_changes_nothing():
    brain, _ = started(DEMO, FakeStreamer())
    history_before, turns_before = brain.history, brain.turns
    brain.finalize()
    assert brain.history == history_before
    assert brain.turns == turns_before


# --- turns --------------------------------------------------------------------


async def test_turns_record_speaker_and_question_idx():
    streamer = FakeStreamer(["fu one"], ["fu two"], ["Thanks. Question number 1."], ["fu three"])
    brain, clock = started(FULL, streamer)
    for _ in range(4):
        clock.advance(60)
        await say(brain, LONG_ANSWER)
    brain.finalize()

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


async def test_a_dropped_draft_that_asks_the_next_question_does_not_move_the_answer_to_it():
    # Drafts are built on plan copies, so a draft that would ask question 1 is not real until
    # it is spoken. The answer it follows still belongs to question 0 either way.
    streamer = FakeStreamer(["a"], ["b"], ["Dropped."], ["Thanks. Question number 1."])
    brain, _ = started(FULL, streamer)
    await say(brain, LONG_ANSWER)
    await say(brain, LONG_ANSWER)
    assert await hear(brain, LONG_ANSWER) == ["Dropped."]  # asks question 1, never spoken
    brain.finalize()
    # The answer the call ended on is kept, under question 0; the reply asking question 1 is not.
    assert [(t.speaker, t.question_idx) for t in brain.turns][-2:] == [
        ("interviewer", 0),
        ("candidate", 0),
    ]
    assert "Dropped." not in [t.text for t in brain.turns]

    await say(brain, LONG_ANSWER)  # this time the question is spoken
    brain.finalize()
    assert [(t.speaker, t.question_idx) for t in brain.turns][-2:] == [
        ("candidate", 0),
        ("interviewer", 1),
    ]


async def test_wrap_up_turn_belongs_to_the_question_in_progress():
    brain, clock = started(DEMO, FakeStreamer())
    clock.advance(290)
    await say(brain, "Okay.")
    brain.finalize()
    turns = brain.turns
    assert [(t.speaker, t.question_idx) for t in turns] == [
        ("interviewer", 0),
        ("candidate", 0),
        ("interviewer", 0),
    ]
    assert turns[-1].text == WRAP_UP_LINE


# --- any order of drafts, commits, cancels and finalizes ---------------------

RANDOM_SCRIPTS = [
    ["Tell me more about that."],
    ["Tell me", " more about that."],
    ["So tell me", HANG],
    [HANG],
    [BrainError("overloaded")],
    ["Go on.", BrainError("dropped")],
    [],
]
RANDOM_ANSWERS = ["", "   ", "I led it.", LONG_ANSWER]


def count_words(text: str) -> int:
    """Words as the oracle counts them: runs of letters and digits, with a hyphen inside a word
    ("twenty-five") joining it. Punctuation, quotes, dashes and spacing do not count."""
    return len(re.findall(r"\w+(?:-\w+)*", text))


def instruction_for(move: Move) -> str | None:
    if move.kind is MoveKind.WRAP_UP:
        return None
    text = move.question.text if move.question is not None else ""
    return MOVE_INSTRUCTION[(move.kind, move.probe)].format(text=text)


@dataclass
class Draft:
    """The test's record of one draft: what was asked of it and the task consuming it."""

    text: str
    elapsed_s: float
    answering_idx: int
    move: Move
    task: asyncio.Task
    heard: list[str]


class RandomSession:
    """Drives a Brain with a random mix of drafts, commits, cancels and finalizes, and works out
    the right answer the slow way. The oracle never copies a plan the way the brain does. It
    replays the committed drafts' inputs on a fresh plan, which is what "the plan advanced
    exactly once per committed draft" means."""

    def __init__(self, seed: int) -> None:
        self.rng = random.Random(seed)
        self.config = self.rng.choice([DEMO, FULL])
        self.streamer = FakeStreamer(*(self.rng.choice(RANDOM_SCRIPTS) for _ in range(60)))
        self.brain, self.clock = started(self.config, self.streamer)
        self.t0 = self.clock.now
        self.stats: Counter = Counter()
        # What the brain should hold
        self.history = self.brain.history
        self.turns = copy.deepcopy(self.brain.turns)
        self.spoken_inputs: list[tuple[float, int]] = []  # (elapsed, words) of each commit
        self.pending: Draft | None = None
        self.committed: Draft | None = None  # spoken, but not settled into the history yet
        self.running: Draft | None = None  # the newest draft's task, in whatever state
        # The newest draft whose answer no turn holds yet, cancelled or not: finalize() records it.
        self.unanswered: Draft | None = None

    def replayed_plan(self) -> InterviewPlan:
        plan = InterviewPlan(self.config, qs(3))
        plan.opening_line()
        for elapsed_s, answer_words in self.spoken_inputs:
            plan.next_move(elapsed_s, answer_words)
        return plan

    def check(self) -> None:
        assert self.brain.history == self.history
        assert self.brain.turns == self.turns
        assert_history_valid(self.brain.history)
        assert self.brain.finished == self.replayed_plan().finished

    def settle(self, spoken: str | None) -> None:
        """What the brain must append when the committed draft is settled."""
        draft = self.committed
        if draft is None:
            return
        self.committed = None
        generated = "".join(draft.heard)
        produced = draft.task.done() and not draft.task.cancelled()
        spoken_words = count_words(spoken or "")
        cut_short = 0 < spoken_words < count_words(generated)
        text = spoken if cut_short else generated
        if not text.strip():
            text = "(cut off)"
            self.stats["settled_as_cut_off"] += 1
        self.stats["settled_shorter"] += cut_short
        # The case a length comparison got wrong: fewer characters, every word.
        spoken_chars = len((spoken or "").strip())
        if not cut_short and 0 < spoken_chars < len(generated.strip()):
            self.stats["settled_same_words_fewer_characters"] += 1
        self.stats["settled_unfinished"] += not produced
        self.history.append(
            {"role": "user", "content": draft.text if draft.text.strip() else "(inaudible)"}
        )
        instruction = instruction_for(draft.move)
        if instruction is not None:
            self.history.append({"role": "system", "content": instruction})
        self.history.append({"role": "assistant", "content": text})
        self.turns.append(
            Turn(
                len(self.turns),
                "interviewer",
                text,
                draft.move.question_idx,
                interrupted=cut_short or not produced,
            )
        )

    def pick_spoken(self) -> str | None:
        generated = "".join(self.committed.heard) if self.committed else "Whatever it said."
        return self.rng.choice(
            [
                None,
                "",
                "   ",
                generated,
                generated[: len(generated) // 2],
                generated[:-1],
                generated + " and more",
                "Some rebuilt words.",
            ]
        )

    async def draft(self) -> None:
        text = self.rng.choice(RANDOM_ANSWERS)
        spoken = self.pick_spoken()
        self.clock.advance(self.rng.choice([0, 5, 30, 90]))
        elapsed_s = self.clock.now - self.t0

        plan = self.replayed_plan()
        answering_idx = plan.current_question_idx
        move = plan.next_move(elapsed_s, len(text.split()))

        self.stats["drafts_dropped"] += self.pending is not None
        self.settle(spoken)
        self.pending = None
        self.unanswered = None
        old = self.running
        calls_before = len(self.streamer.calls)

        # The route cancels the old task first, but the brain has to cope with either order.
        cancel_first = self.rng.random() < 0.5
        if cancel_first and old is not None:
            old.task.cancel()
        task, heard = start_draft(self.brain, text, spoken)
        if not cancel_first and old is not None:
            old.task.cancel()
        await asyncio.sleep(0)  # the new draft takes its first step, and the old one is told off

        draft = Draft(text, elapsed_s, answering_idx, move, task, heard)
        self.running = draft
        if move.kind is MoveKind.DONE:
            self.stats["empty_drafts"] += 1
            assert heard == [] and task.done()
            assert len(self.streamer.calls) == calls_before
        else:
            self.pending = draft
            self.unanswered = draft
            if move.kind is MoveKind.WRAP_UP:
                assert heard == [WRAP_UP_LINE]
                assert len(self.streamer.calls) == calls_before
            else:
                assert len(self.streamer.calls) == calls_before + 1
                sent = self.streamer.calls[-1]["messages"]
                assert sent == [
                    *self.history,
                    {"role": "user", "content": text if text.strip() else "(inaudible)"},
                    {"role": "system", "content": instruction_for(move)},
                ]
        self.check()

    def commit(self) -> None:
        got = self.brain.commit_spoken()
        assert got is (self.pending is not None)
        if got:
            draft = self.pending
            self.stats["commits"] += 1
            self.stats["commits_while_streaming"] += not draft.task.done()
            self.spoken_inputs.append((draft.elapsed_s, len(draft.text.split())))
            self.turns.append(Turn(len(self.turns), "candidate", draft.text, draft.answering_idx))
            self.committed, self.pending, self.unanswered = draft, None, None
            self.stats["wrap_ups_spoken"] += self.brain.finished
        else:
            self.stats["commits_with_nothing_pending"] += 1
        self.check()

    def finalize(self) -> None:
        self.brain.finalize()
        self.settle(None)
        answer = self.unanswered
        self.pending = self.unanswered = None
        if answer is not None and answer.text.strip():
            # The call ended before any reply to this answer was spoken: only the answer is kept.
            self.turns.append(Turn(len(self.turns), "candidate", answer.text, answer.answering_idx))
            self.stats["unanswered_answers_kept"] += 1
            self.stats["unanswered_answers_kept_after_a_cancel"] += answer.task.cancelled()
        self.stats["finalizes"] += 1
        self.check()

    async def cancel_running(self) -> None:
        if self.running is not None:
            self.running.task.cancel()
        await asyncio.sleep(0)
        self.stats["cancels"] += 1
        # A draft cancelled before its line was finished, and never spoken, is gone. One that
        # was spoken, or finished before the cancel arrived, is not touched.
        if self.pending is not None and self.pending.task.cancelled():
            self.pending = None
            self.stats["pending_drafts_cancelled"] += 1
        self.check()

    async def run(self, steps: int = 25) -> Counter:
        ops = ["draft", "commit", "finalize", "cancel"]
        for _ in range(steps):
            op = self.rng.choices(ops, weights=[5, 4, 1, 1])[0]
            if op == "draft":
                await self.draft()
            elif op == "commit":
                self.commit()
            elif op == "finalize":
                self.finalize()
            else:
                await self.cancel_running()
        await self.cancel_running()
        self.finalize()
        assert self.committed is None
        return self.stats


@pytest.mark.parametrize("seed", range(60))
async def test_random_mixes_keep_the_history_valid_and_advance_the_plan_once_per_commit(seed):
    await RandomSession(seed).run()


async def test_the_random_mixes_reach_every_interesting_case():
    total: Counter = Counter()
    for seed in range(60):
        total += await RandomSession(seed).run()
    for case in (
        "commits",
        "commits_while_streaming",
        "commits_with_nothing_pending",
        "drafts_dropped",
        "pending_drafts_cancelled",
        "empty_drafts",
        "wrap_ups_spoken",
        "settled_shorter",
        "settled_same_words_fewer_characters",
        "settled_unfinished",
        "settled_as_cut_off",
        "finalizes",
        "unanswered_answers_kept",
        "unanswered_answers_kept_after_a_cancel",
        "cancels",
    ):
        assert total[case] > 0, f"no random session reached {case}"


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


async def test_an_api_error_becomes_a_brain_error_that_names_its_status_type_and_request():
    # The first live run is where failures show up, so the log line has to say which: the HTTP
    # status, the error type the API names and the request id. Its free text is left out.
    response = httpx2.Response(
        529,
        headers={"request-id": "req_011CU123"},
        request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"),
    )
    body = {"type": "error", "error": {"type": "overloaded_error", "message": "free text here"}}
    error = anthropic.APIStatusError("free text here", response=response, body=body)
    client = FakeAnthropicClient(open_error=error)

    with pytest.raises(BrainError) as raised:
        async for _ in AnthropicStreamer(client).stream(system=[], messages=[]):
            pass

    message = str(raised.value)
    assert message == (
        "Claude request failed: APIStatusError (HTTP 529, overloaded_error, request req_011CU123)"
    )


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
    assert_history_valid(settled(brain))


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
