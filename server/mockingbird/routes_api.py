"""The browser's HTTP routes: start an interview, fetch its report, and the health check.

Starting an interview creates its record, its brain and a one-time socket token, and nothing
else: no Vapi call exists until the browser opens the socket (routes_ws.py), so a start that is
never followed up costs nothing. It does hold a cap of the day's budget until it is abandoned, so
a start must come from a page on this site (`_from_this_site`): otherwise any page elsewhere could
make its own visitors' browsers hold the budget, without a single request of its own.
"""

import hmac
import random
import secrets
import time
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from mockingbird.brain import Brain
from mockingbird.limits import check_can_start, visitor_id
from mockingbird.models import Interview
from mockingbird.plan import DEMO, FULL, InterviewPlan
from mockingbird.questions import load_questions
from mockingbird.sessions import TOKEN_TTL_S, LiveSession

# questions.yaml sits beside the package, not inside it, so find it from this file and not from
# wherever the server happens to be started (ruling R15).
QUESTIONS_PATH = Path(__file__).resolve().parent.parent / "data" / "questions.yaml"

router = APIRouter()


@router.get("/healthz")
async def healthz(request: Request, db: bool = False) -> dict[str, bool]:
    # `?db=1` also checks the database. That is the Supabase keep-alive. Plain /healthz is what
    # the load balancer polls, so it must never touch the database.
    store = request.app.state.store
    if db and store is not None:
        await store.ping()
    return {"ok": True}


@router.post("/api/interviews")
async def start_interview(request: Request) -> JSONResponse:
    """Start a demo, or with the owner key a full interview, and hand back the socket path."""
    state = request.app.state
    settings = state.settings
    owner = _is_owner(request.headers.get("x-owner-key"), settings.owner_key)
    # The owner key skips this as it skips the limits: a page elsewhere cannot send that header
    # without a preflight, which fails, and does not know the key.
    if not owner and not _from_this_site(request, settings.public_base_url):
        return JSONResponse({"error": "forbidden_origin"}, status_code=403)
    visitor_hash, ip_hash, set_cookie = visitor_id(request)
    preset, cap_s = (FULL, settings.full_cap_s) if owner else (DEMO, settings.demo_cap_s)

    questions = load_questions(QUESTIONS_PATH, preset.main_questions, random.Random())
    interview = Interview(
        id=str(uuid.uuid4()),
        mode="full" if owner else "demo",
        status="created",
        visitor_hash=visitor_hash,
        ip_hash=ip_hash,
        cap_s=cap_s,
        questions=[q.text for q in questions],
        created_at=datetime.now(UTC),
    )
    # The check and the create go together, so two requests at once cannot both pass the check
    # before either one is counted: not two from one visitor, and not two from different
    # visitors when one demo's worth of budget is left. A created demo holds its cap in the
    # budget until its call ends or it is abandoned (Store.seconds_used_today).
    async with state.start_lock:
        if not owner:
            verdict = await check_can_start(
                state.store,
                visitor_hash=visitor_hash,
                ip_hash=ip_hash,
                cap_s=cap_s,
                daily_demo_s=settings.daily_demo_minutes * 60,
            )
            if verdict != "ok":
                return _with_cookie(JSONResponse({"error": verdict}, status_code=429), set_cookie)
        await state.store.create_interview(interview)

    # The plan's clock is the call's cap from the settings, so the interviewer wraps up before
    # the relay cuts the call off, whatever DEMO_CAP_S or FULL_CAP_S is set to.
    plan = InterviewPlan(replace(preset, total_s=cap_s), questions)
    token = secrets.token_urlsafe(32)
    state.registry.add(
        LiveSession(
            interview=interview,
            brain=Brain(plan, state.streamer_factory(), time.monotonic),
            token=token,
            token_expires_at=state.registry.clock() + TOKEN_TTL_S,
        )
    )
    body = {
        "interview_id": interview.id,
        "ws_path": f"/ws/interview/{interview.id}?token={token}",
        "cap_s": cap_s,
    }
    return _with_cookie(JSONResponse(body, status_code=201), set_cookie)


@router.get("/api/interviews/{interview_id}/report")
async def get_report(interview_id: str, request: Request) -> JSONResponse:
    """200 with the report once one is saved, whatever its status; 202 while it is on its way,
    which is once the interview has ended; 404 otherwise: no such interview, one that never ran
    a call, or one still live.

    The browser asks as soon as it hears the call ended, and the interview is marked ended
    before it hears that (routes_ws.py), so that first ask already gets 202.
    """
    store = request.app.state.store
    report = await store.get_report(interview_id)
    if report is not None:
        return JSONResponse(report)
    interview = await store.get_interview(interview_id)
    if interview is not None and interview.status == "ended":
        return JSONResponse({"status": "grading"}, status_code=202)
    raise HTTPException(status_code=404)


def _from_this_site(request: Request, public_base_url: str) -> bool:
    """Whether the start came from a page on this site, by its Origin header.

    A browser sends Origin with every POST, and a page cannot change it, so a page elsewhere
    cannot pass for this one. The start has no body and no custom header, so it needs no
    preflight, and CORS alone would not stop a cross-site post: the browser sends the request
    and only hides the reply. A request with no Origin (a script) is refused as well.

    Two origins count. PUBLIC_BASE_URL's, which is where the deployed page lives. And the host
    the request was sent to: in local development the page is on the Vite server, which passes
    /api on to the server, while PUBLIC_BASE_URL is the tunnel that Vapi uses.
    """
    origin = request.headers.get("origin")
    if not origin:
        return False
    if origin == _origin_of(public_base_url):
        return True
    host = request.headers.get("host")
    return host is not None and urlsplit(origin).netloc == host


def _origin_of(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def _is_owner(given: str | None, owner_key: str) -> bool:
    # bytes, because compare_digest refuses a str with characters outside ASCII
    return given is not None and hmac.compare_digest(given.encode(), owner_key.encode())


def _with_cookie(response: JSONResponse, set_cookie: str | None) -> JSONResponse:
    if set_cookie is not None:
        response.headers.append("set-cookie", set_cookie)
    return response
