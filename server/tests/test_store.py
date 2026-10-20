"""The store contract: MemoryStore always, and PostgresStore when TEST_DATABASE_URL is set.

The Postgres cases build a throwaway schema per test and drop it afterwards, so they never touch
(or count) rows that already exist in the database, which makes it safe to point
TEST_DATABASE_URL at a real one. "Now" is a fake clock, so no test depends on the wall clock.

Use a direct or session-mode connection string for TEST_DATABASE_URL. The scratch schema is chosen
with a session-level SET, which a transaction pooler would not keep from one query to the next.
"""

import asyncio
import os
import traceback
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import asyncpg
import pytest

from mockingbird.migrate import MIGRATIONS_DIR, apply_migrations
from mockingbird.migrate import migrate as run_migrate
from mockingbird.models import Interview, Turn
from mockingbird.store import MemoryStore, PostgresStore, create_pool

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
needs_postgres = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="TEST_DATABASE_URL is not set, so PostgresStore is not tested"
)

NOW = datetime(2026, 10, 19, 15, 30, tzinfo=UTC)
CALL_ID = "call-id-is-a-credential"
QUESTIONS = ["Tell me about a time you led a team.", "Tell me about a time you failed."]

REPORT = {
    "status": "graded",
    "questions": [
        {
            "question_idx": 0,
            "question_text": QUESTIONS[0],
            "items": [
                {
                    "item": "action",
                    "score": 3,
                    "rationale": "Named the concrete steps.",
                    "quotes": ["I wrote the migration plan"],
                    "dropped_quotes": ["we saved a million dollars"],
                    "status": "scored",
                },
                {
                    "item": "result",
                    "score": None,
                    "rationale": "No outcome was given.",
                    "quotes": [],
                    "dropped_quotes": [],
                    "status": "not_enough_evidence",
                },
            ],
        },
        {
            "question_idx": 1,
            "question_text": QUESTIONS[1],
            "items": [
                {
                    "item": "ownership",
                    "score": 4,
                    "rationale": "Owned it — and said so.",
                    "quotes": ["it was my call", "I shipped it"],
                    "dropped_quotes": [],
                    "status": "scored",
                },
            ],
        },
    ],
}


class Clock:
    """A settable 'now'. Tests move it by assigning .now."""

    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock(NOW)


def make_interview(id: str = "i1", **overrides) -> Interview:
    fields = {
        "id": id,
        "mode": "demo",
        "status": "created",
        "visitor_hash": "visitor-a",
        "ip_hash": "ip-x",
        "cap_s": 300,
        "questions": list(QUESTIONS),
        "created_at": NOW,
    }
    return Interview(**{**fields, **overrides})


def make_turn(idx: int, **overrides) -> Turn:
    fields = {"idx": idx, "speaker": "candidate", "text": f"line {idx}", "question_idx": 0}
    return Turn(**{**fields, **overrides})


# --- a throwaway Postgres schema ---------------------------------------------------------------


@asynccontextmanager
async def scratch_pool(*, migrated: bool = True):
    """A pool whose every connection works inside its own new schema, dropped on exit."""
    schema = f"test_{uuid.uuid4().hex}"
    admin = await asyncpg.connect(TEST_DATABASE_URL, statement_cache_size=0)
    await admin.execute(f'create schema "{schema}"')

    async def use_schema(conn: asyncpg.Connection) -> None:
        await conn.execute(f'set search_path to "{schema}"')

    # `setup`, not `init`: the pool runs RESET ALL when a connection is released, which would
    # undo a search_path set only once at connect time.
    pool = await create_pool(TEST_DATABASE_URL, setup=use_schema)
    try:
        if migrated:
            async with pool.acquire() as conn:
                await apply_migrations(conn)
        yield pool
    finally:
        await pool.close()
        await admin.execute(f'drop schema "{schema}" cascade')
        await admin.close()


@pytest.fixture
async def pg_pool():
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL is not set")
    async with scratch_pool() as pool:
        yield pool


@pytest.fixture
async def bare_pool():
    """A scratch schema with no migrations applied yet."""
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL is not set")
    async with scratch_pool(migrated=False) as pool:
        yield pool


