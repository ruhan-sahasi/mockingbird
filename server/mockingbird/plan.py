"""The interview plan: a pure state machine that decides what the interviewer does next.

Code owns the shape of the interview (which question, how many follow-ups, when to stop) so a
language model can never talk its way past the clock. Claude only words each move.

Nothing here does I/O or reads a clock. The caller passes in how much time has passed and how
long the candidate's last answer was, which makes every rule testable with plain numbers.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal


@dataclass(frozen=True)
class Question:
    id: str
    text: str
    competency: str


@dataclass(frozen=True)
class PlanConfig:
    total_s: int  # the interview's time cap, in seconds
    main_questions: int  # how many main questions to ask if time allows
    max_follow_ups: int  # follow-ups allowed on each main question
    wrap_up_reserve_s: int  # at or below this many seconds left, stop and wrap up
    new_question_min_s: int  # a new main question needs at least this many seconds left
    short_answer_words: int  # an answer under this many words gets an "expand" probe


DEMO = PlanConfig(
    total_s=300,
    main_questions=1,
    max_follow_ups=2,
    wrap_up_reserve_s=40,
    new_question_min_s=0,
    short_answer_words=25,
)
FULL = PlanConfig(
    total_s=1200,
    main_questions=3,
    max_follow_ups=2,
    wrap_up_reserve_s=90,
    new_question_min_s=240,
    short_answer_words=25,
)


class MoveKind(StrEnum):
    ASK_QUESTION = "ASK_QUESTION"
    FOLLOW_UP = "FOLLOW_UP"
    WRAP_UP = "WRAP_UP"
    DONE = "DONE"


@dataclass(frozen=True)
class Move:
    kind: MoveKind
    # The main question this move belongs to. For ASK_QUESTION it is the question being asked.
    question_idx: int
    # Set only for ASK_QUESTION: the question the interviewer must ask next.
    question: Question | None = None
    # Set only for FOLLOW_UP: "expand" after a short answer, else "weakest_part".
    probe: Literal["expand", "weakest_part"] | None = None


class InterviewPlan:
    def __init__(self, config: PlanConfig, questions: Sequence[Question]):
        if len(questions) < config.main_questions:
            raise ValueError(
                f"need at least {config.main_questions} questions, got {len(questions)}"
            )
        self._config = config
        self._questions = list(questions[: config.main_questions])
        self._asked = 0  # main questions asked so far, including the one in the opening line
        self._follow_ups = 0  # follow-ups used on the current main question
        self._finished = False

    @property
    def finished(self) -> bool:
        return self._finished

    @property
    def current_question_idx(self) -> int:
        return max(self._asked - 1, 0)

    def opening_line(self) -> str:
        """The first thing the interviewer says. It asks question 0, so that counts as asked."""
        self._asked = max(self._asked, 1)
        minutes = self._config.total_s // 60
        return (
            f"Hi, I'm Mockingbird, and I'll be your interviewer. This is a {minutes} minute "
            f"behavioral interview, and you can cut me off any time. {self._questions[0].text}"
        )

    def next_move(self, elapsed_s: float, answer_words: int) -> Move:
        """Pick the next move, given the time elapsed and the words in the candidate's answer."""
        if self._asked == 0:
            # Without the opening line, question 0 was never asked and there is nothing to
            # follow up on. That is a caller bug, so fail loudly instead of guessing.
            raise RuntimeError("call opening_line() before next_move()")
        cfg = self._config
        idx = self.current_question_idx

        if self._finished:
            return Move(MoveKind.DONE, idx)

        time_left = cfg.total_s - elapsed_s
        if time_left <= cfg.wrap_up_reserve_s:
            return self._wrap_up()

        if self._follow_ups < cfg.max_follow_ups:
            self._follow_ups += 1
            probe = "expand" if answer_words < cfg.short_answer_words else "weakest_part"
            return Move(MoveKind.FOLLOW_UP, idx, probe=probe)

        if self._asked < cfg.main_questions and time_left >= cfg.new_question_min_s:
            question = self._questions[self._asked]
            self._asked += 1
            self._follow_ups = 0
            return Move(MoveKind.ASK_QUESTION, self.current_question_idx, question)

        return self._wrap_up()

    def _wrap_up(self) -> Move:
        self._finished = True
        return Move(MoveKind.WRAP_UP, self.current_question_idx)
