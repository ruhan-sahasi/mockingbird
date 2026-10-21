import json
import logging
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import get_args

import anthropic
import httpx2
import pytest

from mockingbird.models import Interview, Turn
from mockingbird.report import (
    GRADER_SYSTEM,
    Grader,
    GradeResult,
    GradingError,
    ItemGrade,
    QuestionGrade,
    build_report,
    load_rubric,
    normalize,
    verify_quotes,
)
from mockingbird.store import MemoryStore

ITEMS = ["situation", "task", "action", "result", "specificity", "ownership"]
QUESTIONS = ["Tell me about a time you led a project.", "Tell me about a disagreement."]
INTERVIEW_ID = "iv1"

# What was said, in order. Question 0 gets a follow-up. Each answer sits with the question it
# answers, so the candidate's reply to the follow-up and to the question after it are the last
# turns of their own question, and the interviewer's next question opens the next one.
TURNS = [
    Turn(0, "interviewer", "Hi there. Tell me about a time you led a project.", 0),
    Turn(1, "candidate", "I led the migration of our billing service. We cut errors by half.", 0),
    Turn(2, "interviewer", "What was your part in the cutover?", 0),
    Turn(3, "candidate", "I wrote the cutover plan myself and ran it on a Saturday.", 0),
    Turn(4, "interviewer", "Tell me about a disagreement.", 1),
    Turn(5, "candidate", "My teammate wanted a rewrite and I pushed back with data.", 1),
]


def item(name: str, quotes: list[str], score: int = 3, rationale: str = "Because.") -> ItemGrade:
    return ItemGrade(item=name, score=score, rationale=rationale, quotes=quotes)


def question_with(idx: int, quotes: list[str], score: int = 3) -> QuestionGrade:
    """A question graded on all six items, each citing the same quotes."""
    return QuestionGrade(question_idx=idx, items=[item(name, quotes, score) for name in ITEMS])


def grade_result_for(*questions: QuestionGrade) -> GradeResult:
    return GradeResult(questions=list(questions))


async def stored_interview(
    turns: list[Turn], questions: list[str] | None = None, store: MemoryStore | None = None
) -> MemoryStore:
    store = store or MemoryStore()
    await store.create_interview(
        Interview(
            id=INTERVIEW_ID,
            mode="demo",
            status="ended",
            visitor_hash="visitor",
            ip_hash="ip",
            cap_s=300,
            questions=list(QUESTIONS if questions is None else questions),
            created_at=datetime(2026, 10, 19, 12, 0, tzinfo=UTC),
        )
    )
    for turn in turns:
        await store.add_turn(INTERVIEW_ID, turn)
    return store


class FakeGrader:
    """Answers `grade` with a prepared result, or raises a prepared error. Records its calls."""

    def __init__(self, result: GradeResult | None = None, error: Exception | None = None):
        self.calls: list[tuple[list[Turn], list[str]]] = []
        self._result = result
        self._error = error

    async def grade(self, turns: list[Turn], questions: list[str]) -> GradeResult:
        self.calls.append((turns, questions))
        if self._error is not None:
            raise self._error
        assert self._result is not None
        return self._result


# normalize


def test_normalize():
    assert normalize("I “led” it,  fast!") == "i led it fast"


def test_normalize_folds_curly_apostrophes_and_other_whitespace():
    assert normalize("We didn’t\twait\n\nfor   it — really…") == "we didnt wait for it really"
    assert normalize("We didn't wait") == normalize("We didn’t wait")


# verify_quotes


def test_verify_keeps_real_quote_and_drops_invented():
    result = grade_result_for(
        QuestionGrade(
            question_idx=0,
            items=[item("action", ["I wrote the cutover plan", "I fixed the whole company"])],
        )
    )

    report = verify_quotes(result, {0: "Well, I wrote the cutover plan myself."})

    assert report["status"] == "graded"
    [graded] = report["questions"][0]["items"]
    assert graded == {
        "item": "action",
        "score": 3,
        "rationale": "Because.",
        "quotes": ["I wrote the cutover plan"],
        "dropped_quotes": ["I fixed the whole company"],
        "status": "scored",
    }
    assert report["questions"][0]["question_idx"] == 0


def test_verify_matches_after_normalizing_both_sides():
    result = grade_result_for(
        QuestionGrade(question_idx=0, items=[item("task", ["I “led” it,  fast!"])])
    )

    report = verify_quotes(result, {0: "so I led it fast and moved on"})

    assert report["questions"][0]["items"][0]["quotes"] == ["I “led” it,  fast!"]