@pytest.fixture(params=["memory", pytest.param("postgres", marks=needs_postgres)])
async def store(request, clock):
    if request.param == "memory":
        yield MemoryStore(clock=clock)
    else:
        async with scratch_pool() as pool:
            yield PostgresStore(pool, clock=clock)


# --- interviews --------------------------------------------------------------------------------


async def test_create_get_and_update_round_trip(store):
    original = make_interview()
    await store.create_interview(original)

    got = await store.get_interview("i1")
    assert got == original
    assert got is not original

    await store.update_interview("i1", status="live", vapi_call_id=CALL_ID)
    live = await store.get_interview("i1")
    assert live == make_interview(status="live", vapi_call_id=CALL_ID)

    await store.update_interview("i1", status="ended", ended_reason="plan_done", duration_s=95)
    ended = await store.get_interview("i1")
    assert ended == make_interview(
        status="ended", vapi_call_id=CALL_ID, ended_reason="plan_done", duration_s=95
    )


async def test_get_unknown_interview_is_none(store):
    assert await store.get_interview("nope") is None


async def test_changing_a_returned_interview_does_not_change_the_store(store):
    await store.create_interview(make_interview())
    got = await store.get_interview("i1")
    got.status = "graded"
    got.questions.append("not a real question")
    assert await store.get_interview("i1") == make_interview()


async def test_questions_keep_their_order(store):
    await store.create_interview(make_interview(questions=["c", "a", "b"]))
    assert (await store.get_interview("i1")).questions == ["c", "a", "b"]


async def test_update_with_no_fields_changes_nothing(store):
    await store.create_interview(make_interview())
    await store.update_interview("i1")
    assert await store.get_interview("i1") == make_interview()


async def test_update_rejects_fields_an_interview_does_not_have(store):
    await store.create_interview(make_interview())
    with pytest.raises(ValueError, match="nonsense"):
        await store.update_interview("i1", nonsense=1)


async def test_create_rejects_an_id_that_exists(store):
    await store.create_interview(make_interview())
    with pytest.raises(ValueError, match="i1"):
        await store.create_interview(make_interview(visitor_hash="someone-else"))
    assert (await store.get_interview("i1")).visitor_hash == "visitor-a"


async def test_create_rejects_a_created_at_without_a_timezone(store):
    with pytest.raises(ValueError, match="timezone"):
        await store.create_interview(make_interview(created_at=datetime(2026, 10, 19, 15, 30)))


async def test_writes_to_an_interview_that_does_not_exist_raise_key_error(store):
    with pytest.raises(KeyError):
        await store.update_interview("nope", status="live")
    with pytest.raises(KeyError):
        await store.add_turn("nope", make_turn(0))
    with pytest.raises(KeyError):
        await store.save_report("nope", REPORT)


async def test_ping(store):
    assert await store.ping() is None


# --- turns -------------------------------------------------------------------------------------


async def test_turns_come_back_ordered_by_idx(store):
    await store.create_interview(make_interview())
    turns = [
        make_turn(0, speaker="interviewer", text="Hello. First question?"),
        make_turn(1, text="I led a team of four."),
        make_turn(2, speaker="interviewer", text="What did you do?", interrupted=True),
        make_turn(3, text="I wrote the plan.", question_idx=1),
    ]
    for t in (turns[2], turns[0], turns[3], turns[1]):
        await store.add_turn("i1", t)

    assert await store.list_turns("i1") == turns


async def test_a_blank_candidate_turn_is_stored(store):
    await store.create_interview(make_interview())
    await store.add_turn("i1", make_turn(0, speaker="interviewer", text="Go ahead."))
    await store.add_turn("i1", make_turn(1, text=""))

    got = await store.list_turns("i1")
    assert [t.text for t in got] == ["Go ahead.", ""]


async def test_turns_belong_to_their_own_interview(store):
    await store.create_interview(make_interview("i1"))
    await store.create_interview(make_interview("i2"))
    await store.add_turn("i1", make_turn(0, text="mine"))
    await store.add_turn("i2", make_turn(0, text="theirs"))

    assert [t.text for t in await store.list_turns("i1")] == ["mine"]
    assert [t.text for t in await store.list_turns("i2")] == ["theirs"]
    assert await store.list_turns("nope") == []


