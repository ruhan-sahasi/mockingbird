"""Vapi's REST API: the assistant we hand it, the call-creating request, and deleting a call.

`build_assistant` makes the config for one interview call. `VapiClient.create_call` asks Vapi to
start a call over a `vapi.websocket` transport and hands back the call id and the websocket URL
the relay connects to. `VapiClient.delete_call` deletes what Vapi keeps of a call once it is over
(its transcript, messages and logs), which the 7-day retention needs (sessions.py). The Vapi
facts here come from docs/vapi-contract.md.

The call id and the websocket URL are credentials while a call is live (ruling R19): anyone who
has the id can rebuild the URL and listen in. So `VapiCall` keeps both out of its `repr`, and a
`VapiError` message holds only the HTTP status and a short reason. It never holds the request or
response body, the API key, or a URL, and it does not chain the HTTP library's error, because the
traceback would print that. The assistant also holds our two path secrets, so do not log it.
"""

import re
from dataclasses import dataclass, field
from urllib.parse import quote

import httpx

VAPI_API_BASE = "https://api.vapi.ai"

# Starting an interview waits on this request, so it must not hang on a stalled connection. The
# client we are given may have no timeout of its own, so each request carries this one.
CREATE_CALL_TIMEOUT = httpx.Timeout(10.0, connect=3.0)
# The hourly purge waits on each delete in turn, so a stalled one must not hold it up for long.
DELETE_CALL_TIMEOUT = CREATE_CALL_TIMEOUT

# Vapi's reason is a short sentence, or a short list of them for a validation error. Anything past
# this many characters is not a reason any more, and could be a body or a long echo of our request.
_MAX_REASON_CHARS = 200
# The reply has no size limit, and the scrubbing below runs on the event loop that carries every
# live interview. So the scrubbing works on at most this many characters. That is well above the
# 200 that survive, which leaves room for runs of spaces that collapse away before the final cut.
_MAX_SCRUBBED_CHARS = 1000
# Starts only at the front of a run of non-space characters. Without the lookbehind, a long run
# with no "://" in it was re-scanned from every character, so the cost grew with the square of the
# run length (200,000 characters took minutes). A match still takes the whole run around the "://".
_URL = re.compile(r"(?<!\S)\S*://\S*")


class VapiError(Exception):
    """Vapi refused a request or could not be reached. The message never holds a secret."""


@dataclass(frozen=True)
class VapiCall:
    id: str = field(repr=False)
    websocket_url: str = field(repr=False)


def build_assistant(opening_line: str, *, llm_url: str, webhook_url: str, cap_s: int) -> dict:
    """The assistant for one call. `llm_url` and `webhook_url` carry their secrets in the path.

    Vapi appends `/chat/completions` to `model.url` (contract, question 1), so `llm_url` is
    `<public url>/vapi/llm/<brain secret>` and Vapi calls `.../chat/completions` under it.
    """
    return {
        "firstMessage": opening_line,
        "model": {"provider": "custom-llm", "url": llm_url, "model": "mockingbird-brain"},
        "server": {"url": webhook_url},
        # Only the report comes by webhook. Live events (speech-update, user-interrupted, status
        # changes) arrive as text frames on the audio socket, in order with the audio. Task 13's
        # live run checks that the socket still carries them with this setting.
        "serverMessages": ["end-of-call-report"],
        # Our own timer ends the call at the cap. Vapi's limit is the backstop, a little later.
        "maxDurationSeconds": cap_s + 30,
        "silenceTimeoutSeconds": 30,
        "startSpeakingPlan": {"waitSeconds": 0.4},
        "stopSpeakingPlan": {"numWords": 0, "voiceSeconds": 0.2, "backoffSeconds": 1},
        # Accepted and honoured by Vapi (contract, question 5): no recording is stored.
        "artifactPlan": {"recordingEnabled": False},
        # Vapi's default end-of-call analysis sends the transcript to one more model (a summary
        # and a success evaluation), which the consent does not name, keeps the summary at Vapi
        # and bills each call for it (contract, question 5 and Surprise 6). Nothing reads it.
        # Task 13b's live run checks that Vapi accepts this and runs no analysis.
        "analysisPlan": {
            "summaryPlan": {"enabled": False},
            "successEvaluationPlan": {"enabled": False},
        },
    }


class VapiClient:
    def __init__(self, api_key: str, http: httpx.AsyncClient):
        self._api_key = api_key
        self._http = http

    async def create_call(self, assistant: dict) -> VapiCall:
        body = {
            "assistant": assistant,
            "transport": {
                "provider": "vapi.websocket",
                "audioFormat": {"format": "pcm_s16le", "container": "raw", "sampleRate": 16000},
            },
        }
        try:
            response = await self._http.post(
                f"{VAPI_API_BASE}/call",
                json=body,
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=CREATE_CALL_TIMEOUT,
                # A redirect would carry the Authorization header to another host.
                follow_redirects=False,
            )
        except httpx.HTTPError as exc:
            # Name the kind of failure (ReadTimeout, ConnectError) and drop the library's text.
            raise VapiError(f"Vapi request failed: {type(exc).__name__}") from None
        if not response.is_success:
            raise VapiError(self._failure_message(response))
        return self._parse_call(response)

    async def delete_call(self, call_id: str) -> None:
        """Delete a call's data at Vapi: its transcript, messages, logs and analysis. A call Vapi
        no longer has (404) counts as deleted. Raises VapiError, with no id, URL or key in it,
        when Vapi refuses or cannot be reached."""
        try:
            response = await self._http.delete(
                # quote(safe=""): the id is one path segment, whatever is in it.
                f"{VAPI_API_BASE}/call/{quote(call_id, safe='')}",
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=DELETE_CALL_TIMEOUT,
                follow_redirects=False,
            )
        except httpx.HTTPError as exc:
            raise VapiError(f"Vapi request failed: {type(exc).__name__}") from None
        if response.status_code == 404:
            return
        if not response.is_success:
            raise VapiError(self._failure_message(response, hide=call_id))

    def _failure_message(self, response: httpx.Response, hide: str = "") -> str:
        reason = self._short_reason(response, hide)
        status = f"Vapi returned {response.status_code}"
        return f"{status}: {reason}" if reason else status

    def _short_reason(self, response: httpx.Response, hide: str = "") -> str:
        """Vapi's `message`, cleaned of URLs, our key and `hide` (a call id the request named),
        or "" if there is no usable one."""
        try:
            message = response.json()["message"]
        except (ValueError, KeyError, TypeError):
            return ""
        if isinstance(message, list):
            message = "; ".join(m for m in message if isinstance(m, str))
        if not isinstance(message, str):
            return ""
        # The key goes first and over the whole message, so the cut below cannot leave half of it.
        if self._api_key:
            message = message.replace(self._api_key, "[key]")
        if hide:
            message = message.replace(hide, "[call]")
        message = _URL.sub("[url]", message[:_MAX_SCRUBBED_CHARS])
        return " ".join(message.split())[:_MAX_REASON_CHARS]

    @staticmethod
    def _parse_call(response: httpx.Response) -> VapiCall:
        try:
            body = response.json()
            call_id = body["id"]
            websocket_url = body["transport"]["websocketCallUrl"]
        except (ValueError, KeyError, TypeError):
            call_id = websocket_url = None
        # Both are needed: without the id we cannot hang up, without the URL we cannot connect.
        if not all(isinstance(value, str) and value for value in (call_id, websocket_url)):
            raise VapiError("Vapi's reply had no call id or websocket URL")
        return VapiCall(call_id, websocket_url)