def test_item_without_verified_quote_is_not_enough_evidence():
    result = grade_result_for(
        QuestionGrade(
            question_idx=0,
            items=[
                item("result", ["we won every award"], score=4),
                item("ownership", [], score=2),
            ],
        )
    )

    report = verify_quotes(result, {0: "I led the migration."})

    invented, no_quotes = report["questions"][0]["items"]
    assert invented["score"] is None
    assert invented["status"] == "not_enough_evidence"
    assert invented["quotes"] == []
    assert invented["dropped_quotes"] == ["we won every award"]
    assert invented["rationale"] == "Because."
    assert no_quotes["score"] is None
    assert no_quotes["status"] == "not_enough_evidence"


def test_quotes_checked_against_their_own_question_only():
    # The words are real, but the candidate said them answering question 1, not question 0.
    result = grade_result_for(
        QuestionGrade(question_idx=0, items=[item("action", ["I pushed back with data"])]),
        QuestionGrade(question_idx=1, items=[item("action", ["I pushed back with data"])]),
    )
    by_question = {0: "I led the migration.", 1: "I pushed back with data."}

    report = verify_quotes(result, by_question)

    q0, q1 = report["questions"]
    assert q0["items"][0]["status"] == "not_enough_evidence"
    assert q0["items"][0]["dropped_quotes"] == ["I pushed back with data"]
    assert q1["items"][0]["status"] == "scored"
    assert q1["items"][0]["quotes"] == ["I pushed back with data"]


def test_question_with_no_candidate_text_gets_no_scores():
    result = grade_result_for(question_with(2, ["I led the migration"]))

    report = verify_quotes(result, {0: "I led the migration."})

    assert {i["status"] for i in report["questions"][0]["items"]} == {"not_enough_evidence"}


@pytest.mark.parametrize("quote", ["", "   ", "...", "“ ”"])
def test_a_quote_with_no_words_in_it_is_never_kept(quote: str):
    # Nothing is left of such a quote once it is normalized, and "" is inside every string.
    result = grade_result_for(QuestionGrade(question_idx=0, items=[item("action", [quote])]))

    report = verify_quotes(result, {0: "I led the migration."})

    graded = report["questions"][0]["items"][0]
    assert graded["quotes"] == []
    assert graded["dropped_quotes"] == [quote]
    assert graded["status"] == "not_enough_evidence"


# the grader's request


def test_rubric_file_has_six_items_with_four_descriptors_each():
    rubric = load_rubric()

    assert list(rubric) == ITEMS
    # The names in the file are the choices the grader's answer may use.
    assert list(get_args(ItemGrade.model_fields["item"].annotation)) == ITEMS
    for name in ITEMS:
        assert sorted(rubric[name]["scores"]) == [1, 2, 3, 4]
        assert rubric[name]["asks"]


def test_rubric_loads_the_same_from_any_working_directory(tmp_path, monkeypatch):
    expected = load_rubric()
    monkeypatch.chdir(tmp_path)

    assert load_rubric() == expected


class FakeBetaClient:
    """Stands in for AsyncAnthropic. Records the kwargs of beta.messages.stream(**kwargs) and
    answers with a prepared final message. It has no `parse`: the grade is streamed, because a
    request this long would otherwise wait on one HTTP response for the whole generation."""

    def __init__(self, response):
        self.requests: list[dict] = []
        self.closed = 0
        self._response = response
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream))

    def _stream(self, **kwargs):
        self.requests.append(kwargs)
        return FakeStream(self)


class FakeStream:
    """The async context manager beta.messages.stream returns, holding the final message."""

    def __init__(self, client: FakeBetaClient) -> None:
        self._client = client

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> None:
        self._client.closed += 1

    async def get_final_message(self):
        return self._client._response


async def test_grader_request_shape():
    graded = grade_result_for(question_with(0, ["I led the migration"]))
    client = FakeBetaClient(SimpleNamespace(stop_reason="end_turn", parsed_output=graded))
    turns = [*TURNS, Turn(6, "candidate", "", 1)]  # silence: left out of what the grader reads

    result = await Grader(client, load_rubric()).grade(turns, QUESTIONS)

    assert result is graded
    assert client.closed == 1  # the stream is shut once the final message is in
    [request] = client.requests
    [message] = request.pop("messages")
    assert message["role"] == "user"
    assert isinstance(message["content"], str)
    assert request == {
        "model": "claude-opus-5-5",
        "max_tokens": 16000,
        "betas": ["server-side-fallback-2026-07-01"],
        "fallbacks": "default",
        "output_config": {"effort": "high"},
        "system": GRADER_SYSTEM,
        "output_format": GradeResult,
    }
    # Opus 5.5 thinks adaptively by default and refuses thinking={"type": "disabled"}, so the
    # request has no `thinking` at all (the equality above already rules it out).