async def test_a_turn_idx_cannot_be_used_twice(store):
    await store.create_interview(make_interview())
    await store.add_turn("i1", make_turn(0, text="first"))
    with pytest.raises(ValueError, match="idx"):
        await store.add_turn("i1", make_turn(0, text="again"))
    assert [t.text for t in await store.list_turns("i1")] == ["first"]


# --- reports -----------------------------------------------------------------------------------


async def test_report_round_trip_and_replace(store):
    await store.create_interview(make_interview())
    assert await store.get_report("i1") is None

    await store.save_report("i1", REPORT)
    assert await store.get_report("i1") == REPORT

    replacement = {"status": "no_answers", "questions": []}
    await store.save_report("i1", replacement)
    assert await store.get_report("i1") == replacement


async def test_a_report_that_repeats_an_item_is_still_saved(store):
    # Nothing stops the grader repeating an item, or a whole question. The report is the
    # authoritative copy, so such a report must be kept as it is, never refused.
    answer = {"item": "action", "rationale": "r", "quotes": ["q"], "status": "scored"}
    repeated = {
        "status": "graded",
        "questions": [
            {
                "question_idx": 0,
                "question_text": QUESTIONS[0],
                "items": [dict(answer, score=1), dict(answer, score=2)],  # one item twice
            },
            {
                "question_idx": 0,  # the same question again
                "question_text": QUESTIONS[0],
                "items": [dict(answer, score=3)],
            },
        ],
    }
    await store.create_interview(make_interview())
    await store.save_report("i1", repeated)
    assert await store.get_report("i1") == repeated


async def test_get_report_for_an_unknown_interview_is_none(store):
    assert await store.get_report("nope") is None


async def test_the_returned_report_is_a_copy(store):
    await store.create_interview(make_interview())
    await store.save_report("i1", {"status": "graded", "questions": []})
    got = await store.get_report("i1")
    got["status"] = "tampered"
    assert (await store.get_report("i1"))["status"] == "graded"


# --- the daily limits --------------------------------------------------------------------------


async def test_seconds_used_today_is_zero_for_an_empty_store(store):
    assert await store.seconds_used_today() == 0


async def test_seconds_used_today_counts_live_at_cap_and_ended_at_duration(store):
    demos = [
        ("live-demo", "live", None),  # counts its cap_s: 300
        ("ended-demo", "ended", 95),
        ("graded-demo", "graded", 40),  # grading does not give the seconds back
        ("ended-without-duration", "ended", None),  # counts nothing
        ("created-demo", "created", None),  # not started yet
        ("abandoned-demo", "abandoned", 77),
    ]
    for id, status, duration_s in demos:
        await store.create_interview(make_interview(id, status=status, duration_s=duration_s))
    # Full mode has its own cap and is never limited.
    await store.create_interview(
        make_interview("full-live", mode="full", status="live", cap_s=1200)
    )
    await store.create_interview(
        make_interview("full-ended", mode="full", status="ended", duration_s=500)
    )
    # Ended on an earlier day: not today's.
    await store.create_interview(
        make_interview(
            "ended-yesterday", status="ended", duration_s=111, created_at=NOW - timedelta(days=1)
        )
    )

    assert await store.seconds_used_today() == 300 + 95 + 40


async def test_a_live_demo_counts_at_its_own_cap(store):
    await store.create_interview(make_interview(status="live", cap_s=123))
    assert await store.seconds_used_today() == 123


async def test_days_roll_over_at_utc_midnight(store, clock):
    created = datetime(2026, 10, 19, 23, 58, tzinfo=UTC)
    await store.create_interview(
        make_interview(status="ended", duration_s=100, created_at=created, visitor_hash="v")
    )

    clock.now = datetime(2026, 10, 19, 23, 59, 59, tzinfo=UTC)
    assert await store.seconds_used_today() == 100
    assert await store.starts_today("v", "other") == 1

    clock.now = datetime(2026, 10, 20, 0, 0, 0, tzinfo=UTC)
    assert await store.seconds_used_today() == 0
    assert await store.starts_today("v", "other") == 0


