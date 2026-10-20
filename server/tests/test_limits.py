"""The demo limits: one demo per visitor per UTC day, and a daily budget of demo seconds.

`check_can_start` is tested against MemoryStore and `visitor_id` against plain Starlette requests.
The owner key is a rule of the start route, so those tests go through POST /api/interviews, with
the IP set through the CloudFront-Viewer-Address header so that each request can pick its own.
"""

import hashlib
import uuid
from contextlib import ExitStack
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from mockingbird.app import create_app
from mockingbird.limits import check_can_start, visitor_id
from mockingbird.models import Interview
from mockingbird.store import MemoryStore

SALT = "test-visitor-salt"  # the salt in conftest's test_settings
OWNER_KEY = "test-owner-key"


def interview(id: str, *, visitor_hash: str, ip_hash: str, status: str = "created") -> Interview:
    return Interview(
        id=id,
        mode="demo",
        status=status,
        visitor_hash=visitor_hash,
        ip_hash=ip_hash,
        cap_s=300,
        questions=["Tell me about a time you led a team."],
        created_at=datetime.now(UTC),
    )


async def can_start(store: MemoryStore, visitor: str, ip: str, *, daily_demo_s: int = 3600):
    return await check_can_start(
        store, visitor_hash=visitor, ip_hash=ip, cap_s=300, daily_demo_s=daily_demo_s
    )


# --- check_can_start ---------------------------------------------------------------------------


async def test_a_first_start_with_budget_left_is_ok():
    assert await can_start(MemoryStore(), "visitor-a", "ip-a") == "ok"


async def test_a_second_start_the_same_day_with_the_same_cookie_is_a_visitor_limit():
    store = MemoryStore()
    await store.create_interview(interview("first", visitor_hash="visitor-a", ip_hash="ip-a"))
    # Same cookie, different network: still the same visitor.
    assert await can_start(store, "visitor-a", "ip-b") == "visitor_limit"


async def test_a_new_cookie_from_the_same_ip_is_a_visitor_limit():
    store = MemoryStore()
    await store.create_interview(interview("first", visitor_hash="visitor-a", ip_hash="ip-a"))
    # Clearing cookies does not buy a second demo from the same address.
    assert await can_start(store, "visitor-b", "ip-a") == "visitor_limit"


async def test_a_600_s_budget_with_one_live_300_s_demo_allows_one_more_start_then_runs_out():
    store = MemoryStore()
    await store.create_interview(
        interview("a", visitor_hash="visitor-a", ip_hash="ip-a", status="live")
    )
    # 300 used + 300 for the new one = 600, which is not over a 600 s budget.
    assert await can_start(store, "visitor-b", "ip-b", daily_demo_s=600) == "ok"

    await store.create_interview(
        interview("b", visitor_hash="visitor-b", ip_hash="ip-b", status="live")
    )
    assert await can_start(store, "visitor-c", "ip-c", daily_demo_s=600) == "daily_budget"


async def test_the_visitor_limit_is_reported_before_the_daily_budget():
    store = MemoryStore()
    await store.create_interview(
        interview("a", visitor_hash="visitor-a", ip_hash="ip-a", status="live")
    )
    assert await can_start(store, "visitor-a", "ip-a", daily_demo_s=0) == "visitor_limit"


# --- visitor_id --------------------------------------------------------------------------------


def make_request(*, headers: list[tuple[str, str]] = (), client=("203.0.113.9", 5000)) -> Request:
    settings = SimpleNamespace(visitor_salt=SALT)
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/interviews",
        "headers": [(name.lower().encode(), value.encode()) for name, value in headers],
        "client": client,
        "app": SimpleNamespace(state=SimpleNamespace(settings=settings)),
    }
    return Request(scope)


def salted(value: str) -> str:
    return hashlib.sha256((SALT + value).encode()).hexdigest()


def test_a_new_visitor_gets_a_secure_one_year_cookie_holding_a_uuid4():
    visitor_hash, ip_hash, set_cookie = visitor_id(make_request())

    name, _, rest = set_cookie.partition("=")
    value, *attributes = [part.strip() for part in rest.split(";")]
    assert name == "mb_visitor"
    assert uuid.UUID(value).version == 4
    assert set(attributes) == {
        "Max-Age=31536000",
        "Path=/",
        "HttpOnly",
        "Secure",
        "SameSite=Lax",
    }
    assert visitor_hash == salted(value)
    assert ip_hash == salted("203.0.113.9")


