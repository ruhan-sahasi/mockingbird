"""Parsing of Vapi's payloads, tested against the real recordings in fixtures/vapi.

The expected values are written out by hand from the recorded files (see docs/vapi-contract.md),
not computed from them, so a parser that reads the wrong field cannot pass by agreeing with
itself.
"""

import copy
import json
from pathlib import Path

import pytest

from mockingbird.vapi_types import (
    EndOfCallReport,
    Hangup,
    Ignored,
    LlmRequest,
    SpeechUpdate,
    StatusUpdate,
    UserInterrupted,
    parse_llm_request,
    parse_server_message,
    parse_ws_event,
)

FIXTURES = Path(__file__).parent / "fixtures" / "vapi"

# The id of the second recorded call. That call has ended, which is the only reason it may sit in
# the repo (docs/vapi-contract.md, Surprise 5).
CALL_ID = "01a15690-48f3-7331-a348-64c2e5c04886"

OPENING_LINE = "Hi. This is a Mockingbird test call. Tell me about a project you led."
SPOKEN_BEFORE_INTERRUPT = "Request for Thanks. That is"
SENTENCE_1 = (
    "At my last internship, I led a migration of our billing service"
    " from 1 big monolith to a few smaller services."
)
FULL_ANSWER = (
    f"{SENTENCE_1} I wrote the plan. Split the work between 3 engineers."
    " And we shipped it 2 weeks early with no downtime."
)


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def load_ws_frames() -> list[dict]:
    """The audio socket's text frames. Each row is {t, json}: time since connect, then the frame."""
    rows = (FIXTURES / "ws_text_frames.jsonl").read_text().splitlines()
    return [json.loads(row)["json"] for row in rows if row.strip()]


def llm_body(*messages: tuple[str, str]) -> dict:
    """A minimal custom-LLM request body: just the fields the parser reads."""
    return {
        "call": {"id": CALL_ID},
        "messages": [{"role": role, "content": content} for role, content in messages],
    }


# ---- LLM requests: one test per recorded request ------------------------------------------------


@pytest.mark.parametrize(
    ("fixture", "user_text", "spoken_assistant_text"),
    [
        # The four requests Vapi made for one answer, as its transcript grew.
        ("llm_request_turn1.json", SENTENCE_1, OPENING_LINE),
        ("llm_request_n2.json", f"{SENTENCE_1} I wrote the plan.", OPENING_LINE),
        (
            "llm_request_n3.json",
            f"{SENTENCE_1} I wrote the plan. Split the work between 3 engineers.",
            OPENING_LINE,
        ),
        ("llm_request_n4.json", FULL_ANSWER, OPENING_LINE),
        # After the interruption the history holds only the words that were actually spoken.
        ("llm_request_after_interrupt.json", "Sorry to cut in.", SPOKEN_BEFORE_INTERRUPT),
        (
            "llm_request_n6.json",
            "Sorry to cut in. But I should add that the hardest part was convincing the team"
            " to pause new features while we did the migrate",
            SPOKEN_BEFORE_INTERRUPT,
        ),
    ],
)
def test_recorded_llm_requests(fixture, user_text, spoken_assistant_text):
    assert parse_llm_request(load(fixture)) == LlmRequest(
        call_id=CALL_ID, user_text=user_text, spoken_assistant_text=spoken_assistant_text
    )


def test_full_answer_is_the_n4_request_not_the_first():
    # turn1 is a partial answer: its text is a prefix of the full one in n4.
    partial = parse_llm_request(load("llm_request_turn1.json")).user_text
    full = parse_llm_request(load("llm_request_n4.json")).user_text
    assert full.startswith(partial) and full != partial


def test_user_text_is_the_last_user_message_not_a_join():
    body = llm_body(
        ("assistant", "Tell me about a project."),
        ("user", "First part."),
        ("user", "Second part."),
    )
    assert parse_llm_request(body).user_text == "Second part."


def test_spoken_assistant_text_is_the_last_assistant_message():
    body = llm_body(
        ("system", "You are an assistant."),
        ("assistant", "Opening line."),
        ("user", "An answer."),
        ("assistant", "What was spoken."),
        ("user", "Sorry to cut in."),
    )
    request = parse_llm_request(body)
    assert request.spoken_assistant_text == "What was spoken."
    assert request.user_text == "Sorry to cut in."