async def test_the_day_is_a_utc_day_whatever_offset_the_time_is_written_in(store):
    # 20:00 on the 19th in UTC-5 is 01:00 on the 20th in UTC, which is not "today" (the 19th).
    late_evening_in_new_york = datetime(2026, 10, 19, 20, 0, tzinfo=timezone(timedelta(hours=-5)))
    await store.create_interview(
        make_interview(status="ended", duration_s=100, created_at=late_evening_in_new_york)
    )
    assert await store.seconds_used_today() == 0
    assert await store.starts_today("visitor-a", "ip-x") == 0

    # 20:00 on the 18th in UTC-5 is 01:00 on the 19th in UTC, which is.
    evening_before = datetime(2026, 10, 18, 20, 0, tzinfo=timezone(timedelta(hours=-5)))
    await store.create_interview(
        make_interview("i2", status="ended", duration_s=60, created_at=evening_before)
    )
    assert await store.seconds_used_today() == 60
    assert await store.starts_today("visitor-a", "ip-x") == 1


async def test_starts_today_matches_on_the_visitor_alone_or_the_ip_alone(store):
    await store.create_interview(make_interview("a", visitor_hash="visitor-a", ip_hash="ip-x"))
    await store.create_interview(make_interview("b", visitor_hash="visitor-b", ip_hash="ip-y"))

    assert await store.starts_today("visitor-a", "someone-else") == 1  # same cookie, new network
    assert await store.starts_today("someone-else", "ip-y") == 1  # same network, cleared cookie
    assert await store.starts_today("someone-else", "elsewhere") == 0
    assert await store.starts_today("visitor-a", "ip-y") == 2  # one of each
    assert await store.starts_today("visitor-a", "ip-x") == 1  # matching both counts it once


async def test_starts_today_ignores_abandoned_other_days_and_full_mode(store):
    await store.create_interview(make_interview("abandoned", status="abandoned"))
    await store.create_interview(
        make_interview("yesterday", created_at=NOW - timedelta(days=1), status="ended")
    )
    await store.create_interview(make_interview("full", mode="full"))
    assert await store.starts_today("visitor-a", "ip-x") == 0

    for id, status in [("c", "created"), ("l", "live"), ("e", "ended"), ("g", "graded")]:
        await store.create_interview(make_interview(id, status=status))
    assert await store.starts_today("visitor-a", "ip-x") == 4


# --- lookups for the sweeper and the webhook ---------------------------------------------------


async def test_list_live_interviews_returns_every_live_one_and_nothing_else(store):
    for id, status in [("c", "created"), ("e", "ended"), ("g", "graded"), ("a", "abandoned")]:
        await store.create_interview(make_interview(id, status=status))
    await store.create_interview(make_interview("live-demo", status="live"))
    # A live call left over from another day is still live: the sweeper must see it.
    await store.create_interview(
        make_interview(
            "live-full", mode="full", status="live", cap_s=1200, created_at=NOW - timedelta(days=2)
        )
    )

    live = await store.list_live_interviews()

    assert sorted(i.id for i in live) == ["live-demo", "live-full"]
    assert {i.id: i.cap_s for i in live} == {"live-demo": 300, "live-full": 1200}
    assert next(i for i in live if i.id == "live-full").created_at == NOW - timedelta(days=2)


async def test_list_live_interviews_is_empty_when_none_are_live(store):
    await store.create_interview(make_interview(status="ended"))
    assert await store.list_live_interviews() == []


async def test_get_interview_by_call_finds_the_interview_that_holds_the_call_id(store):
    await store.create_interview(make_interview("with-call", vapi_call_id=CALL_ID))
    await store.create_interview(make_interview("without-call"))

    found = await store.get_interview_by_call(CALL_ID)

    assert found == make_interview("with-call", vapi_call_id=CALL_ID)
    assert await store.get_interview_by_call("some-other-call") is None


# --- the 7-day retention -----------------------------------------------------------------------


