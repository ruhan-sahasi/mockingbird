"""Where interviews, their turns and their reports are kept.

`Store` is what the rest of the server talks to. `MemoryStore` backs the tests and any run without
a database. `PostgresStore` is the real one, on Supabase Postgres. Both do the same things, and
the same tests run against both.

Two rules every store follows:

- The daily limits count UTC calendar days, and "now" comes from a clock passed to the
  constructor, so tests can move time instead of waiting for it. An interview belongs to the day
  it was created, because the record keeps no separate end time. One that crosses midnight is
  charged to the day it started.
- `Interview.vapi_call_id` is a credential while a call is live. Nothing here logs it, and it is
  left out of every error message. That takes work on Postgres: when a write breaks a rule, the
  database quotes the whole failing row in its error, and an `interviews` row holds the call id.
  So PostgresStore turns those errors into a ValueError that names the rule and nothing else, and
  does not chain it to the driver's error, whose traceback would print the row.
"""

import copy
import json
from collections.abc import Callable
from dataclasses import fields, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, get_args

import asyncpg

from mockingbird.models import Interview, Mode, Status, Turn

Clock = Callable[[], datetime]

# A finished interview. A graded one still counts toward the day's seconds: grading the report
# does not give the call time back.
_FINISHED = ("ended", "graded")
# What update_interview may change: any field of an Interview except its id. This is also what
# makes it safe to put the field names into the SQL.
_UPDATABLE = frozenset(f.name for f in fields(Interview)) - {"id"}
_INTERVIEW_COLUMNS = (
    "id, mode, status, visitor_hash, ip_hash, cap_s, questions, created_at,"
    " vapi_call_id, ended_reason, duration_s"
)


def utc_now() -> datetime:
    return datetime.now(UTC)


class Store(Protocol):
    """Everything the server needs to remember. All methods are async.

    An operation on an interview that does not exist raises KeyError, except the reads, which
    return None or an empty list.
    """

    async def create_interview(self, i: Interview) -> None:
        """Raises ValueError if the id is taken, if `created_at` has no timezone, or if a value
        breaks a rule of the interviews table (an unknown mode or status, a missing hash). The
        message names the rule, never the value."""
        ...

    async def get_interview(self, id: str) -> Interview | None: ...

    async def get_interview_by_call(self, call_id: str) -> Interview | None:
        """The interview whose `vapi_call_id` is `call_id`. Vapi's end-of-call report names only
        the call, and it can come after the live session is gone."""
        ...

    async def list_live_interviews(self) -> list[Interview]:
        """Every interview whose status is `live`, of any mode and any day. One left live by a
        crash shows up here, so the sweeper can end it."""
        ...

    async def update_interview(self, id: str, **changes: Any) -> None:
        """Change fields of an interview. Raises ValueError for a field it does not have, for
        `id`, which never changes, or for a value that breaks a rule of the interviews table, as
        in create_interview."""
        ...

    async def add_turn(self, interview_id: str, t: Turn) -> None:
        """Raises ValueError if the interview already has a turn with this idx. Blank text is
        fine: the brain records silence as a blank candidate turn."""
        ...

    async def list_turns(self, interview_id: str) -> list[Turn]:
        """The interview's turns in order of `idx`."""
        ...

    async def save_report(self, interview_id: str, report: dict) -> None:
        """Keep the report, replacing any earlier one. A repeated item is kept as it is: the
        grader's output is not trusted to be free of repeats. Raises ValueError for a report whose
        items lack a key or hold a value the scores and quotes tables cannot store, and then the
        earlier report stays."""
        ...

    async def get_report(self, interview_id: str) -> dict | None: ...

    async def seconds_used_today(self) -> int:
        """Demo seconds spent today: `duration_s` of each finished demo interview, plus `cap_s`
        of each demo that is live right now. Full-mode and abandoned interviews are ignored."""
        ...

    async def starts_today(self, visitor_hash: str, ip_hash: str) -> int:
        """Demo interviews started today, other than abandoned ones, by this visitor or from
        this IP. One that matches on both counts once."""
        ...

    async def ping(self) -> None:
        """Raises if the store cannot be reached."""
        ...

    async def delete_demo_data_older_than(self, days: int) -> int:
        """Delete demo interviews created more than `days` days ago, with their turns, scores
        and quotes. Full-mode interviews are kept. Returns how many interviews it deleted."""
        ...


def _utc_day(now: datetime) -> tuple[datetime, datetime]:
    """The start and end of the UTC calendar day that `now` falls in."""
    start = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