def test_a_returning_visitor_keeps_their_cookie_and_is_not_sent_a_new_one():
    value = str(uuid.uuid4())
    visitor_hash, _, set_cookie = visitor_id(
        make_request(headers=[("cookie", f"mb_visitor={value}")])
    )
    assert set_cookie is None
    assert visitor_hash == salted(value)


def test_a_cookie_that_is_not_a_uuid_is_replaced():
    visitor_hash, _, set_cookie = visitor_id(
        make_request(headers=[("cookie", "mb_visitor=not-a-uuid")])
    )
    assert set_cookie is not None
    assert visitor_hash != salted("not-a-uuid")


@pytest.mark.parametrize(
    "header, ip",
    [
        ("198.51.100.7:46532", "198.51.100.7"),
        ("2001:db8:85a3::8a2e:370:7334:46532", "2001:db8:85a3::8a2e:370:7334"),
    ],
)
def test_the_ip_is_the_cloudfront_viewer_address_without_its_port(header, ip):
    _, ip_hash, _ = visitor_id(make_request(headers=[("CloudFront-Viewer-Address", header)]))
    assert ip_hash == salted(ip)


def test_without_the_cloudfront_header_the_ip_is_the_client_host():
    _, ip_hash, _ = visitor_id(make_request(client=("192.0.2.44", 1234)))
    assert ip_hash == salted("192.0.2.44")


def test_neither_hash_holds_the_raw_value():
    value = str(uuid.uuid4())
    visitor_hash, ip_hash, _ = visitor_id(
        make_request(headers=[("cookie", f"mb_visitor={value}")], client=("192.0.2.44", 1))
    )
    assert value not in visitor_hash
    assert "192.0.2.44" not in ip_hash
    assert visitor_hash != hashlib.sha256(value.encode()).hexdigest()  # the salt is in it


# --- the start route ---------------------------------------------------------------------------


@pytest.fixture
def make_client(settings):
    """A TestClient on a fresh app with a MemoryStore, with any settings changed as asked."""
    with ExitStack() as stack:

        def make(**overrides) -> TestClient:
            app = create_app(settings.model_copy(update=overrides), store=MemoryStore())
            # https, so the client sends the Secure cookie back.
            return stack.enter_context(TestClient(app, base_url="https://testserver"))

        yield make


def post(client: TestClient, ip: str, **headers):
    headers["CloudFront-Viewer-Address"] = f"{ip}:40000"
    return client.post("/api/interviews", headers=headers)


def test_the_route_refuses_the_same_cookie_from_a_new_ip(make_client):
    client = make_client()
    assert post(client, "198.51.100.1").status_code == 201

    second = post(client, "198.51.100.2")  # the client sends back the cookie it was given

    assert second.status_code == 429
    assert second.json() == {"error": "visitor_limit"}


def test_the_route_refuses_a_new_cookie_from_the_same_ip(make_client):
    client = make_client()
    assert post(client, "198.51.100.1").status_code == 201
    client.cookies.clear()

    second = post(client, "198.51.100.1")

    assert second.status_code == 429
    assert second.json() == {"error": "visitor_limit"}
    assert second.headers["set-cookie"].startswith("mb_visitor=")  # a new visitor all the same


def test_the_route_refuses_a_demo_when_the_daily_budget_is_spent(make_client):
    client = make_client(daily_demo_minutes=4)  # 240 s, less than one 300 s demo

    refused = post(client, "198.51.100.1")

    assert refused.status_code == 429
    assert refused.json() == {"error": "daily_budget"}


def test_the_owner_key_bypasses_the_visitor_limit_and_starts_a_full_interview(make_client):
    client = make_client()
    assert post(client, "198.51.100.1").status_code == 201
    assert post(client, "198.51.100.1").status_code == 429

    owner = post(client, "198.51.100.1", **{"X-Owner-Key": OWNER_KEY})

    assert owner.status_code == 201
    assert owner.json()["cap_s"] == 1200


def test_the_owner_key_bypasses_the_daily_budget(make_client):
    client = make_client(daily_demo_minutes=0)

    owner = post(client, "198.51.100.1", **{"X-Owner-Key": OWNER_KEY})

    assert owner.status_code == 201
    assert owner.json()["cap_s"] == 1200


def test_a_wrong_owner_key_is_an_ordinary_demo_under_the_limits(make_client):
    client = make_client()

    first = post(client, "198.51.100.1", **{"X-Owner-Key": "a-guess"})
    second = post(client, "198.51.100.1", **{"X-Owner-Key": "a-guess"})

    assert first.status_code == 201
    assert first.json()["cap_s"] == 300
    assert second.status_code == 429