async def test_delete_demo_data_older_than_removes_old_demos_and_keeps_the_rest(store):
    old_demo = make_interview("old-demo", created_at=NOW - timedelta(days=8))
    recent_demo = make_interview("recent-demo", created_at=NOW - timedelta(days=6))
    old_full = make_interview("old-full", mode="full", created_at=NOW - timedelta(days=8))
    for i in (old_demo, recent_demo, old_full):
        await store.create_interview(i)
        await store.add_turn(i.id, make_turn(0, text=f"turn of {i.id}"))
        await store.save_report(i.id, REPORT)

    assert await store.delete_demo_data_older_than(7) == 1

    assert await store.get_interview("old-demo") is None
    assert await store.list_turns("old-demo") == []
    assert await store.get_report("old-demo") is None
    for kept in (recent_demo, old_full):
        assert await store.get_interview(kept.id) == kept
        assert [t.text for t in await store.list_turns(kept.id)] == [f"turn of {kept.id}"]
        assert await store.get_report(kept.id) == REPORT

    assert await store.delete_demo_data_older_than(7) == 0


async def test_delete_demo_data_older_than_counts_every_interview_it_deletes(store):
    for n in range(3):
        await store.create_interview(make_interview(f"old-{n}", created_at=NOW - timedelta(days=9)))
    await store.create_interview(make_interview("new"))
    assert await store.delete_demo_data_older_than(7) == 3


async def test_an_interview_exactly_seven_days_old_is_kept(store):
    await store.create_interview(make_interview("edge", created_at=NOW - timedelta(days=7)))
    await store.create_interview(
        make_interview("past-edge", created_at=NOW - timedelta(days=7, seconds=1))
    )
    assert await store.delete_demo_data_older_than(7) == 1
    assert await store.get_interview("edge") is not None
    assert await store.get_interview("past-edge") is None


# --- the Vapi call id is a credential while a call is live -------------------------------------


def test_the_vapi_call_id_stays_out_of_an_interviews_repr():
    assert CALL_ID not in repr(make_interview(vapi_call_id=CALL_ID))


async def test_the_store_never_logs_the_vapi_call_id(store, caplog):
    # The store has no logger today, so this only guards the day someone adds one.
    caplog.set_level("DEBUG")
    await store.create_interview(make_interview())
    await store.update_interview("i1", status="live", vapi_call_id=CALL_ID)
    await store.get_interview("i1")
    await store.get_interview_by_call(CALL_ID)
    await store.list_live_interviews()
    await store.seconds_used_today()
    await store.delete_demo_data_older_than(7)
    assert CALL_ID not in caplog.text


def assert_call_id_not_shown(exc: BaseException, caplog) -> None:
    """The call id appears nowhere a caller could put a failure: not in its text, its repr, the
    traceback a logger would write (which includes whatever it was chained to), or the log."""
    shown = "".join(traceback.format_exception(exc))
    for text in (str(exc), repr(exc), shown, caplog.text):
        assert CALL_ID not in text


# Writes the store must refuse, on a database where another interview already holds the id "ok"
# with a live call. Postgres quotes the whole failing row in its error when a check constraint
# fails, and that row holds the call id.
REFUSED_WRITES = {
    "create-unknown-status": (
        lambda s: s.create_interview(
            make_interview("new", status="exploded", vapi_call_id=CALL_ID)
        ),
        "status",
    ),
    "create-unknown-mode": (
        lambda s: s.create_interview(make_interview("new", mode="trial", vapi_call_id=CALL_ID)),
        "mode",
    ),
    "create-id-taken": (
        lambda s: s.create_interview(make_interview("ok", vapi_call_id=CALL_ID)),
        "already exists",
    ),
    "update-to-unknown-status": (lambda s: s.update_interview("ok", status="exploded"), "status"),
    "update-to-unknown-mode": (lambda s: s.update_interview("ok", mode="trial"), "mode"),
}


@pytest.mark.parametrize("write, names", REFUSED_WRITES.values(), ids=REFUSED_WRITES.keys())
async def test_a_refused_write_never_shows_the_vapi_call_id(store, caplog, write, names):
    caplog.set_level("DEBUG")
    live = make_interview("ok", status="live", vapi_call_id=CALL_ID)
    await store.create_interview(live)

    with pytest.raises(ValueError, match=names) as refused:
        await write(store)

    assert_call_id_not_shown(refused.value, caplog)
    assert await store.get_interview("ok") == live  # and nothing changed