def _check_values(values: dict[str, Any]) -> None:
    """MemoryStore's version of the check constraints on `mode` and `status`, so that it refuses
    what the database refuses. It names the field, not the value."""
    for name, allowed in (("mode", get_args(Mode)), ("status", get_args(Status))):
        if name in values and values[name] not in allowed:
            raise ValueError(f"{name} must be one of {', '.join(allowed)}")


def _refusal(subject: str, exc: asyncpg.PostgresError) -> ValueError:
    """A ValueError saying that the database refused `subject`, and which rule it broke.

    This is all it says. Postgres writes the whole failing row into the error text, and asyncpg
    writes the offending value, so neither may reach a message that a caller will log: an
    `interviews` row holds the Vapi call id. Raise it `from None` as well, so a traceback does not
    print the driver's error as the cause.
    """
    rule = getattr(exc, "constraint_name", None) or getattr(exc, "column_name", None)
    if rule:
        return ValueError(f"{subject} was refused by the database: {rule}")
    return ValueError(f"{subject} has a value the database cannot hold")


def _check_new(i: Interview) -> None:
    # A time without a timezone could land on the wrong UTC day, so refuse it up front.
    if i.created_at.tzinfo is None:
        raise ValueError("created_at must have a timezone")


def _check_fields(changes: dict[str, Any]) -> None:
    unknown = changes.keys() - _UPDATABLE
    if unknown:
        raise ValueError(f"cannot update interview field(s): {', '.join(sorted(unknown))}")


class MemoryStore:
    """Keeps everything in dicts. It hands out copies, as a database would, so changing what
    you got back never changes what is stored. It keeps the report as one piece and does not
    split it into scores and quotes, because nothing reads them back."""

    def __init__(self, clock: Clock = utc_now) -> None:
        self._clock = clock
        self._interviews: dict[str, Interview] = {}
        self._turns: dict[str, dict[int, Turn]] = {}
        self._reports: dict[str, dict] = {}

    async def create_interview(self, i: Interview) -> None:
        _check_new(i)
        _check_values({"mode": i.mode, "status": i.status})
        if i.id in self._interviews:
            raise ValueError(f"interview {i.id} already exists")
        self._interviews[i.id] = copy.deepcopy(i)

    async def get_interview(self, id: str) -> Interview | None:
        found = self._interviews.get(id)
        return None if found is None else copy.deepcopy(found)

    async def get_interview_by_call(self, call_id: str) -> Interview | None:
        for i in self._interviews.values():
            if i.vapi_call_id == call_id:
                return copy.deepcopy(i)
        return None

    async def list_live_interviews(self) -> list[Interview]:
        return [copy.deepcopy(i) for i in self._interviews.values() if i.status == "live"]

    async def update_interview(self, id: str, **changes: Any) -> None:
        _check_fields(changes)
        if not changes:
            return
        if id not in self._interviews:
            raise KeyError(id)
        _check_values(changes)
        for name, value in changes.items():
            setattr(self._interviews[id], name, copy.deepcopy(value))

    async def add_turn(self, interview_id: str, t: Turn) -> None:
        if interview_id not in self._interviews:
            raise KeyError(interview_id)
        turns = self._turns.setdefault(interview_id, {})
        if t.idx in turns:
            raise ValueError(f"interview {interview_id} already has a turn with idx {t.idx}")
        turns[t.idx] = replace(t)

    async def list_turns(self, interview_id: str) -> list[Turn]:
        turns = self._turns.get(interview_id, {})
        return [replace(turns[idx]) for idx in sorted(turns)]

    async def save_report(self, interview_id: str, report: dict) -> None:
        if interview_id not in self._interviews:
            raise KeyError(interview_id)
        # Through JSON, like the jsonb column: a copy, and only what JSON can hold.
        self._reports[interview_id] = json.loads(json.dumps(report))

    async def get_report(self, interview_id: str) -> dict | None:
        report = self._reports.get(interview_id)
        return None if report is None else copy.deepcopy(report)

    async def seconds_used_today(self) -> int:
        start, end = _utc_day(self._clock())
        total = 0
        for i in self._interviews.values():
            if i.mode != "demo":
                continue
            if i.status == "live":
                total += i.cap_s
            elif i.status in _FINISHED and start <= i.created_at < end:
                total += i.duration_s or 0
        return total

    async def starts_today(self, visitor_hash: str, ip_hash: str) -> int:
        start, end = _utc_day(self._clock())
        return sum(
            1
            for i in self._interviews.values()
            if i.mode == "demo"
            and i.status != "abandoned"
            and start <= i.created_at < end
            and (i.visitor_hash == visitor_hash or i.ip_hash == ip_hash)
        )

    async def ping(self) -> None:
        return None

    async def delete_demo_data_older_than(self, days: int) -> int:
        cutoff = self._clock() - timedelta(days=days)
        old = [
            i.id for i in self._interviews.values() if i.mode == "demo" and i.created_at < cutoff
        ]
        for id in old:
            del self._interviews[id]
            self._turns.pop(id, None)
            self._reports.pop(id, None)
        return len(old)