def test_spoken_assistant_text_is_none_without_an_assistant_message():
    request = parse_llm_request(llm_body(("system", "You are an assistant."), ("user", "Hello.")))
    assert request.spoken_assistant_text is None
    assert request.user_text == "Hello."


def test_no_user_message_gives_empty_user_text():
    # The brain has its own handling for a blank transcript, so this is not an error here.
    request = parse_llm_request(llm_body(("system", "Hi."), ("assistant", "Opening line.")))
    assert request.user_text == ""
    assert request.spoken_assistant_text == "Opening line."


@pytest.mark.parametrize("messages", [None, [], "not a list", [None, 3, "x", {"role": "user"}]])
def test_odd_messages_give_empty_text_not_a_crash(messages):
    body = {"call": {"id": CALL_ID}, "messages": messages}
    assert parse_llm_request(body) == LlmRequest(
        call_id=CALL_ID, user_text="", spoken_assistant_text=None
    )


def test_message_content_that_is_not_a_string_is_not_used():
    # The last user message wins even when its content is unusable: an older answer must not
    # be mistaken for the newest one.
    body = {
        "call": {"id": CALL_ID},
        "messages": [
            {"role": "user", "content": "An older answer."},
            {"role": "user", "content": [{"type": "text", "text": "parts"}]},
        ],
    }
    assert parse_llm_request(body).user_text == ""


@pytest.mark.parametrize(
    "call",
    [
        pytest.param({}, id="empty call object"),
        pytest.param({"id": ""}, id="empty id"),
        pytest.param({"id": None}, id="null id"),
        pytest.param({"id": 12345}, id="id not a string"),
        pytest.param(None, id="null call"),
        pytest.param("a-call-id", id="call not an object"),
    ],
)
def test_request_without_a_usable_call_id_raises(call):
    body = {"call": call, "messages": [{"role": "user", "content": "Hi."}]}
    with pytest.raises(ValueError, match="no call id"):
        parse_llm_request(body)


def test_request_with_no_call_key_raises():
    with pytest.raises(ValueError, match="no call id"):
        parse_llm_request({"messages": [{"role": "user", "content": "Hi."}]})


def test_body_that_is_not_an_object_raises_value_error():
    with pytest.raises(ValueError, match="no call id"):
        parse_llm_request([])  # type: ignore[arg-type]


def test_missing_call_id_error_names_nothing_from_the_payload():
    # Ruling R19: the call object carries capability URLs. With the id removed, the rest of the
    # call must not leak into the error either.
    body = load("llm_request_n4.json")
    del body["call"]["id"]
    with pytest.raises(ValueError) as caught:
        parse_llm_request(body)
    text = str(caught.value)
    assert text == "custom-LLM request has no call id"
    assert CALL_ID not in text and "wss://" not in text and "https://" not in text


def test_llm_request_ignores_fields_it_does_not_need():
    body = load("llm_request_n6.json")
    expected = parse_llm_request(body)
    for key in (
        "model",
        "temperature",
        "max_tokens",
        "assistant",
        "metadata",
        "timestamp",
        "stream",
    ):
        del body[key]
    body["call"] = {"id": CALL_ID}  # the rest of the call object (status, transport, monitor, ...)
    assert parse_llm_request(body) == expected


# ---- Webhooks: one test per recorded event ------------------------------------------------------


WEBHOOK_CASES = [
    ("webhook_speech_update.json", SpeechUpdate(CALL_ID, "assistant", "started")),
    (
        "webhook_speech_update_assistant_started.json",
        SpeechUpdate(CALL_ID, "assistant", "started"),
    ),
    (
        "webhook_speech_update_assistant_stopped.json",
        SpeechUpdate(CALL_ID, "assistant", "stopped"),
    ),
    ("webhook_speech_update_user_started.json", SpeechUpdate(CALL_ID, "user", "started")),
    ("webhook_speech_update_user_stopped.json", SpeechUpdate(CALL_ID, "user", "stopped")),
    ("webhook_user_interrupted.json", UserInterrupted(CALL_ID)),
    ("webhook_status_update_in-progress.json", StatusUpdate(CALL_ID, "in-progress")),
    ("webhook_status_update_ended.json", StatusUpdate(CALL_ID, "ended")),
    (
        "webhook_end_of_call_report.json",
        EndOfCallReport(CALL_ID, "assistant-ended-call-after-message-spoken"),
    ),
]