class NotAString:
    """Not text, so Postgres cannot take it. asyncpg's error for that quotes the value's repr."""

    def __repr__(self) -> str:
        return f"NotAString({CALL_ID})"


# What only a real database can refuse: a column that must not be null, and a value of a type it
# cannot hold. Each of these makes Postgres or asyncpg quote the row or the value in its error.
DATABASE_REFUSED_WRITES = {
    "create-null-visitor": (
        lambda s: s.create_interview(
            make_interview("new", visitor_hash=None, vapi_call_id=CALL_ID)
        ),
        "visitor_hash",
    ),
    "update-null-visitor": (lambda s: s.update_interview("ok", visitor_hash=None), "visitor_hash"),
    "create-call-id-of-the-wrong-type": (
        lambda s: s.create_interview(make_interview("new", vapi_call_id=NotAString())),
        "cannot hold",
    ),
    "update-call-id-to-the-wrong-type": (
        lambda s: s.update_interview("ok", vapi_call_id=NotAString()),
        "cannot hold",
    ),
}


@needs_postgres
@pytest.mark.parametrize(
    "write, names", DATABASE_REFUSED_WRITES.values(), ids=DATABASE_REFUSED_WRITES.keys()
)
async def test_a_write_the_database_refuses_never_shows_the_vapi_call_id(
    pg_pool, clock, caplog, write, names
):
    caplog.set_level("DEBUG")
    store = PostgresStore(pg_pool, clock=clock)
    live = make_interview("ok", status="live", vapi_call_id=CALL_ID)
    await store.create_interview(live)

    with pytest.raises(ValueError, match=names) as refused:
        await write(store)

    assert_call_id_not_shown(refused.value, caplog)
    assert await store.get_interview("ok") == live


# --- pools and connections ---------------------------------------------------------------------


async def test_create_pool_turns_the_statement_cache_off(monkeypatch):
    # Supabase's transaction pooler breaks asyncpg's cached prepared statements.
    seen = {}

    async def fake_create_pool(dsn, **kwargs):
        seen.update(kwargs, dsn=dsn)
        return "a pool"

    monkeypatch.setattr(asyncpg, "create_pool", fake_create_pool)
    assert await create_pool("postgresql://db.test/x", setup="callback") == "a pool"
    assert seen["statement_cache_size"] == 0
    assert seen["setup"] == "callback"  # other options pass through


async def test_migrate_connects_with_the_statement_cache_off(monkeypatch, tmp_path):
    seen = {}

    class FakeConnection:
        async def close(self):
            seen["closed"] = True

    async def fake_connect(dsn, **kwargs):
        seen.update(kwargs, dsn=dsn)
        return FakeConnection()

    async def fake_apply(conn, migrations_dir):
        seen["applied_to"] = conn
        return ["001_x.sql"]

    monkeypatch.setattr(asyncpg, "connect", fake_connect)
    monkeypatch.setattr("mockingbird.migrate.apply_migrations", fake_apply)
    assert await run_migrate("postgresql://db.test/x", tmp_path) == ["001_x.sql"]
    assert seen["statement_cache_size"] == 0
    assert seen["closed"] is True


# --- Postgres only: what the memory store has no equivalent of ---------------------------------


@needs_postgres
async def test_save_report_fills_the_scores_and_quotes_tables(pg_pool, clock):
    store = PostgresStore(pg_pool, clock=clock)
    await store.create_interview(make_interview())
    await store.save_report("i1", REPORT)

    scores = await pg_pool.fetch(
        "select question_idx, item, score, rationale, status from scores where interview_id = 'i1'"
    )
    assert sorted(tuple(r) for r in scores) == [
        (0, "action", 3, "Named the concrete steps.", "scored"),
        (0, "result", None, "No outcome was given.", "not_enough_evidence"),
        (1, "ownership", 4, "Owned it — and said so.", "scored"),
    ]
    quotes = await pg_pool.fetch(
        "select s.item, q.text, q.verified, q.start_ms, q.end_ms"
        " from quotes q join scores s on s.id = q.score_id where s.interview_id = 'i1'"
    )
    assert sorted(tuple(r) for r in quotes) == [
        ("action", "I wrote the migration plan", True, None, None),
        ("action", "we saved a million dollars", False, None, None),
        ("ownership", "I shipped it", True, None, None),
        ("ownership", "it was my call", True, None, None),
    ]


