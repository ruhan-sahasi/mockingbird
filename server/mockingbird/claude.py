"""Talking to Claude Haiku for the live interviewer lines.

The brain only knows the `ClaudeStreamer` protocol, so tests can drive it with a scripted fake.
`AnthropicStreamer` is the real one. It turns every way the call can go wrong (an API error, a
timeout, a dropped connection, a refusal) into a single `BrainError`, which the brain answers
with a fixed line instead of dead air.

The SDK only wraps errors that happen while it sends the request. Once the response headers are
in, a read timeout or a dropped connection comes out of the stream as the HTTP library's own
exception, not an `anthropic` one. So the streamer catches every `Exception` from the call, not
just the SDK's error classes. `CancelledError` and `GeneratorExit` are not `Exception`s, so
cancelling a reply or closing the stream early still works.
"""

from collections.abc import AsyncIterator
from typing import Protocol

import anthropic

from mockingbird.failures import describe

MODEL = "claude-haiku-5-5"
MAX_TOKENS = 200  # two spoken sentences are far shorter than this; it only caps a runaway reply


class BrainError(Exception):
    """Claude could not give a usable line for this turn."""


class ClaudeStreamer(Protocol):
    def stream(self, *, system: list[dict], messages: list[dict]) -> AsyncIterator[str]:
        """Yield the reply as text chunks. Raise BrainError if Claude fails or refuses."""
        ...


def make_anthropic_client(api_key: str) -> anthropic.AsyncAnthropic:
    # A live phone-style call cannot wait. Give up on connecting after 2 s and on a silent
    # stream after 10 s, and retry once at most, so a bad turn costs seconds and not a minute.
    return anthropic.AsyncAnthropic(
        api_key=api_key,
        timeout=anthropic.Timeout(10.0, connect=2.0),
        max_retries=1,
    )


class AnthropicStreamer:
    def __init__(self, client: anthropic.AsyncAnthropic):
        self._client = client

    async def stream(self, *, system: list[dict], messages: list[dict]) -> AsyncIterator[str]:
        try:
            async with self._client.messages.stream(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                # Thinking off and low effort keep the first word fast. Spoken lines need that.
                thinking={"type": "disabled"},
                output_config={"effort": "low"},
                system=system,
                messages=messages,
            ) as stream:
                async for text in stream.text_stream:
                    yield text
                final = await stream.get_final_message()
        except Exception as exc:
            # Opening the request, reading the stream and reading the final message all land
            # here: an API error, a timeout, a dropped connection, or a malformed event.
            # The type, and for an API error its status, error type and request id (failures.py).
            raise BrainError(f"Claude request failed: {describe(exc)}") from exc
        # Haiku 5.5 has no server-side refusal fallback, so a refusal reaches us as a normal
        # end of stream with this stop reason. Treat it like any other failure.
        if final.stop_reason == "refusal":
            raise BrainError("Claude refused to answer")
