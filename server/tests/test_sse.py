import json

import pytest

from mockingbird.sse import SSE_DONE, sse_chunk


def test_chunk_shape():
    line = sse_chunk("Hi", chunk_id="c1", created=1, first=True)
    assert line.startswith("data: ") and line.endswith("\n\n")
    body = json.loads(line[6:])
    assert body["id"] == "c1" and body["created"] == 1
    assert body["object"] == "chat.completion.chunk" and body["model"] == "mockingbird-brain"
    assert body["choices"][0]["delta"] == {"role": "assistant", "content": "Hi"}
    assert body["choices"][0]["finish_reason"] is None


def test_later_chunk_carries_only_content():
    body = json.loads(sse_chunk("there", chunk_id="c1", created=1)[6:])
    assert body["choices"][0]["delta"] == {"content": "there"}
    assert body["choices"][0]["finish_reason"] is None


def test_finish_chunk():
    body = json.loads(sse_chunk("", chunk_id="c1", created=1, finish=True)[6:])
    assert body["choices"][0]["finish_reason"] == "stop" and body["choices"][0]["delta"] == {}


def test_finish_chunk_refuses_text_instead_of_dropping_it():
    # The finish chunk has an empty delta, so text sent with finish would vanish from the
    # stream. Raising keeps the last words of a reply from being lost without an error.
    with pytest.raises(ValueError, match="finish=True"):
        sse_chunk("Thanks, that's all.", chunk_id="c1", created=1, finish=True)


def test_non_ascii_text_round_trips_unescaped():
    text = "I’m fine, café “done”"
    line = sse_chunk(text, chunk_id="c1", created=1)
    assert json.loads(line[6:])["choices"][0]["delta"]["content"] == text
    # ensure_ascii=False: the curly quote is written as itself, not as a \u escape.
    assert "’" in line and "\\u2019" not in line


def test_json_is_compact():
    payload = sse_chunk("Hi", chunk_id="c1", created=1, first=True)[6:-2]
    assert payload == json.dumps(
        json.loads(payload), separators=(",", ":"), ensure_ascii=False
    )


def test_done_line():
    assert SSE_DONE == "data: [DONE]\n\n"