async def create_pool(dsn: str, **options: Any) -> asyncpg.Pool:
    """Open the connection pool for PostgresStore. `options` go to asyncpg.create_pool.

    statement_cache_size=0 turns off asyncpg's cache of prepared statements. Supabase's
    connection pooler can run in transaction mode, where each transaction may be given a
    different server connection, so a statement prepared earlier is not there next time and
    asyncpg fails with "prepared statement does not exist". With the cache off this works in
    transaction mode, session mode and against a plain Postgres. It is not an option here.

    The pool is small because the app is one process making a few short queries, and Supabase's
    pooler limits how many connections it hands out.
    """
    settings = {"min_size": 1, "max_size": 5, **options, "statement_cache_size": 0}
    return await asyncpg.create_pool(dsn, **settings)


class PostgresStore:
    """The store on Postgres, over a pool from `create_pool`. Needs the tables from
    server/migrations (see migrate.py).

    JSON columns are encoded and decoded here, not by the pool, so any asyncpg pool works.
    """

    def __init__(self, pool: asyncpg.Pool, clock: Clock = utc_now) -> None:
        self._pool = pool
        self._clock = clock

    async def create_interview(self, i: Interview) -> None:
        _check_new(i)
        try:
            await self._pool.execute(
                f"insert into interviews ({_INTERVIEW_COLUMNS})"
                " values ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9, $10, $11)",
                i.id,
                i.mode,
                i.status,
                i.visitor_hash,
                i.ip_hash,
                i.cap_s,
                json.dumps(i.questions),
                i.created_at,
                i.vapi_call_id,
                i.ended_reason,
                i.duration_s,
            )
        except asyncpg.UniqueViolationError:
            raise ValueError(f"interview {i.id} already exists") from None
        except (asyncpg.IntegrityConstraintViolationError, asyncpg.DataError) as exc:
            raise _refusal(f"interview {i.id}", exc) from None

    async def get_interview(self, id: str) -> Interview | None:
        row = await self._pool.fetchrow(
            f"select {_INTERVIEW_COLUMNS} from interviews where id = $1", id
        )
        return None if row is None else _interview(row)

    async def get_interview_by_call(self, call_id: str) -> Interview | None:
        row = await self._pool.fetchrow(
            f"select {_INTERVIEW_COLUMNS} from interviews where vapi_call_id = $1", call_id
        )
        return None if row is None else _interview(row)

    async def list_live_interviews(self) -> list[Interview]:
        rows = await self._pool.fetch(
            f"select {_INTERVIEW_COLUMNS} from interviews where status = 'live'"
        )
        return [_interview(row) for row in rows]

    async def update_interview(self, id: str, **changes: Any) -> None:
        _check_fields(changes)
        if not changes:
            return
        assignments = []
        values = []
        for n, (name, value) in enumerate(changes.items(), start=2):
            # `name` was checked against the Interview fields above, so it is safe in the SQL.
            if name == "questions":
                assignments.append(f"{name} = ${n}::jsonb")
                values.append(json.dumps(value))
            else:
                assignments.append(f"{name} = ${n}")
                values.append(value)
        try:
            result = await self._pool.execute(
                f"update interviews set {', '.join(assignments)} where id = $1", id, *values
            )
        except (asyncpg.IntegrityConstraintViolationError, asyncpg.DataError) as exc:
            raise _refusal(f"update of interview {id}", exc) from None
        if result == "UPDATE 0":
            raise KeyError(id)

    async def add_turn(self, interview_id: str, t: Turn) -> None:
        try:
            await self._pool.execute(
                "insert into turns (interview_id, idx, speaker, text, question_idx, interrupted)"
                " values ($1, $2, $3, $4, $5, $6)",
                interview_id,
                t.idx,
                t.speaker,
                t.text,
                t.question_idx,
                t.interrupted,
            )
        except asyncpg.ForeignKeyViolationError as exc:
            raise KeyError(interview_id) from exc
        except asyncpg.UniqueViolationError as exc:
            raise ValueError(
                f"interview {interview_id} already has a turn with idx {t.idx}"
            ) from exc

    async def list_turns(self, interview_id: str) -> list[Turn]:
        rows = await self._pool.fetch(
            "select idx, speaker, text, question_idx, interrupted from turns"
            " where interview_id = $1 order by idx",
            interview_id,
        )
        return [Turn(**row) for row in rows]

    async def save_report(self, interview_id: str, report: dict) -> None:
        """Keep the report as jsonb, and copy its scores and quotes into their own tables. All
        or nothing: a report that cannot be saved leaves the earlier one in place.

        The tables hold no uniqueness rule, because they are copies of the report and the report
        can repeat an item. Whatever the grader returns that is shaped like a report is saved.
        """
        score_rows = _score_rows(report)
        try:
            async with self._pool.acquire() as conn, conn.transaction():
                result = await conn.execute(
                    "update interviews set report = $2::jsonb where id = $1",
                    interview_id,
                    json.dumps(report),
                )
                if result == "UPDATE 0":
                    raise KeyError(interview_id)
                # Deleting the scores deletes their quotes too (on delete cascade).
                await conn.execute("delete from scores where interview_id = $1", interview_id)
                for question_idx, item, score, rationale, status, quotes in score_rows:
                    score_id = await conn.fetchval(
                        "insert into scores"
                        " (interview_id, question_idx, item, score, rationale, status)"
                        " values ($1, $2, $3, $4, $5, $6) returning id",
                        interview_id,
                        question_idx,
                        item,
                        score,
                        rationale,
                        status,
                    )
                    await conn.executemany(
                        "insert into quotes (score_id, text, verified) values ($1, $2, $3)",
                        [(score_id, text, verified) for text, verified in quotes],
                    )
        except (asyncpg.IntegrityConstraintViolationError, asyncpg.DataError) as exc:
            raise _refusal(f"report for interview {interview_id}", exc) from None

    async def get_report(self, interview_id: str) -> dict | None:
        report = await self._pool.fetchval(
            "select report from interviews where id = $1", interview_id
        )
        return None if report is None else json.loads(report)

    async def seconds_used_today(self) -> int:
        start, end = _utc_day(self._clock())
        return await self._pool.fetchval(
            "select coalesce(sum(case when status = 'live' then cap_s"
            "                         else coalesce(duration_s, 0) end), 0)"
            " from interviews"
            " where mode = 'demo'"
            "   and (status = 'live'"
            "        or (status = any($1::text[]) and created_at >= $2 and created_at < $3))",
            list(_FINISHED),
            start,
            end,
        )

    async def starts_today(self, visitor_hash: str, ip_hash: str) -> int:
        start, end = _utc_day(self._clock())
        return await self._pool.fetchval(
            "select count(*) from interviews"
            " where mode = 'demo' and status <> 'abandoned'"
            "   and created_at >= $1 and created_at < $2"
            "   and (visitor_hash = $3 or ip_hash = $4)",
            start,
            end,
            visitor_hash,
            ip_hash,
        )

    async def ping(self) -> None:
        await self._pool.execute("select 1")

    async def delete_demo_data_older_than(self, days: int) -> int:
        cutoff = self._clock() - timedelta(days=days)
        # The turns, scores and quotes go with their interview (on delete cascade).
        return await self._pool.fetchval(
            "with gone as (delete from interviews where mode = 'demo' and created_at < $1"
            " returning 1) select count(*) from gone",
            cutoff,
        )


def _interview(row: asyncpg.Record) -> Interview:
    return Interview(**{**row, "questions": json.loads(row["questions"])})


def _score_rows(report: dict) -> list[tuple]:
    """Flatten a report into (question_idx, item, score, rationale, status, quotes) rows, where
    quotes is a list of (text, verified). Quotes the verifier dropped are kept with verified
    False, so the share of quotes that verify can be measured later."""
    rows = []
    try:
        for question in report.get("questions", []):
            for item in question["items"]:
                quotes = [(text, True) for text in item.get("quotes", [])]
                quotes += [(text, False) for text in item.get("dropped_quotes", [])]
                rows.append(
                    (
                        question["question_idx"],
                        item["item"],
                        item["score"],
                        item["rationale"],
                        item["status"],
                        quotes,
                    )
                )
    except KeyError as exc:
        raise ValueError(f"report is missing {exc}") from exc
    return rows
