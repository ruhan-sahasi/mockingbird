"""OpenAI chat-completion SSE encoding for the brain's replies.

Vapi reads our custom-LLM endpoint as an OpenAI stream: one `data:` line per chunk, each line
ended by a blank line, and `data: [DONE]` at the end. This module only builds those strings.
It does no I/O.

json.dumps escapes newlines and carriage returns, so each event stays on one `data:` line even
when the spoken text contains them.
"""

import json

MODEL_NAME = "mockingbird-brain"
SSE_DONE = "data: [DONE]\n\n"


def sse_chunk(
    text: str,
    *,
    chunk_id: str,
    created: int,
    first: bool = False,
    finish: bool = False,
) -> str:
    """Return one `data:` event for a chat.completion.chunk.

    The first chunk carries role "assistant" with its text. Later chunks carry only content.
    The finish chunk has an empty delta and finish_reason "stop", so `text` is ignored when
    `finish` is true.
    """
    if finish:
        delta: dict[str, str] = {}
        finish_reason: str | None = "stop"
    else:
        delta = {"role": "assistant", "content": text} if first else {"content": text}
        finish_reason = None
    body = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": MODEL_NAME,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    # Compact separators and ensure_ascii=False keep curly quotes and accents as the characters
    # themselves, so the spoken text is not padded with \u escapes.
    payload = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
    return f"data: {payload}\n\n"
