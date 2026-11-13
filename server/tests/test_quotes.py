"""The quote locator: where a quote the grader cited sits in the candidate's transcribed words,
and whether it is close enough to keep (Review Focus 2).

The two tables, FOUND and NOT_FOUND, run against one answer, split on spaces into 68 words the
way a transcriber writes them, and their expected values are the plan's. Each rejected case names
the rule that rejects it. The guarded ones each catch a different wrong build: one that reads
only the transcript's side of an edit, one that reads only the quote's side, one that compares
the two sides as bags of words, and one whose walk back stops at the table's edge.
"""

import ast
import sys
from pathlib import Path

import pytest

from mockingbird.quotes import (
    FILLERS,
    FIRST_PERSON,
    NEGATIONS,
    QuoteMatch,
    locate_quote,
    span_text,
    tokenize,
)

ANSWER = (
    "So, um, I led the migration of our billing service to the new queue, and uh I did not "
    "skip the load test. We cut errors from 12% to 0.4% in two weeks, like, you know, which was "
    "much earlier than we planned. - Latency dropped by twenty-five percent after the second "
    "release. I'm proud that I owned the fix end to end. The fix shipped on Friday."
)
WORDS = ANSWER.split()


def test_the_answer_is_the_68_words_the_cases_count():
    assert len(WORDS) == 68
    assert (WORDS[0], WORDS[43], WORDS[53]) == ("So,", "-", "I'm")


@pytest.mark.parametrize(
    ("text", "tokens"),
    [
        ("e-commerce", ["ecommerce"]),
        ("twenty-five percent", ["twentyfive", "percent"]),
        ("12%", ["12", "percent"]),
        ("0.4%", ["0", "4", "percent"]),
        ("can't", ["can", "not"]),
        ("cannot", ["can", "not"]),
        ("won't", ["will", "not"]),
        ("didn't", ["did", "not"]),
        ("I'm", ["i", "am"]),
        ("we've", ["we", "have"]),
        ("they'll", ["they", "will"]),
        ("You're", ["you", "are"]),
        # An em dash splits two words. Phase 1's normalize deleted it and made one word.
        ("cause—and", ["cause", "and"]),
        ("O\u2019Brien's", ["obriens"]),  # a curly apostrophe, then a straight one
        ("'my team'", ["my", "team"]),  # a quote mark before a word is not a contraction
        ("…", []),
    ],
)
def test_tokenize(text, tokens):
    assert tokenize(text) == tokens


@pytest.mark.parametrize(
    "apostrophe",
    ["\u2019", "\u2018", "\u02bc"],
    ids=["right-single-quote", "left-single-quote", "modifier-letter-apostrophe"],
)
def test_every_apostrophe_works_like_the_straight_one(apostrophe):
    # The grader may copy "I'm" with a curly apostrophe. The modifier letter apostrophe is not
    # Unicode punctuation at all, so only the explicit list of apostrophes deletes it.
    text = f"I{apostrophe}m sure O{apostrophe}Brien didn{apostrophe}t"
    assert tokenize(text) == ["i", "am", "sure", "obrien", "did", "not"]


@pytest.mark.parametrize("hyphen", ["\u2010", "\u2011"], ids=["hyphen", "non-breaking-hyphen"])
def test_a_unicode_hyphen_inside_a_word_is_deleted_like_the_ascii_one(hyphen):
    assert tokenize(f"e{hyphen}commerce") == ["ecommerce"]


# (quote, first, last, ratio, shown): `shown` is span_text of the match, the transcript's words.
FOUND = [
    pytest.param(
        "I led the migration of our billing service",
        2,
        9,
        1.0,
        "I led the migration of our billing service",
        id="exact",
    ),
    pytest.param(
        "“I Led the migration — of our billing service.”",
        2,
        9,
        1.0,
        "I led the migration of our billing service",
        id="quote-marks-capitals-and-a-dash",
    ),
    pytest.param(
        "So I led the migration",
        0,
        5,
        1.0,
        "So, um, I led the migration",
        id="um-dropped",
    ),
    pytest.param(
        "and I did not skip the load test",
        14,
        22,
        1.0,
        "and uh I did not skip the load test.",
        id="uh-dropped",
    ),
    pytest.param(
        "I am proud that I owned the fix",
        53,
        59,
        1.0,
        "I'm proud that I owned the fix",
        id="contraction-spelled-out",
    ),
    pytest.param("migration", 5, 5, 1.0, "migration", id="one-word"),
    pytest.param("the fix", 58, 59, 1.0, "the fix", id="said-twice-the-earlier-one"),
    pytest.param(
        "the fix shipped on Friday",
        63,
        67,
        1.0,
        "The fix shipped on Friday.",
        id="the-second-the-fix",
    ),
    pytest.param(
        "than we planned. Latency dropped by twenty-five percent",
        40,
        48,
        1.0,
        "than we planned. - Latency dropped by twenty-five percent",
        id="a-lone-dash-costs-nothing",
    ),
    pytest.param(
        "We cut errors from 12% to 0.4% in two weeks, which was much earlier than we planned",
        23,
        42,
        0.87,
        "We cut errors from 12% to 0.4% in two weeks, like, you know, which was much earlier "
        "than we planned.",
        id="like-you-know-left-out",
    ),
    pytest.param(
        "which was much earlier than we expected",
        36,
        42,
        0.857,
        "which was much earlier than we planned.",
        id="last-word-changed",
    ),
    pytest.param(
        "I personally owned the fix end to end",
        56,
        62,
        0.875,
        "I owned the fix end to end.",
        id="word-added",
    ),
    pytest.param(
        "the migration our billing",
        4,
        8,
        0.8,
        "the migration of our billing",
        id="exactly-at-the-80-percent-line",
    ),
    # No word list can catch an antonym, which is why the report shows the transcript's words.
    pytest.param(
        "Latency rose by twenty-five percent after the second release",
        44,
        52,
        0.889,
        "Latency dropped by twenty-five percent after the second release.",
        id="antonym-shows-what-was-said",
    ),
]


