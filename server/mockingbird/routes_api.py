"""The browser's HTTP routes: start an interview, fetch its report, and the health check.

Starting an interview creates its record, its brain and a one-time socket token, and nothing
else: no Vapi call exists until the browser opens the socket (routes_ws.py), so a start that is
never followed up costs nothing.
"""

import hmac
import random
import secrets
import time
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

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
    visitor_hash, ip_hash, set_cookie = visitor_id(request)
    owner = _is_owner(request.headers.get("x-owner-key"), settings.owner_key)
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
    # The check and the create go together, so two requests at once from one visitor cannot
    # both pass the check before either one is counted.
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
    """200 with the report once one is saved, whatever its status; 202 while it is on its way;
    404 when there is no such interview, or it never ran a call, so no report will come.

    A live interview counts as on its way. The relay tells the browser the call ended before the
    route has marked the interview ended, and the report page may ask in between.
    """
    store = request.app.state.store
    report = await store.get_report(interview_id)
    if report is not None:
        return JSONResponse(report)
    interview = await store.get_interview(interview_id)
    if interview is not None and interview.status in ("live", "ended"):
        return JSONResponse({"status": "grading"}, status_code=202)
    raise HTTPException(status_code=404)


def _is_owner(given: str | None, owner_key: str) -> bool:
    # bytes, because compare_digest refuses a str with characters outside ASCII
    return given is not None and hmac.compare_digest(given.encode(), owner_key.encode())


def _with_cookie(response: JSONResponse, set_cookie: str | None) -> JSONResponse:
    if set_cookie is not None:
        response.headers.append("set-cookie", set_cookie)
    return response