@pytest.mark.parametrize(("fixture", "expected"), WEBHOOK_CASES)
def test_recorded_webhooks(fixture, expected):
    assert parse_server_message(load(fixture)) == expected


def test_every_recorded_webhook_has_a_case():
    # If a new webhook_*.json is recorded, this fails until a case is added to WEBHOOK_CASES.
    assert {p.name for p in FIXTURES.glob("webhook_*.json")} == {name for name, _ in WEBHOOK_CASES}


def test_unknown_webhook_type_is_ignored():
    body = {"message": {"type": "conversation-update", "call": {"id": CALL_ID}}}
    assert parse_server_message(body) == Ignored(call_id=CALL_ID, type="conversation-update")


def test_unknown_webhook_type_without_a_call_is_ignored_with_no_call_id():
    assert parse_server_message({"message": {"type": "transcript"}}) == Ignored(
        call_id=None, type="transcript"
    )


def test_hangup_is_not_a_webhook_event():
    # Hangup is an audio-socket frame. As a webhook it is just another type we do not act on.
    body = {"message": {"type": "hangup", "call": {"id": CALL_ID}}}
    assert parse_server_message(body) == Ignored(call_id=CALL_ID, type="hangup")


def test_event_without_a_call_has_no_call_id():
    body = {"message": {"type": "user-interrupted"}}
    assert parse_server_message(body) == UserInterrupted(call_id=None)


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({}, id="no message"),
        pytest.param({"message": None}, id="null message"),
        pytest.param({"message": "text"}, id="message not an object"),
        pytest.param({"message": {}}, id="message without a type"),
        pytest.param({"message": {"type": 7}}, id="type not a string"),
        pytest.param([], id="body not an object"),
    ],
)
def test_malformed_webhook_envelope_is_ignored_not_an_error(body):
    assert parse_server_message(body) == Ignored(call_id=None, type="")


@pytest.mark.parametrize(
    ("message", "kind"),
    [
        ({"type": "speech-update", "status": "started"}, "speech-update"),  # no role
        ({"type": "speech-update", "role": "user"}, "speech-update"),  # no status
        ({"type": "speech-update", "role": 1, "status": "started"}, "speech-update"),
        ({"type": "status-update"}, "status-update"),  # no status
        ({"type": "end-of-call-report"}, "end-of-call-report"),  # no endedReason
    ],
)
def test_known_event_missing_a_field_is_ignored_with_its_type(message, kind):
    # A payload change must not crash the webhook route or the relay loop. The type is kept so
    # the caller can log that something unexpected arrived.
    body = {"message": {**message, "call": {"id": CALL_ID}}}
    assert parse_server_message(body) == Ignored(call_id=CALL_ID, type=kind)


def test_webhook_ignores_fields_it_does_not_need():
    # The recorded reports carry big artifact, analysis and call objects. Dropping them must not
    # change the result, which shows the parser does not depend on them.
    for name in FIXTURES.glob("webhook_*.json"):
        body = json.loads(name.read_text())
        expected = parse_server_message(body)
        slim = copy.deepcopy(body)
        message = slim["message"]
        message["call"] = {"id": message["call"]["id"]}
        for key in list(message):
            if key not in {"type", "call", "role", "status", "endedReason"}:
                del message[key]
        assert parse_server_message(slim) == expected, name.name


# ---- Audio-socket text frames -------------------------------------------------------------------

# The nine frame types in the recording, with how many of each. If a re-recording adds or drops a
# type, test_ws_recording_has_the_documented_frame_types says so.
WS_TYPE_COUNTS = {
    "assistant.started": 1,
    "status-update": 2,
    "speech-update": 10,
    "transcript": 35,
    "conversation-update": 14,
    "model-output": 103,
    "voice-input": 103,
    "user-interrupted": 1,
    "hangup": 1,
}
WS_IGNORED_TYPES = [
    "assistant.started",
    "transcript",
    "conversation-update",
    "model-output",
    "voice-input",
]


