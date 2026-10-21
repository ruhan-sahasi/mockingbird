import random
from collections import Counter
from pathlib import Path

import pytest
import yaml

from mockingbird.plan import DEMO, FULL, InterviewPlan, Move, MoveKind, PlanConfig, Question
from mockingbird.questions import load_questions

QUESTIONS_YAML = Path(__file__).resolve().parents[1] / "data" / "questions.yaml"
COMPETENCIES = {"ownership", "conflict", "failure", "ambiguity", "leadership", "impact"}


def qs(n: int) -> list[Question]:
    return [Question(f"q{i}", f"Question number {i}.", "ownership") for i in range(n)]


# --- the plan: moves ---------------------------------------------------------


def test_demo_follows_up_twice_then_wraps_up():
    p = InterviewPlan(DEMO, qs(3))
    p.opening_line()
    assert p.next_move(30, 80).kind is MoveKind.FOLLOW_UP
    assert p.next_move(60, 80).kind is MoveKind.FOLLOW_UP
    assert p.next_move(90, 80).kind is MoveKind.WRAP_UP
    assert p.next_move(95, 10).kind is MoveKind.DONE


def test_short_answer_probes_expand():
    p = InterviewPlan(DEMO, qs(1))
    p.opening_line()
    assert p.next_move(20, 10).probe == "expand"
    assert p.next_move(40, 60).probe == "weakest_part"


def test_answer_length_threshold_is_exclusive():
    p = InterviewPlan(DEMO, qs(1))
    p.opening_line()
    assert p.next_move(20, DEMO.short_answer_words - 1).probe == "expand"
    assert p.next_move(40, DEMO.short_answer_words).probe == "weakest_part"


def test_wraps_up_inside_reserve_even_with_follow_ups_left():
    p = InterviewPlan(DEMO, qs(1))
    p.opening_line()
    assert p.next_move(261, 80).kind is MoveKind.WRAP_UP


def test_reserve_boundary_is_inclusive():
    # 300 - 260 = 40 s left, exactly the reserve: wrap up. A hair more time: keep going.
    inside = InterviewPlan(DEMO, qs(1))
    inside.opening_line()
    assert inside.next_move(260, 80).kind is MoveKind.WRAP_UP
    outside = InterviewPlan(DEMO, qs(1))
    outside.opening_line()
    assert outside.next_move(259.5, 80).kind is MoveKind.FOLLOW_UP


def test_full_moves_to_next_question_after_two_follow_ups():
    p = InterviewPlan(FULL, qs(3))
    p.opening_line()
    p.next_move(60, 80)
    p.next_move(120, 80)
    m = p.next_move(180, 80)
    assert m.kind is MoveKind.ASK_QUESTION and m.question_idx == 1


def test_full_skips_new_question_when_under_min_time():
    p = InterviewPlan(FULL, qs(3))
    p.opening_line()
    p.next_move(900, 80)
    p.next_move(950, 80)
    assert p.next_move(1000, 80).kind is MoveKind.WRAP_UP


def test_new_question_min_time_boundary_is_inclusive():
    # 1200 - 960 = 240 s left, exactly the minimum: ask. Half a second less: wrap up.
    enough = InterviewPlan(FULL, qs(3))
    enough.opening_line()
    enough.next_move(900, 80)
    enough.next_move(930, 80)
    assert enough.next_move(960, 80).kind is MoveKind.ASK_QUESTION
    short = InterviewPlan(FULL, qs(3))
    short.opening_line()
    short.next_move(900, 80)
    short.next_move(930, 80)
    assert short.next_move(960.5, 80).kind is MoveKind.WRAP_UP


def test_ask_question_carries_the_next_question_and_resets_follow_ups():
    questions = qs(3)
    p = InterviewPlan(FULL, questions)
    p.opening_line()
    p.next_move(60, 80)
    p.next_move(120, 80)
    ask = p.next_move(180, 80)
    assert ask == Move(MoveKind.ASK_QUESTION, 1, questions[1], None)
    assert p.current_question_idx == 1
    # The new question gets its own two follow-ups.
    assert p.next_move(240, 80).kind is MoveKind.FOLLOW_UP
    assert p.next_move(300, 80).kind is MoveKind.FOLLOW_UP
    assert p.next_move(360, 80).kind is MoveKind.ASK_QUESTION