async def test_grader_reads_the_rubric_and_the_transcript_by_question():
    graded = grade_result_for(question_with(0, ["I led the migration"]))
    client = FakeBetaClient(SimpleNamespace(stop_reason="end_turn", parsed_output=graded))
    turns = [*TURNS, Turn(6, "candidate", "  ", 1)]
    rubric = load_rubric()

    await Grader(client, rubric).grade(turns, QUESTIONS)

    [message] = client.requests[0]["messages"]
    content = message["content"]
    # The rubric: every item with its asks and each of the four score descriptors.
    for name in ITEMS:
        assert name in content
        assert rubric[name]["asks"] in content
        for descriptor in rubric[name]["scores"].values():
            assert descriptor in content
    # The transcript: grouped under the question it belongs to, speaker by speaker, in order.
    q0 = content.index(f"Question 0: {QUESTIONS[0]}")
    q1 = content.index(f"Question 1: {QUESTIONS[1]}")
    assert q0 < q1
    lines = [
        "Interviewer: What was your part in the cutover?",
        "Candidate: I wrote the cutover plan myself and ran it on a Saturday.",
    ]
    assert content.index(lines[0]) < content.index(lines[1]) < q1
    assert content.index("Candidate: My teammate wanted a rewrite") > q1
    # The blank turn is not in there.
    assert content.count("Candidate:") == 3


def test_system_prompt_states_the_grading_rules():
    assert "six items" in GRADER_SYSTEM
    assert "verbatim" in GRADER_SYSTEM
    assert "Never quote the interviewer" in GRADER_SYSTEM
    # The transcript is the candidate's words, so it is data and never an instruction.
    assert "instructions" in GRADER_SYSTEM


async def test_grader_raises_grading_error_on_a_refusal():
    # The refusal comes with a result that parses. Only the stop reason says it is a refusal, so
    # this fails if the stop reason is not checked. A missing result would not prove that, since
    # an empty reply raises GradingError anyway.
    partial = grade_result_for(question_with(0, ["I led the migration"]))
    client = FakeBetaClient(SimpleNamespace(stop_reason="refusal", parsed_output=partial))

    with pytest.raises(GradingError):
        await Grader(client, load_rubric()).grade(TURNS, QUESTIONS)


async def test_grader_raises_grading_error_when_nothing_parsed():
    # A reply cut off at max_tokens has no complete result in it.
    client = FakeBetaClient(SimpleNamespace(stop_reason="max_tokens", parsed_output=None))

    with pytest.raises(GradingError):
        await Grader(client, load_rubric()).grade(TURNS, QUESTIONS)


# The same request through the real SDK, with only the network replaced. This is what proves the
# SDK really puts `fallbacks`, the beta header and the output schema on the wire.


def message_reply(text: str = "", stop_reason: str = "end_turn") -> dict:
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-5-5",
        "content": [{"type": "text", "text": text}] if text else [],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 10},
    }


def sse(reply: dict) -> str:
    """`reply` as the event stream the API sends for a streamed request."""
    message = {**reply, "content": [], "stop_reason": None}
    events = [{"type": "message_start", "message": message}]
    for index, block in enumerate(reply["content"]):
        events += [
            {"type": "content_block_start", "index": index, "content_block": {**block, "text": ""}},
            {
                "type": "content_block_delta",
                "index": index,
                "delta": {"type": "text_delta", "text": block["text"]},
            },
            {"type": "content_block_stop", "index": index},
        ]
    events += [
        {
            "type": "message_delta",
            "delta": {"stop_reason": reply["stop_reason"], "stop_sequence": None},
            "usage": {"output_tokens": 10},
        },
        {"type": "message_stop"},
    ]
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)


def real_sdk_grader(reply: dict) -> tuple[Grader, list[httpx2.Request]]:
    requests: list[httpx2.Request] = []

    def answer(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(200, text=sse(reply), headers={"content-type": "text/event-stream"})

    client = anthropic.AsyncAnthropic(
        api_key="test-key",
        max_retries=0,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(answer)),
    )
    return Grader(client, load_rubric()), requests


