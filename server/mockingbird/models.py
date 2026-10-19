"""Plain data types shared across the server. Task 7 adds the interview record to this file."""

from dataclasses import dataclass
from typing import Literal


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
