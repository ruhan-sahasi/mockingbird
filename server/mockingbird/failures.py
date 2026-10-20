"""What a log line may say about an exception.

Most failures are logged by their type alone, because a library's message can hold a
credential (the Vapi call id or a socket URL, ruling R19) or the candidate's words. That hides
what went wrong on a first live run, so two kinds of error say more, as their details are known
to be safe:
- a VapiError: its message is the HTTP status and Vapi's reason, already cleaned of URLs, the key
  and the call id (vapi_client.py);
- an Anthropic API error with an HTTP status: the status, the error type the API names (such as
  overloaded_error) and the request id, which Anthropic's support can look up. Its free text is
  left out.
"""

import re

import anthropic

from mockingbird.vapi_client import VapiError

# The API's error types are short snake_case names. Anything else is not repeated.
_ERROR_TYPE = re.compile(r"[a-z_]{1,64}")
_REQUEST_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")


def describe(exc: BaseException) -> str:
    """The exception's type, with the safe details of the kinds above."""
    name = type(exc).__name__
    if isinstance(exc, VapiError):
        return f"{name}: {exc}"
    if isinstance(exc, anthropic.APIStatusError):
        details = [f"HTTP {exc.status_code}"]
        if isinstance(exc.type, str) and _ERROR_TYPE.fullmatch(exc.type):
            details.append(exc.type)
        if isinstance(exc.request_id, str) and _REQUEST_ID.fullmatch(exc.request_id):
            details.append(f"request {exc.request_id}")
        return f"{name} ({', '.join(details)})"
    return name
