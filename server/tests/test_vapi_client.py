"""The Vapi REST client, tested with respx standing in for api.vapi.ai.

The create-call response is the real recording (fixtures/vapi/create_call_response.json). Expected
ids and URLs are written out by hand from it, not read back from it, so a client that picked the
wrong field cannot pass by agreeing with itself.

The call id and websocket URL are credentials while a call is live (docs/vapi-contract.md,
Surprise 5, and ruling R19), so most of the failure tests check that nothing secret reaches an
error message.
"""

import json
import traceback
from pathlib import Path

import httpx
import pytest

from mockingbird.vapi_client import VapiCall, VapiClient, VapiError, build_assistant

FIXTURES = Path(__file__).parent / "fixtures" / "vapi"

API_KEY = "vapi-key-0123456789abcdef"
CREATE_CALL_URL = "https://api.vapi.ai/call"

# From create_call_response.json (the second recorded call, which has ended).
CALL_ID = "01a15690-48f3-7331-a348-64c2e5c04886"
WEBSOCKET_URL = "wss://phone-call-websocket.aws-us-west-2-backend-production1.vapi.ai/REDACTED"

LLM_URL = "https://mockingbird.test/vapi/llm/brain-secret"
WEBHOOK_URL = "https://mockingbird.test/vapi/webhook/webhook-secret"
OPENING_LINE = "Hi, I'm your interviewer today. Tell me about a project you led."


def create_call_response() -> dict:
    return json.loads((FIXTURES / "create_call_response.json").read_text())


@pytest.fixture
async def http():
    async with httpx.AsyncClient() as client:
        yield client


@pytest.fixture
def vapi(http: httpx.AsyncClient) -> VapiClient:
    return VapiClient(API_KEY, http)


def assistant() -> dict:
    return build_assistant(OPENING_LINE, llm_url=LLM_URL, webhook_url=WEBHOOK_URL, cap_s=300)


def test_build_assistant_exact():
    assert assistant() == {
        "firstMessage": OPENING_LINE,
        "model": {"provider": "custom-llm", "url": LLM_URL, "model": "mockingbird-brain"},
        "server": {"url": WEBHOOK_URL},
        # Webhooks carry only the end-of-call report. Live events come from the audio socket.
        "serverMessages": ["end-of-call-report"],
        # The cap plus 30 s, so our own timer ends the call first and Vapi's is the backstop.
        "maxDurationSeconds": 330,
        "silenceTimeoutSeconds": 30,
        "startSpeakingPlan": {"waitSeconds": 0.4},
        "stopSpeakingPlan": {"numWords": 0, "voiceSeconds": 0.2, "backoffSeconds": 1},
        "artifactPlan": {"recordingEnabled": False},
    }


def test_build_assistant_max_duration_follows_the_cap():
    full = build_assistant(OPENING_LINE, llm_url=LLM_URL, webhook_url=WEBHOOK_URL, cap_s=1200)

    assert full["maxDurationSeconds"] == 1230


async def test_create_call_parses_id_and_ws_url(vapi: VapiClient, respx_mock):
    route = respx_mock.post(CREATE_CALL_URL).respond(201, json=create_call_response())

    call = await vapi.create_call(assistant())

    assert call.id == CALL_ID
    assert call.websocket_url == WEBSOCKET_URL
    request = route.calls.last.request
    assert request.headers["authorization"] == f"Bearer {API_KEY}"
    assert json.loads(request.content) == {
        "assistant": assistant(),
        "transport": {
            "provider": "vapi.websocket",
            "audioFormat": {"format": "pcm_s16le", "container": "raw", "sampleRate": 16000},
        },
    }


async def test_create_call_has_its_own_timeout(respx_mock):
    """The injected client could have no timeout at all, and starting an interview must not hang."""
    route = respx_mock.post(CREATE_CALL_URL).respond(201, json=create_call_response())

    async with httpx.AsyncClient(timeout=None) as client_without_timeout:
        await VapiClient(API_KEY, client_without_timeout).create_call(assistant())

    timeout = route.calls.last.request.extensions["timeout"]
    assert all(seconds is not None and seconds <= 15 for seconds in timeout.values())


async def test_create_call_raises_on_400(vapi: VapiClient, respx_mock):
    body = {
        "message": ["assistant.maxDurationSeconds must not be less than 10"],
        "error": "Bad Request",
        "statusCode": 400,
    }
    respx_mock.post(CREATE_CALL_URL).respond(400, json=body)

    with pytest.raises(VapiError) as excinfo:
        await vapi.create_call(assistant())

    assert str(excinfo.value) == (
        "Vapi returned 400: assistant.maxDurationSeconds must not be less than 10"
    )


async def test_create_call_raises_on_a_server_error_with_no_json(vapi: VapiClient, respx_mock):
    respx_mock.post(CREATE_CALL_URL).respond(502, text="<html>Bad gateway</html>")

    with pytest.raises(VapiError) as excinfo:
        await vapi.create_call(assistant())

    assert str(excinfo.value) == "Vapi returned 502"