def test_only_ask_question_carries_a_question():
    p = InterviewPlan(DEMO, qs(1))
    p.opening_line()
    assert p.next_move(30, 80) == Move(MoveKind.FOLLOW_UP, 0, None, "weakest_part")
    p.next_move(60, 80)
    assert p.next_move(90, 80) == Move(MoveKind.WRAP_UP, 0, None, None)
    assert p.next_move(95, 80) == Move(MoveKind.DONE, 0, None, None)


def test_finished_flag_and_current_question_idx():
    p = InterviewPlan(DEMO, qs(1))
    assert p.finished is False
    assert p.current_question_idx == 0
    p.opening_line()
    p.next_move(30, 80)
    p.next_move(60, 80)
    assert p.finished is False
    assert p.next_move(90, 80).kind is MoveKind.WRAP_UP
    assert p.finished is True
    assert p.current_question_idx == 0


def test_extra_questions_beyond_main_questions_are_never_asked():
    p = InterviewPlan(DEMO, qs(5))
    p.opening_line()
    p.next_move(30, 80)
    p.next_move(60, 80)
    # DEMO has one main question, so there is never an ASK_QUESTION.
    assert p.next_move(90, 80).kind is MoveKind.WRAP_UP


def test_too_few_questions_raises():
    with pytest.raises(ValueError):
        InterviewPlan(FULL, qs(2))


def test_next_move_before_opening_line_raises():
    # Without the opening line, question 0 has not been asked, so there is nothing to follow up.
    p = InterviewPlan(DEMO, qs(1))
    with pytest.raises(RuntimeError):
        p.next_move(10, 50)


def test_calling_opening_line_twice_does_not_count_a_second_question():
    p = InterviewPlan(FULL, qs(3))
    p.opening_line()
    p.opening_line()
    assert p.current_question_idx == 0
    p.next_move(60, 80)
    p.next_move(120, 80)
    assert p.next_move(180, 80).question_idx == 1


# --- the plan: the properties that must hold for any input ------------------


@pytest.mark.parametrize("config", [DEMO, FULL], ids=["demo", "full"])
@pytest.mark.parametrize("max_step_s", [60, 240], ids=["steps_5_to_60s", "steps_5_to_240s"])
def test_never_more_than_max_follow_ups_property(config: PlanConfig, max_step_s: int):
    # 500 seeded random runs of (elapsed increasing, words 0..200) until DONE. The 60 s steps are
    # the usual pace of an interview. The 240 s steps make the clock, not the question count, end
    # the interview, so the reserve and the minimum-time rules run too.
    for seed in range(500):
        rng = random.Random(seed)
        plan = InterviewPlan(config, qs(3))
        plan.opening_line()
        elapsed = 0.0
        moves: list[Move] = []
        for _ in range(500):
            elapsed += rng.uniform(5, max_step_s)
            moves.append(plan.next_move(elapsed, rng.randint(0, 200)))
            if moves[-1].kind is MoveKind.DONE:
                break
        else:
            pytest.fail(f"seed {seed}: never reached DONE")

        kinds = [m.kind for m in moves]
        # Exactly one WRAP_UP, then DONE straight after it, and DONE nowhere else.
        assert kinds.count(MoveKind.WRAP_UP) == 1, f"seed {seed}: {kinds}"
        assert kinds[-2:] == [MoveKind.WRAP_UP, MoveKind.DONE], f"seed {seed}: {kinds}"
        assert MoveKind.DONE not in kinds[:-1], f"seed {seed}: {kinds}"
        # DONE is stable: asking again never starts anything new.
        for _ in range(3):
            elapsed += rng.uniform(5, max_step_s)
            assert plan.next_move(elapsed, 100).kind is MoveKind.DONE
        assert plan.finished
        # At most max_follow_ups per question.
        per_question = Counter(m.question_idx for m in moves if m.kind is MoveKind.FOLLOW_UP)
        assert all(n <= config.max_follow_ups for n in per_question.values()), f"seed {seed}"
        # New questions come in order and never beyond main_questions.
        asked = [m.question_idx for m in moves if m.kind is MoveKind.ASK_QUESTION]
        assert asked == list(range(1, len(asked) + 1)), f"seed {seed}"
        assert len(asked) < config.main_questions, f"seed {seed}"