def ws_frames_of(kind: str) -> list[dict]:
    return [frame for frame in load_ws_frames() if frame["type"] == kind]


def test_ws_recording_has_the_documented_frame_types():
    counts: dict[str, int] = {}
    for frame in load_ws_frames():
        counts[frame["type"]] = counts.get(frame["type"], 0) + 1
    assert counts == WS_TYPE_COUNTS


def test_ws_speech_update_frames_in_recorded_order():
    # Opening line, answer 1, reply (interrupted), answer 2, last reply.
    events = [parse_ws_event(frame) for frame in ws_frames_of("speech-update")]
    assert events == [
        SpeechUpdate(None, "assistant", "started"),
        SpeechUpdate(None, "assistant", "stopped"),
        SpeechUpdate(None, "user", "started"),
        SpeechUpdate(None, "user", "stopped"),
        SpeechUpdate(None, "assistant", "started"),
        SpeechUpdate(None, "assistant", "stopped"),
        SpeechUpdate(None, "user", "started"),
        SpeechUpdate(None, "user", "stopped"),
        SpeechUpdate(None, "assistant", "started"),
        SpeechUpdate(None, "assistant", "stopped"),
    ]


def test_ws_status_update_frames():
    events = [parse_ws_event(frame) for frame in ws_frames_of("status-update")]
    assert events == [StatusUpdate(None, "in-progress"), StatusUpdate(None, "ended")]


def test_ws_user_interrupted_frame():
    assert [parse_ws_event(f) for f in ws_frames_of("user-interrupted")] == [UserInterrupted(None)]


def test_ws_hangup_frame():
    assert [parse_ws_event(f) for f in ws_frames_of("hangup")] == [Hangup(None)]


@pytest.mark.parametrize("kind", WS_IGNORED_TYPES)
def test_ws_frames_we_do_not_act_on_are_ignored(kind):
    frames = ws_frames_of(kind)
    assert len(frames) == WS_TYPE_COUNTS[kind]
    assert all(parse_ws_event(frame) == Ignored(call_id=None, type=kind) for frame in frames)


def test_every_recorded_ws_frame_parses_and_none_carries_a_call_id():
    frames = load_ws_frames()
    assert len(frames) == sum(WS_TYPE_COUNTS.values())
    assert all(parse_ws_event(frame).call_id is None for frame in frames)


def test_ws_frame_with_an_unknown_type_is_ignored():
    assert parse_ws_event({"type": "something-new", "x": 1}) == Ignored(None, "something-new")


@pytest.mark.parametrize("frame", [{}, {"type": None}, {"type": 3}, [], None])
def test_malformed_ws_frame_is_ignored_not_an_error(frame):
    assert parse_ws_event(frame) == Ignored(call_id=None, type="")  # type: ignore[arg-type]


def test_ws_event_missing_a_field_is_ignored_with_its_type():
    assert parse_ws_event({"type": "speech-update", "role": "user"}) == Ignored(
        None, "speech-update"
    )
    assert parse_ws_event({"type": "status-update"}) == Ignored(None, "status-update")


def test_ws_end_of_call_report_is_not_a_socket_event():
    # The report is a webhook. If it ever shows up as a frame it is not one parse_ws_event acts on.
    frame = {"type": "end-of-call-report", "endedReason": "x"}
    assert parse_ws_event(frame) == Ignored(None, "end-of-call-report")


# ---- Call id as a credential (ruling R19) -------------------------------------------------------


def test_repr_of_every_parsed_event_hides_the_call_id():
    # A stray `log.info("%r", event)` must not leak the id of a live call.
    parsed = [parse_llm_request(load("llm_request_n4.json"))]
    parsed += [
        parse_server_message(json.loads(p.read_text())) for p in FIXTURES.glob("webhook_*.json")
    ]
    parsed += [Ignored(CALL_ID, "x"), Hangup(CALL_ID)]
    assert all(event.call_id == CALL_ID for event in parsed)
    assert all(CALL_ID not in repr(event) for event in parsed)


def test_events_are_frozen():
    event = parse_server_message(load("webhook_user_interrupted.json"))
    with pytest.raises(AttributeError):
        event.call_id = "other"  # type: ignore[misc]
