"""Plain data types shared across the server: the interview record and its spoken lines."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

Mode = Literal["demo", "full"]
Status = Literal["created", "live", "ended", "graded", "abandoned"]


@dataclass
class Turn:
    """One spoken line of the interview, in the order it was said."""

    idx: int  # 0 for the opening line, then counting up by one
    speaker: Literal["interviewer", "candidate"]
    text: str
    # The main question this line belongs to. A candidate's answer belongs to the question that
    # was in progress when they said it, even if that answer is what moves the interview on.
    question_idx: int
    interrupted: bool = False  # True if the line was cut off before it was fully produced


@dataclass
class Interview:
    """One interview, from the moment it is created until its report is graded."""

    id: str
    mode: Mode
    status: Status
    visitor_hash: str
    ip_hash: str
    cap_s: int  # the longest the call may run, in seconds
    questions: list[str]  # the main question texts, in the order they are asked
    created_at: datetime  # always timezone-aware; the daily limits count UTC days from it
    # While a call is live, anyone who knows this id can listen to it or control it, so it is a
    # credential: keep it out of logs, error text and anything sent to the browser. repr=False
    # keeps it out of tracebacks and log lines that print the whole interview.
    vapi_call_id: str | None = field(default=None, repr=False)
    ended_reason: str | None = None
    duration_s: int | None = None