async def test_real_sdk_sends_fallback_beta_and_schema_and_parses_the_reply():
    graded = grade_result_for(question_with(0, ["I led the migration"], score=4))
    grader, requests = real_sdk_grader(message_reply(graded.model_dump_json()))

    result = await grader.grade(TURNS, QUESTIONS)

    assert result == graded
    [request] = requests
    assert request.url.path == "/v1/messages"
    assert "server-side-fallback-2026-07-01" in request.headers["anthropic-beta"]
    body = json.loads(request.content)
    assert body["stream"] is True
    assert body["model"] == "claude-opus-5-5"
    assert body["fallbacks"] == "default"
    assert body["max_tokens"] == 16000
    assert body["output_config"]["effort"] == "high"
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert "thinking" not in body
    assert body["system"] == GRADER_SYSTEM
    assert [m["role"] for m in body["messages"]] == ["user"]


async def test_real_sdk_refusal_becomes_grading_error():
    # A refusal whose text is a complete, valid grade. The SDK parses it, so only the stop reason
    # separates it from a real grade.
    graded = grade_result_for(question_with(0, ["I led the migration"]))
    grader, _ = real_sdk_grader(message_reply(text=graded.model_dump_json(), stop_reason="refusal"))

    with pytest.raises(GradingError):
        await grader.grade(TURNS, QUESTIONS)


async def test_real_sdk_reply_cut_off_at_max_tokens_becomes_grading_error():
    # The streamed text stops partway through the JSON. The SDK's parse fails, and that error's
    # message quotes the text, so only a fixed message may come out of the grader.
    cut = '{"questions": [{"question_idx": 0, "items": [{"item": "action", "rationale": "I led'
    grader, _ = real_sdk_grader(message_reply(text=cut, stop_reason="max_tokens"))

    with pytest.raises(GradingError) as raised:
        await grader.grade(TURNS, QUESTIONS)

    assert "I led" not in str(raised.value)
    assert raised.value.__cause__ is None and raised.value.__suppress_context__


# build_report


async def test_build_report_saves_the_verified_report_and_marks_it_graded():
    graded = grade_result_for(
        QuestionGrade(
            question_idx=0,
            items=[
                # Said in the answer to the follow-up, so it counts for question 0.
                item("action", ["I wrote the cutover plan myself"]),
                # The interviewer said this, never the candidate.
                item("task", ["What was your part in the cutover"]),
            ],
        ),
        QuestionGrade(
            question_idx=1, items=[item("ownership", ["I pushed back with data"], score=4)]
        ),
    )
    store = await stored_interview(TURNS)
    grader = FakeGrader(graded)

    await build_report(store, grader, INTERVIEW_ID)

    report = await store.get_report(INTERVIEW_ID)
    assert report is not None
    assert report["status"] == "graded"
    q0, q1 = report["questions"]
    assert (q0["question_idx"], q0["question_text"]) == (0, QUESTIONS[0])
    assert (q1["question_idx"], q1["question_text"]) == (1, QUESTIONS[1])
    action, task = q0["items"]
    assert (action["status"], action["quotes"]) == ("scored", ["I wrote the cutover plan myself"])
    assert task["status"] == "not_enough_evidence"
    assert task["score"] is None
    assert task["dropped_quotes"] == ["What was your part in the cutover"]
    assert q1["items"][0]["score"] == 4
    interview = await store.get_interview(INTERVIEW_ID)
    assert interview is not None and interview.status == "graded"
    # The grader got every stored turn, and the main question texts from the interview.
    assert grader.calls == [(TURNS, QUESTIONS)]


async def test_a_graded_question_the_interview_does_not_have_is_left_out():
    # The grader names a question the interview does not have.
    graded = grade_result_for(
        question_with(0, ["I led the migration"]), question_with(7, ["I led the migration"])
    )
    store = await stored_interview(TURNS)

    await build_report(store, FakeGrader(graded), INTERVIEW_ID)

    report = await store.get_report(INTERVIEW_ID)
    assert report is not None
    assert [q["question_idx"] for q in report["questions"]] == [0]


async def test_no_candidate_turns_skips_grader():
    store = await stored_interview([TURNS[0]])
    grader = FakeGrader(error=AssertionError("the grader must not be called"))

    await build_report(store, grader, INTERVIEW_ID)

    assert grader.calls == []
    assert await store.get_report(INTERVIEW_ID) == {"status": "no_answers", "questions": []}
    interview = await store.get_interview(INTERVIEW_ID)
    assert interview is not None and interview.status == "graded"