@needs_postgres
async def test_saving_a_report_again_replaces_its_scores_and_quotes(pg_pool, clock):
    store = PostgresStore(pg_pool, clock=clock)
    await store.create_interview(make_interview())
    await store.save_report("i1", REPORT)
    await store.save_report("i1", REPORT)
    assert await pg_pool.fetchval("select count(*) from scores") == 3
    assert await pg_pool.fetchval("select count(*) from quotes") == 4

    await store.save_report("i1", {"status": "no_answers", "questions": []})
    assert await pg_pool.fetchval("select count(*) from scores") == 0
    assert await pg_pool.fetchval("select count(*) from quotes") == 0


@needs_postgres
async def test_a_repeated_item_becomes_a_row_for_each_copy(pg_pool, clock):
    # scores and quotes are copies of the report, so they hold what it holds, repeats included.
    store = PostgresStore(pg_pool, clock=clock)
    await store.create_interview(make_interview())
    item = {"item": "action", "rationale": "r", "status": "scored"}
    await store.save_report(
        "i1",
        {
            "status": "graded",
            "questions": [
                {
                    "question_idx": 0,
                    "items": [
                        dict(item, score=1, quotes=["one"]),
                        dict(item, score=2, quotes=["two", "three"]),
                    ],
                },
                {"question_idx": 0, "items": [dict(item, score=3, quotes=[])]},
            ],
        },
    )

    rows = await pg_pool.fetch("select score, question_idx, item from scores order by score")
    assert [tuple(r) for r in rows] == [(1, 0, "action"), (2, 0, "action"), (3, 0, "action")]
    quotes = await pg_pool.fetch(
        "select s.score, q.text from quotes q join scores s on s.id = q.score_id order by q.text"
    )
    assert [tuple(r) for r in quotes] == [(1, "one"), (2, "three"), (2, "two")]


BAD_ITEM = {"item": "action", "score": 1, "rationale": "r", "quotes": [], "status": "scored"}
UNSTORABLE_REPORTS = {
    "score-is-not-a-number": dict(BAD_ITEM, score="high"),  # asyncpg refuses it
    "rationale-is-null": dict(BAD_ITEM, rationale=None),  # a not null column refuses it
    "no-rationale-key": {k: v for k, v in BAD_ITEM.items() if k != "rationale"},  # caught early
}


@needs_postgres
@pytest.mark.parametrize("bad_item", UNSTORABLE_REPORTS.values(), ids=UNSTORABLE_REPORTS.keys())
async def test_a_report_that_cannot_be_copied_into_the_tables_leaves_the_old_one_in_place(
    pg_pool, clock, bad_item
):
    # A report is all or nothing. This also pins that the refusal is a ValueError, not a driver
    # error, the same as every other refused write.
    store = PostgresStore(pg_pool, clock=clock)
    await store.create_interview(make_interview())
    await store.save_report("i1", REPORT)

    with pytest.raises(ValueError, match="report"):
        await store.save_report(
            "i1", {"status": "graded", "questions": [{"question_idx": 0, "items": [bad_item]}]}
        )

    assert await store.get_report("i1") == REPORT
    assert await pg_pool.fetchval("select count(*) from scores") == 3
    assert await pg_pool.fetchval("select count(*) from quotes") == 4


@needs_postgres
async def test_deleting_old_demo_data_also_deletes_its_scores_and_quotes(pg_pool, clock):
    store = PostgresStore(pg_pool, clock=clock)
    await store.create_interview(make_interview("old", created_at=NOW - timedelta(days=8)))
    await store.create_interview(
        make_interview("old-full", mode="full", created_at=NOW - timedelta(days=8))
    )
    for id in ("old", "old-full"):
        await store.add_turn(id, make_turn(0))
        await store.save_report(id, REPORT)

    assert await store.delete_demo_data_older_than(7) == 1

    for table in ("turns", "scores"):
        ids = {r[0] for r in await pg_pool.fetch(f"select interview_id from {table}")}
        assert ids == {"old-full"}, table
    assert await pg_pool.fetchval("select count(*) from quotes") == 4  # only old-full's remain