# --- the plan: copy -----------------------------------------------------------


def test_opening_line_copy():
    p = InterviewPlan(
        DEMO, [Question("q1", "Tell me about a time you disagreed with a teammate.", "conflict")]
    )
    assert p.opening_line() == (
        "Hi, I'm Mockingbird, and I'll be your interviewer. This is a 5 minute "
        "behavioral interview, and you can cut me off any time. "
        "Tell me about a time you disagreed with a teammate."
    )


def test_opening_line_says_the_minutes_for_the_mode():
    assert "This is a 20 minute behavioral interview" in InterviewPlan(FULL, qs(3)).opening_line()


def test_presets():
    assert DEMO == PlanConfig(300, 1, 2, 40, 0, 25)
    assert FULL == PlanConfig(1200, 3, 2, 90, 240, 25)


# --- loading questions --------------------------------------------------------


def test_question_bank_covers_every_competency_and_reads_aloud():
    entries = yaml.safe_load(QUESTIONS_YAML.read_text(encoding="utf-8"))
    assert len(entries) >= 12
    assert len({e["id"] for e in entries}) == len(entries)
    assert Counter(e["competency"] for e in entries).keys() == COMPETENCIES
    assert all(n >= 2 for n in Counter(e["competency"] for e in entries).values())
    for e in entries:
        text = e["text"]
        # These are spoken aloud: one sentence, no dashes, nothing to read out as punctuation.
        assert text.endswith((".", "?")), text
        assert text.count(".") + text.count("?") == 1, text
        assert not any(ch in text for ch in "—–;:()*#_`"), text


@pytest.mark.parametrize("k", range(1, 7))
def test_load_questions_spreads_competencies_when_k_is_six_or_fewer(k: int):
    for seed in range(100):
        picked = load_questions(QUESTIONS_YAML, k, random.Random(seed))
        assert len(picked) == k
        assert len({q.id for q in picked}) == k, f"seed {seed}: repeated question"
        assert len({q.competency for q in picked}) == k, f"seed {seed}: repeated competency"


def test_load_questions_picks_distinct_questions_beyond_six():
    picked = load_questions(QUESTIONS_YAML, 9, random.Random(3))
    assert len({q.id for q in picked}) == 9


def test_load_questions_is_deterministic_for_a_seed_and_varies_across_seeds():
    first = load_questions(QUESTIONS_YAML, 3, random.Random(7))
    assert first == load_questions(QUESTIONS_YAML, 3, random.Random(7))
    picks = {
        tuple(q.id for q in load_questions(QUESTIONS_YAML, 3, random.Random(seed)))
        for seed in range(20)
    }
    assert len(picks) > 1


def test_load_questions_spreads_even_when_one_competency_dominates(tmp_path: Path):
    bank = tmp_path / "bank.yaml"
    entries = [("ownership", i) for i in range(1, 6)] + [("conflict", 1), ("failure", 1)]
    bank.write_text(
        "".join(
            f'- id: {c}-{i}\n  text: "Tell me about {c} number {i}."\n  competency: {c}\n'
            for c, i in entries
        ),
        encoding="utf-8",
    )
    for seed in range(50):
        picked = load_questions(bank, 3, random.Random(seed))
        assert {q.competency for q in picked} == {"ownership", "conflict", "failure"}


def test_load_questions_returns_question_objects():
    q = load_questions(QUESTIONS_YAML, 1, random.Random(0))[0]
    assert isinstance(q, Question)
    assert q.id and q.text and q.competency in COMPETENCIES


def test_load_questions_too_many_requested_raises():
    with pytest.raises(ValueError):
        load_questions(QUESTIONS_YAML, 1000, random.Random(0))