@pytest.mark.parametrize(("quote", "first", "last", "ratio", "shown"), FOUND)
def test_a_quote_the_candidate_said_is_found(quote, first, last, ratio, shown):
    match = locate_quote(quote, WORDS)
    assert match == QuoteMatch(first=first, last=last, ratio=ratio)
    assert span_text(WORDS, match) == shown


NOT_FOUND = [
    pytest.param("I did skip the load test", id="drops-not"),
    pytest.param("We cut errors from 15% to 0.4% in two weeks", id="changes-a-number"),
    pytest.param("in three weeks, like, you know, which was much earlier", id="three-for-two"),
    pytest.param("We led the migration of our billing service to the new queue", id="we-for-i"),
    pytest.param(
        "Latency dropped by thirty-five percent after the second release",
        id="compound-number",
    ),
    pytest.param(
        "Latency dropped by twenty-five percent after the third release",
        id="ordinal",
    ),
    pytest.param("which was not much earlier than we planned", id="adds-not"),
    # Guarded on the quote's side only.
    pytest.param("which was never earlier than we planned", id="never-for-much"),
    # Guarded on the transcript's side only.
    pytest.param("which was much earlier than they planned", id="they-for-we"),
    # Ratio 0.826: only the guard stops it, and only if it compares positions, not bags of words.
    pytest.param(
        "We cut errors from 0.4% to 12% in two weeks, like, you know, which was much earlier "
        "than we planned",
        id="two-numbers-swapped",
    ),
    # Ratio 0.9: the added "no" sits on the alignment table's edge.
    pytest.param("No, so I led the migration of our billing service", id="adds-no-at-the-start"),
    pytest.param(
        "in two weeks, which was much earlier than we planned",
        id="3-of-13-left-out-is-under-80-percent",
    ),
    pytest.param("I led the migration and we cut errors", id="a-splice-of-two-places"),
    pytest.param("I rewrote the scheduler in Rust over a weekend", id="invented"),
    pytest.param("skip the test", id="1-of-4-left-out-is-75-percent"),
    pytest.param("skip the unit test", id="1-edit-in-4-is-75-percent"),
    pytest.param("um", id="the-span-is-all-filler"),
    pytest.param("", id="empty"),
    pytest.param("… —", id="no-tokens"),
]


@pytest.mark.parametrize("quote", NOT_FOUND)
def test_a_quote_that_changes_the_meaning_or_was_never_said_is_none(quote):
    assert locate_quote(quote, WORDS) is None


# The grader left out the word beside the quote's first or last word. Two readings cost one edit
# each: that word was left out, or the quote's own first or last word was changed into it. The
# one that matches more words wins, in the table's cells for the first word and in the choice of
# end for the last. Without it, "I" would read as changed into "personally" and the guard would
# drop the quote, and "customer" would be cut off what the report shows.
@pytest.mark.parametrize(
    ("said", "quote"),
    [
        pytest.param(
            "I personally owned the rollout plan.",
            "I owned the rollout plan",
            id="left-out-after-the-first-word",
        ),
        pytest.param(
            "I talked to every single customer.",
            "I talked to every customer",
            id="left-out-before-the-last-word",
        ),
    ],
)
def test_of_two_equally_cheap_readings_the_one_that_matches_more_words_wins(said, quote):
    words = said.split()
    match = locate_quote(quote, words)
    assert match == QuoteMatch(first=0, last=5, ratio=0.833)
    assert span_text(words, match) == said


def test_fillers_that_match_fillers_are_none():
    assert locate_quote("Um, uh", "Um, uh, I do not know.".split()) is None


def test_a_filler_that_is_the_last_word_is_none():
    assert locate_quote("um", "I think so, um".split()) is None


def test_no_words_is_none():
    assert locate_quote("anything", []) is None


def test_a_quote_far_longer_than_the_transcript_is_none():
    quote = (
        "I single handedly rewrote our whole payments platform in Go over one long weekend and "
        "then trained every new hire on it while running the hiring loop for the team"
    )
    words = "We shipped it on Friday.".split()
    assert (len(quote.split()), len(words)) == (30, 5)
    assert locate_quote(quote, words) is None


def test_span_text_joins_the_words_with_single_spaces_as_they_were_written():
    assert span_text(WORDS, QuoteMatch(first=0, last=3, ratio=1.0)) == "So, um, I led"


def test_the_word_lists_are_the_plans():
    assert FILLERS == set("um uh uhm erm er ah hmm mm mhm".split())
    assert NEGATIONS == set("not no never nothing nobody none neither nor without".split())
    # "you" is not first person, so a dropped "you know" still passes.
    assert FIRST_PERSON == set("i me my mine myself we us our ours ourselves".split())


def test_the_module_imports_only_the_standard_library():
    module_path = Path(sys.modules["mockingbird.quotes"].__file__)
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            # A relative import is one of ours, never the standard library.
            imported.add("." if node.level else node.module.split(".")[0])
    assert imported - sys.stdlib_module_names == set()