@needs_postgres
async def test_the_database_refuses_a_turn_from_a_speaker_the_models_do_not_allow(pg_pool, clock):
    # The interview tables' own check constraints are covered by test_a_refused_write_never_...
    store = PostgresStore(pg_pool, clock=clock)
    await store.create_interview(make_interview())
    with pytest.raises(asyncpg.CheckViolationError):
        await store.add_turn("i1", make_turn(0, speaker="narrator"))


# --- migrations (Postgres only) ----------------------------------------------------------------


def write_migrations(directory: Path, files: dict[str, str]) -> Path:
    for name, sql in files.items():
        (directory / name).write_text(sql)
    return directory


async def applied_names(conn) -> list[str]:
    return [r[0] for r in await conn.fetch("select name from schema_migrations order by name")]


@needs_postgres
async def test_the_real_migration_creates_the_four_tables(pg_pool):
    tables = {
        r[0]
        for r in await pg_pool.fetch(
            "select table_name from information_schema.tables where table_schema = current_schema()"
        )
    }
    assert tables == {"schema_migrations", "interviews", "turns", "scores", "quotes"}
    assert "001_init.sql" in [p.name for p in MIGRATIONS_DIR.glob("*.sql")]
    async with pg_pool.acquire() as conn:
        assert await applied_names(conn) == ["001_init.sql"]


@needs_postgres
async def test_row_level_security_is_on_for_every_table(pg_pool):
    # Supabase's REST API serves any table without it. See the end of 001_init.sql.
    rows = await pg_pool.fetch(
        "select relname, relrowsecurity from pg_class"
        " where relnamespace = current_schema()::regnamespace and relkind = 'r'"
    )
    assert {r["relname"]: r["relrowsecurity"] for r in rows} == {
        "interviews": True,
        "turns": True,
        "scores": True,
        "quotes": True,
        "schema_migrations": True,
    }


@needs_postgres
async def test_migrations_run_in_name_order_once_each(bare_pool, tmp_path):
    migrations = write_migrations(
        tmp_path,
        {
            # 002 needs the table 001 makes, and would fail if it ran a second time.
            "002_more.sql": "alter table widgets add column color text;",
            "001_first.sql": "create table widgets (id int primary key);",
        },
    )
    async with bare_pool.acquire() as conn:
        assert await apply_migrations(conn, migrations) == ["001_first.sql", "002_more.sql"]
        assert await apply_migrations(conn, migrations) == []
        assert await applied_names(conn) == ["001_first.sql", "002_more.sql"]

        write_migrations(tmp_path, {"003_third.sql": "alter table widgets add column size int;"})
        assert await apply_migrations(conn, migrations) == ["003_third.sql"]


@needs_postgres
async def test_a_failing_migration_rolls_back_and_leaves_earlier_ones_applied(bare_pool, tmp_path):
    migrations = write_migrations(
        tmp_path,
        {
            "001_ok.sql": "create table kept (id int);",
            "002_half.sql": "create table half (id int); select 1 / 0;",
        },
    )
    async with bare_pool.acquire() as conn:
        with pytest.raises(asyncpg.DivisionByZeroError):
            await apply_migrations(conn, migrations)

        assert await applied_names(conn) == ["001_ok.sql"]
        assert await conn.fetchval("select to_regclass('kept')") is not None
        assert await conn.fetchval("select to_regclass('half')") is None


@needs_postgres
async def test_two_apps_migrating_at_once_apply_each_file_once(bare_pool, tmp_path):
    # An ECS deploy briefly runs the old and the new task side by side, and both migrate.
    migrations = write_migrations(
        tmp_path, {"001_first.sql": "create table widgets (id int primary key);"}
    )
    async with bare_pool.acquire() as one, bare_pool.acquire() as two:
        results = await asyncio.gather(
            apply_migrations(one, migrations), apply_migrations(two, migrations)
        )
        assert sorted(results) == [[], ["001_first.sql"]]
        assert await applied_names(one) == ["001_first.sql"]