async def test_blank_candidate_turns_count_as_no_answer():
    # A muted mic: the brain stored silence as blank candidate turns, not as missing turns.
    silent = [
        TURNS[0],
        Turn(1, "candidate", "", 0),
        Turn(2, "interviewer", "Are you still there?", 0),
        Turn(3, "candidate", "   ", 0),
    ]
    store = await stored_interview(silent)
    grader = FakeGrader(error=AssertionError("the grader must not be called"))

    await build_report(store, grader, INTERVIEW_ID)

    assert grader.calls == []
    assert await store.get_report(INTERVIEW_ID) == {"status": "no_answers", "questions": []}


async def test_an_interview_with_no_turns_at_all_has_no_answers():
    store = await stored_interview([])
    grader = FakeGrader(error=AssertionError("the grader must not be called"))

    await build_report(store, grader, INTERVIEW_ID)

    assert grader.calls == []
    assert await store.get_report(INTERVIEW_ID) == {"status": "no_answers", "questions": []}


async def test_grading_error_saves_failed_status():
    store = await stored_interview(TURNS)

    await build_report(store, FakeGrader(error=GradingError("refused")), INTERVIEW_ID)

    assert await store.get_report(INTERVIEW_ID) == {"status": "grading_failed", "questions": []}
    interview = await store.get_interview(INTERVIEW_ID)
    assert interview is not None and interview.status == "graded"


async def test_any_other_failure_saves_failed_status_and_logs_only_its_type(caplog):
    secret = "I led the migration for Acme and my manager is Dana"
    store = await stored_interview(TURNS)
    caplog.set_level(logging.DEBUG)

    await build_report(store, FakeGrader(error=RuntimeError(secret)), INTERVIEW_ID)

    assert await store.get_report(INTERVIEW_ID) == {"status": "grading_failed", "questions": []}
    assert "RuntimeError" in caplog.text
    assert secret not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


async def test_an_api_failure_logs_its_status_type_and_request_but_not_its_text(caplog):
    caplog.set_level(logging.DEBUG)
    secret = "I led the migration for Acme"
    response = httpx2.Response(
        500,
        headers={"request-id": "req_011CU456"},
        request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"),
    )
    body = {"type": "error", "error": {"type": "api_error", "message": secret}}
    error = anthropic.InternalServerError(secret, response=response, body=body)
    store = await stored_interview(TURNS)

    await build_report(store, FakeGrader(error=error), INTERVIEW_ID)

    assert await store.get_report(INTERVIEW_ID) == {"status": "grading_failed", "questions": []}
    assert "InternalServerError (HTTP 500, api_error, request req_011CU456)" in caplog.text
    assert secret not in caplog.text


class StrictStore(MemoryStore):
    """A MemoryStore whose save_report refuses reports the way PostgresStore does."""

    def __init__(self, *, refuse_graded: bool = False, break_everything: bool = False) -> None:
        super().__init__()
        self._refuse_graded = refuse_graded
        self._break_everything = break_everything
        self.saved: list[str] = []

    async def save_report(self, interview_id: str, report: dict) -> None:
        if self._break_everything:
            raise ConnectionError("database is down")
        if self._refuse_graded and report["status"] == "graded":
            raise ValueError("report is missing 'score'")
        self.saved.append(report["status"])
        await super().save_report(interview_id, report)


async def test_a_report_the_store_refuses_is_replaced_by_a_failed_one():
    graded = grade_result_for(question_with(0, ["I led the migration"]))
    store = await stored_interview(TURNS, store=StrictStore(refuse_graded=True))

    await build_report(store, FakeGrader(graded), INTERVIEW_ID)

    assert store.saved == ["grading_failed"]
    assert await store.get_report(INTERVIEW_ID) == {"status": "grading_failed", "questions": []}
    interview = await store.get_interview(INTERVIEW_ID)
    assert interview is not None and interview.status == "graded"


async def test_build_report_never_raises_and_does_not_mark_graded_without_a_saved_report(caplog):
    graded = grade_result_for(question_with(0, ["I led the migration"]))
    store = await stored_interview(TURNS, store=StrictStore(break_everything=True))
    caplog.set_level(logging.DEBUG)

    await build_report(store, FakeGrader(graded), INTERVIEW_ID)  # must not raise

    assert await store.get_report(INTERVIEW_ID) is None
    interview = await store.get_interview(INTERVIEW_ID)
    assert interview is not None and interview.status == "ended"
    assert "ConnectionError" in caplog.text
    assert "database is down" not in caplog.text


async def test_build_report_for_an_unknown_interview_does_not_raise():
    await build_report(MemoryStore(), FakeGrader(error=AssertionError("unused")), "nope")
