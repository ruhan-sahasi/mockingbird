"""Loading the behavioral question bank (server/data/questions.yaml)."""

import random
from pathlib import Path

import yaml

from mockingbird.plan import Question


def load_questions(path: Path, k: int, rng: random.Random) -> list[Question]:
    """Pick k distinct questions from the bank at `path`, spread across competencies.

    The bank is grouped by competency. Each round visits the competencies in a random order and
    takes one random question from each, so for k up to the number of competencies no two picked
    questions share one. Past that, a second round starts and questions are still distinct.
    Passing a seeded `rng` makes the pick repeatable.
    """
    entries = yaml.safe_load(path.read_text(encoding="utf-8"))
    bank = [Question(id=e["id"], text=e["text"], competency=e["competency"]) for e in entries]
    if k > len(bank):
        raise ValueError(f"asked for {k} questions but the bank only has {len(bank)}")

    by_competency: dict[str, list[Question]] = {}
    for question in bank:
        by_competency.setdefault(question.competency, []).append(question)
    for group in by_competency.values():
        rng.shuffle(group)

    picked: list[Question] = []
    while len(picked) < k:
        competencies = [name for name, group in by_competency.items() if group]
        rng.shuffle(competencies)
        for name in competencies[: k - len(picked)]:
            picked.append(by_competency[name].pop())
    return picked
