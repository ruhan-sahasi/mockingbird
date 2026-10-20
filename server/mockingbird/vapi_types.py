"""Typed views of what Vapi sends us, parsed from plain dicts. No I/O, no logging.

Three kinds of payload come in (docs/vapi-contract.md has the recorded shapes):
- custom-LLM requests: Vapi asks the brain for the next interviewer line (`parse_llm_request`);
- webhooks: `{"message": {"type": ..., "call": {"id": ...}, ...}}` (`parse_server_message`);
- text frames on the audio WebSocket: flat `{"type": ..., ...}` with no call id (`parse_ws_event`).

Each parser reads only the fields it needs, by plain dict access, so a change anywhere else in
Vapi's payloads cannot break it. Anything it does not recognise becomes `Ignored`, and so does a
known type that lacks a field we need: a payload change must not crash the webhook route or the
relay loop, and the type is kept so the caller can note what arrived.

The call id is a credential while a call is live (ruling R19): the call's capability URLs are
built from it. So the parsers never put the id, a URL or any other payload text in an error
message, and every event hides `call_id` from its `repr` so a stray `log.info("%r", event)`
cannot leak it. Do not log payload bodies.
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class LlmRequest:
    """What the brain needs from one custom-LLM request."""

    call_id: str = field(repr=False)
    # The candidate's whole answer so far this turn, from the last `user` message. Vapi sends
    # several requests per answer and each one holds the transcript so far, not a delta.
    user_text: str
    # The last `assistant` message in the history, or None if there is none. Vapi rebuilds it from
    # the words it actually spoke, so after an interruption it is shorter than what we generated.
    spoken_assistant_text: str | None


@dataclass(frozen=True)
class _CallEvent:
    # None for audio-socket frames, which do not carry the call id, and for a webhook without one.
    call_id: str | None = field(repr=False)


@dataclass(frozen=True)
class SpeechUpdate(_CallEvent):
    role: str  # "assistant" or "user"
    status: str  # "started" or "stopped"


@dataclass(frozen=True)
class UserInterrupted(_CallEvent):
    pass


@dataclass(frozen=True)
class StatusUpdate(_CallEvent):
    status: str  # for example "in-progress" or "ended"


@dataclass(frozen=True)
class Hangup(_CallEvent):
    """The audio socket's `hangup` frame, which arrives right after status-update "ended"."""


@dataclass(frozen=True)
class EndOfCallReport(_CallEvent):
    ended_reason: str


@dataclass(frozen=True)
class Ignored(_CallEvent):
    type: str  # the payload's `type`, or "" if it had none


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _type_of(payload: dict) -> str:
    return _str(payload.get("type")) or ""


def _last_text(messages: Any, role: str) -> str | None:
    """The content of the last message with this role, or None if there is none or it is not text.

    The last message with the role wins even when its content is unusable, so an older answer is
    never mistaken for the newest one.
    """
    if not isinstance(messages, list):
        return None
    for message in reversed(messages):
        if isinstance(message, dict) and message.get("role") == role:
            return _str(message.get("content"))
    return None


def parse_llm_request(body: dict) -> LlmRequest:
    """Read the call id, the candidate's latest text and the last spoken interviewer text.

    Raises ValueError if the body has no call id. The message says only that, never what the body
    did contain.
    """
    call_id = _str(_dict(_dict(body).get("call")).get("id"))
    if not call_id:
        raise ValueError("custom-LLM request has no call id")
    messages = _dict(body).get("messages")
    return LlmRequest(
        call_id=call_id,
        user_text=_last_text(messages, "user") or "",
        spoken_assistant_text=_last_text(messages, "assistant"),
    )


def _shared_event(
    payload: dict, call_id: str | None
) -> SpeechUpdate | UserInterrupted | StatusUpdate | None:
    """The three event types that look the same as a webhook message and as a socket frame."""
    kind = _type_of(payload)
    if kind == "speech-update":
        role, status = _str(payload.get("role")), _str(payload.get("status"))
        if role is not None and status is not None:
            return SpeechUpdate(call_id, role, status)
    elif kind == "user-interrupted":
        return UserInterrupted(call_id)
    elif kind == "status-update":
        status = _str(payload.get("status"))
        if status is not None:
            return StatusUpdate(call_id, status)
    return None


def parse_server_message(
    body: dict,
) -> SpeechUpdate | UserInterrupted | StatusUpdate | EndOfCallReport | Ignored:
    """Parse a webhook body, `{"message": {"type": ..., "call": {"id": ...}, ...}}`."""
    message = _dict(_dict(body).get("message"))
    call_id = _str(_dict(message.get("call")).get("id")) or None
    event = _shared_event(message, call_id)
    if event is not None:
        return event
    if _type_of(message) == "end-of-call-report":
        ended_reason = _str(message.get("endedReason"))
        if ended_reason is not None:
            return EndOfCallReport(call_id, ended_reason)
    return Ignored(call_id, _type_of(message))


def parse_ws_event(
    frame: dict,
) -> SpeechUpdate | UserInterrupted | StatusUpdate | Hangup | Ignored:
    """Parse one text frame from Vapi's audio WebSocket. Frames carry no call id, so it is None."""
    frame = _dict(frame)
    event = _shared_event(frame, None)
    if event is not None:
        return event
    if _type_of(frame) == "hangup":
        return Hangup(None)
    return Ignored(None, _type_of(frame))
