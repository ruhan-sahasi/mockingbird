"""Who may start a demo: one per visitor per UTC day, within a daily budget of demo seconds.

A visitor is known two ways, so that clearing cookies or changing networks alone does not buy a
second demo: an anonymous `mb_visitor` cookie, and the IP address. Neither is stored as it is.
Both are hashed with a salt from the settings, so the database never holds an IP address.

A start that never became a call (its socket was never opened, or Vapi refused it) is abandoned
and does not count as the visitor's demo, so an honest retry works. But a start holds a cap of
the day's budget until it is abandoned, a minute or two later, so free retries would let one
address hold the budget all day with one request every two minutes. So every start counts toward
a second limit, MAX_ATTEMPTS_PER_DAY, abandoned or not.

The owner key skips all of this. That rule lives in the start route, not here.
"""

import hashlib
import uuid
from typing import Literal

from starlette.requests import Request

from mockingbird.store import Store

VISITOR_COOKIE = "mb_visitor"
COOKIE_MAX_AGE_S = 365 * 24 * 3600  # one year
# CloudFront sets this to "<viewer ip>:<viewer port>". The ALB only takes traffic from
# CloudFront, so it is the visitor's real address; request.client is CloudFront's.
VIEWER_ADDRESS_HEADER = "cloudfront-viewer-address"

# Starts of any kind per visitor or IP per UTC day: the demo plus two that never became a call.
MAX_ATTEMPTS_PER_DAY = 3

StartCheck = Literal["ok", "visitor_limit", "daily_budget"]


async def check_can_start(
    store: Store, *, visitor_hash: str, ip_hash: str, cap_s: int, daily_demo_s: int
) -> StartCheck:
    """Whether a demo of `cap_s` seconds may start now. The visitor limit is checked first, so
    a visitor who has had their demo hears that, not that the budget ran out."""
    if await store.starts_today(visitor_hash, ip_hash) >= 1:
        return "visitor_limit"
    if await store.attempts_today(visitor_hash, ip_hash) >= MAX_ATTEMPTS_PER_DAY:
        return "visitor_limit"
    if await store.seconds_used_today() + cap_s > daily_demo_s:
        return "daily_budget"
    return "ok"


def visitor_id(request: Request) -> tuple[str, str, str | None]:
    """The salted hashes of the visitor's cookie and IP, and a Set-Cookie header value to send
    when the visitor has no valid cookie yet (None when they have one)."""
    salt = request.app.state.settings.visitor_salt
    cookie = request.cookies.get(VISITOR_COOKIE)
    set_cookie = None
    if not _is_uuid(cookie):
        cookie = str(uuid.uuid4())
        set_cookie = (
            f"{VISITOR_COOKIE}={cookie}; Max-Age={COOKIE_MAX_AGE_S}; Path=/;"
            " HttpOnly; Secure; SameSite=Lax"
        )
    return _salted(salt, cookie), _salted(salt, _client_ip(request)), set_cookie


def _client_ip(request: Request) -> str:
    viewer = request.headers.get(VIEWER_ADDRESS_HEADER, "").strip()
    if viewer:
        # The port follows the last colon. An IPv6 address has colons of its own, and no brackets.
        address, colon, _port = viewer.rpartition(":")
        return address if colon else viewer
    return request.client.host if request.client is not None else ""


def _is_uuid(value: str | None) -> bool:
    if not value:
        return False
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


def _salted(salt: str, value: str) -> str:
    return hashlib.sha256((salt + value).encode()).hexdigest()