@pytest.mark.parametrize(
    "reply",
    [
        {},
        [],
        {"id": CALL_ID},
        {"id": CALL_ID, "transport": {}},
        {"id": CALL_ID, "transport": {"websocketCallUrl": ""}},
        {"id": CALL_ID, "transport": {"websocketCallUrl": None}},
        {"id": "", "transport": {"websocketCallUrl": WEBSOCKET_URL}},
        {"id": 12, "transport": {"websocketCallUrl": WEBSOCKET_URL}},
    ],
)
async def test_create_call_rejects_a_reply_without_an_id_and_a_url(
    reply, vapi: VapiClient, respx_mock
):
    """Without the id we cannot hang up, and without the URL we cannot connect."""
    respx_mock.post(CREATE_CALL_URL).respond(201, json=reply)

    with pytest.raises(VapiError, match="no call id or websocket URL"):
        await vapi.create_call(assistant())


async def test_a_long_reason_is_cut_short(vapi: VapiClient, respx_mock):
    respx_mock.post(CREATE_CALL_URL).respond(400, json={"message": "x" * 5000})

    with pytest.raises(VapiError) as excinfo:
        await vapi.create_call(assistant())

    assert len(str(excinfo.value)) < 250


async def test_create_call_does_not_follow_a_redirect(respx_mock):
    """A 3xx is not success, and following it would send the Authorization header elsewhere.

    The injected client may be set to follow redirects, so the request itself must refuse to.
    """
    respx_mock.post(CREATE_CALL_URL).respond(307, headers={"location": "https://elsewhere.test/"})
    elsewhere = respx_mock.post("https://elsewhere.test/").respond(201, json=create_call_response())

    async with httpx.AsyncClient(follow_redirects=True) as following_client:
        with pytest.raises(VapiError, match="307"):
            await VapiClient(API_KEY, following_client).create_call(assistant())

    assert not elsewhere.called


def test_vapi_call_repr_hides_the_id_and_the_websocket_url():
    text = repr(VapiCall(CALL_ID, WEBSOCKET_URL))

    assert CALL_ID not in text
    assert "wss://" not in text
    assert "vapi.ai" not in text


# Each case is a way create_call can fail while Vapi's answer or our request holds a secret. The
# error must name the problem and nothing else.
LEAKY_FAILURES = {
    "400 whose message echoes the url and the key": lambda respx_mock: respx_mock.post(
        CREATE_CALL_URL
    ).respond(
        400,
        json={
            "message": f"bad model.url {LLM_URL}; sent Bearer {API_KEY} from {WEBSOCKET_URL}",
            "request": assistant(),
        },
    ),
    "400 whose message is a list that echoes the url": lambda respx_mock: respx_mock.post(
        CREATE_CALL_URL
    ).respond(400, json={"message": [f"{LLM_URL} is unreachable", f"key {API_KEY}"]}),
    "502 whose body is text holding a websocket url": lambda respx_mock: respx_mock.post(
        CREATE_CALL_URL
    ).respond(502, text=f"upstream {WEBSOCKET_URL} failed for {CALL_ID}"),
    "201 that has a websocket url but no id": lambda respx_mock: respx_mock.post(
        CREATE_CALL_URL
    ).respond(201, json={"transport": {"websocketCallUrl": WEBSOCKET_URL}}),
    "201 whose body is not json": lambda respx_mock: respx_mock.post(CREATE_CALL_URL).respond(
        201, text=f"{WEBSOCKET_URL} {CALL_ID}"
    ),
    "a network timeout": lambda respx_mock: respx_mock.post(CREATE_CALL_URL).mock(
        side_effect=httpx.ReadTimeout(f"timed out talking to {CREATE_CALL_URL} with {API_KEY}")
    ),
}


@pytest.mark.parametrize("arrange", LEAKY_FAILURES.values(), ids=LEAKY_FAILURES.keys())
async def test_vapi_error_carries_no_url_key_or_id(arrange, vapi: VapiClient, respx_mock):
    arrange(respx_mock)

    with pytest.raises(VapiError) as excinfo:
        await vapi.create_call(assistant())

    error = excinfo.value
    # str, repr, and the printed traceback are the three ways an error reaches a log.
    for text in (str(error), repr(error), "".join(traceback.format_exception(error))):
        assert "://" not in text
        assert "wss" not in text
        assert API_KEY not in text
        assert CALL_ID not in text
        assert "brain-secret" not in text


async def test_error_says_what_failed_for_a_network_timeout(vapi: VapiClient, respx_mock):
    respx_mock.post(CREATE_CALL_URL).mock(side_effect=httpx.ReadTimeout("timed out"))

    with pytest.raises(VapiError, match="ReadTimeout"):
        await vapi.create_call(assistant())
