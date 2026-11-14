"""The quote locator: where a quote the grader cited sits in the candidate's transcribed words,
and whether it is close enough to keep (Review Focus 2).

FOUND and NOT_FOUND run against one answer, split on spaces into 68 words the way a transcriber
writes them. Most rows after them bring their own words, for what that answer cannot show. Every
row and its result is the plan's. Each rejected row names the rule that rejects it, and each row
the guard rejects is run again with the guard switched off, where it must be found, so no guarded
row can pass by failing the 80% line instead.

The guarded rows each catch a different wrong build: one that reads only the transcript's side of
an edit, one that reads only the quote's side, one that compares the two sides as bags of words,
one that misses an edit on the alignment table's edge, one whose match can start or stop inside a
word, one without the edge rule, whose match stops short of a changed word, one whose edge rule
reads past a left-out quote word but not past a changed one, one whose edge rule reads a single
token of the word outside the match, and one that reads a single cheapest alignment, the one that
leaves a swapped "not" alone.
"""

import ast
import dataclasses
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
        # A hyphen splits like any other punctuation, so no guarded word hides in a compound.
        ("e-commerce", ["e", "commerce"]),
        ("twenty-five percent", ["twenty", "five", "percent"]),
        ("can't-do", ["can", "not", "do"]),
        ("We-we", ["we", "we"]),
        ("12-15", ["12", "15"]),
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
        # "'d" is "would" or "had", so it stays a bare "d", and the pronoun stands alone.
        ("I'd", ["i", "d"]),
        ("let's", ["let", "us"]),
        ("Let\u2019s", ["let", "us"]),  # a curly apostrophe
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
    text = "I'm sure O'Brien didn't, so let's say we'd".replace("'", apostrophe)
    assert tokenize(text) == "i am sure obrien did not so let us say we d".split()


@pytest.mark.parametrize("hyphen", ["\u2010", "\u2011"], ids=["hyphen", "non-breaking-hyphen"])
def test_a_unicode_hyphen_splits_a_word_like_the_ascii_one(hyphen):
    assert tokenize(f"five{hyphen}person") == ["five", "person"]


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
        0.9,
        "Latency dropped by twenty-five percent after the second release.",
        id="antonym-shows-what-was-said",
    ),
    # Two readings cost one edit and match as many words: "So" in place of "that", or "So" never
    # said. Cells keep the one that starts earlier, so the match starts at "that".
    pytest.param(
        "So I owned the fix end to end",
        55,
        62,
        0.875,
        "that I owned the fix end to end.",
        id="word-added-at-the-start-reads-as-changed",
    ),
]


@pytest.mark.parametrize(("quote", "first", "last", "ratio", "shown"), FOUND)
def test_a_quote_the_candidate_said_is_found(quote, first, last, ratio, shown):
    match = locate_quote(quote, WORDS)
    assert match == QuoteMatch(first=first, last=last, ratio=ratio)
    assert span_text(WORDS, match) == shown


def _assert_none_and_only_the_guard_says_so(quote, words, unguarded, monkeypatch):
    assert locate_quote(quote, words) is None
    # With the guard off, a row the guard rejects is found where `unguarded` says, so it cannot
    # pass by failing the 80% line instead. A row that is not close enough stays None.
    monkeypatch.setattr("mockingbird.quotes._guarded", lambda token: False)
    assert locate_quote(quote, words) == (None if unguarded is None else QuoteMatch(*unguarded))


# (quote, unguarded): `unguarded` is (first, last, ratio) with the guard switched off, or None
# when the quote is not close enough even then.
NOT_FOUND = [
    pytest.param("I did skip the load test", (16, 22, 0.857), id="drops-not"),
    pytest.param(
        "We cut errors from 15% to 0.4% in two weeks", (23, 32, 0.923), id="changes-a-number"
    ),
    pytest.param(
        "in three weeks, like, you know, which was much earlier", (30, 39, 0.9), id="three-for-two"
    ),
    pytest.param(
        "We led the migration of our billing service to the new queue",
        (2, 13, 0.917),
        id="we-for-i",
    ),
    pytest.param(
        "Latency dropped by thirty-five percent after the second release",
        (44, 52, 0.9),
        id="compound-number",
    ),
    pytest.param(
        "Latency dropped by twenty-five percent after the third release",
        (44, 52, 0.9),
        id="ordinal",
    ),
    pytest.param("which was not much earlier than we planned", (36, 42, 0.875), id="adds-not"),
    # Guarded on the quote's side only.
    pytest.param("which was never earlier than we planned", (36, 42, 0.857), id="never-for-much"),
    # Guarded on the transcript's side only.
    pytest.param("which was much earlier than they planned", (36, 42, 0.857), id="they-for-we"),
    # Ratio 0.826: only the guard stops it, and only if it compares positions, not bags of words.
    pytest.param(
        "We cut errors from 0.4% to 12% in two weeks, like, you know, which was much earlier "
        "than we planned",
        (23, 42, 0.826),
        id="two-numbers-swapped",
    ),
    # Ratio 0.9: the added "no" sits on the alignment table's edge.
    pytest.param(
        "No, so I led the migration of our billing service",
        (0, 9, 0.9),
        id="adds-no-at-the-start",
    ),
    # The quote's first word replaces "I" or "We" with a word the guard does not know, so only
    # the transcript's side is guarded.
    pytest.param("He owned the fix end to end", (56, 62, 0.857), id="he-for-i-at-the-start"),
    pytest.param(
        "They cut errors from 12% to 0.4% in two weeks",
        (23, 32, 0.923),
        id="they-for-we-at-the-start",
    ),
    # "12%" quoted as "12": the "percent" left out is a number word.
    pytest.param(
        "We cut errors from 12 to 0.4% in two weeks", (23, 32, 0.923), id="drops-a-percent"
    ),
    pytest.param(
        "in two weeks, which was much earlier than we planned",
        None,
        id="3-of-13-left-out-is-under-80-percent",
    ),
    pytest.param("I led the migration and we cut errors", None, id="a-splice-of-two-places"),
    pytest.param("I rewrote the scheduler in Rust over a weekend", None, id="invented"),
    pytest.param("skip the test", None, id="1-of-4-left-out-is-75-percent"),
    pytest.param("skip the unit test", None, id="1-edit-in-4-is-75-percent"),
    pytest.param("um", None, id="only-fillers"),
    pytest.param("", None, id="empty"),
    pytest.param("… —", None, id="no-tokens"),
]


@pytest.mark.parametrize(("quote", "unguarded"), NOT_FOUND)
def test_a_quote_that_changes_the_meaning_or_was_never_said_is_none(quote, unguarded, monkeypatch):
    _assert_none_and_only_the_guard_says_so(quote, WORDS, unguarded, monkeypatch)


# (said, quote, first, last, ratio, shown), each with its own words.
FOUND_IN_OWN_WORDS = [
    pytest.param(
        "I'd have shipped the whole fix end to end by Friday",
        "I would have shipped the whole fix end to end by Friday",
        0,
        10,
        0.917,
        "I'd have shipped the whole fix end to end by Friday",
        id="would-for-d",
    ),
    pytest.param(
        "Let's say we sharded the queue by region",
        "Let us say we sharded the queue by region",
        0,
        7,
        1.0,
        "Let's say we sharded the queue by region",
        id="let-us-for-lets",
    ),
    # The match is whole words, so the first "I knew we could" is not exact (see below).
    pytest.param(
        "I knew we couldn't. Later I knew we could.",
        "I knew we could",
        5,
        8,
        1.0,
        "I knew we could.",
        id="found-where-it-was-said-whole",
    ),
    pytest.param(
        "we cut latency from 12-15 ms to 4 ms",
        "we cut latency from 12\u201315 ms to 4 ms",
        0,
        8,
        1.0,
        "we cut latency from 12-15 ms to 4 ms",
        id="an-en-dash-for-a-hyphen",
    ),
    # Fillers never count, in the quote either.
    pytest.param("I led the migration", "um I led", 0, 1, 1.0, "I led", id="a-filler-quoted"),
]


@pytest.mark.parametrize(("said", "quote", "first", "last", "ratio", "shown"), FOUND_IN_OWN_WORDS)
def test_a_quote_is_found_in_its_own_words(said, quote, first, last, ratio, shown):
    words = said.split()
    match = locate_quote(quote, words)
    assert match == QuoteMatch(first=first, last=last, ratio=ratio)
    assert span_text(words, match) == shown


# (said, quote, unguarded), as in NOT_FOUND.
NOT_FOUND_IN_OWN_WORDS = [
    # "'d" stays a bare "d", so the pronoun before it stands alone.
    pytest.param(
        "I'd have shipped the whole fix end to end by Friday",
        "We'd have shipped the whole fix end to end by Friday",
        (0, 10, 0.917),
        id="we-d-for-i-d",
    ),
    pytest.param(
        "They'd already shipped the fix by the time I joined",
        "We'd already shipped the fix by the time I joined",
        (0, 9, 0.909),
        id="we-d-for-they-d",
    ),
    # A hyphen splits, so a number or a "not" inside a compound is a token of its own.
    pytest.param(
        "We had a five-person team", "We had a six-person team", (0, 4, 0.833), id="six-for-five"
    ),
    pytest.param(
        "we finished the whole rewrite in a two-week sprint last spring",
        "we finished the whole rewrite in a three-week sprint last spring",
        (0, 10, 0.917),
        id="three-week-for-two-week",
    ),
    pytest.param(
        "the launch was not-so-great for the whole team this quarter",
        "the launch was so-great for the whole team this quarter",
        (0, 9, 0.917),
        id="drops-the-not-of-not-so-great",
    ),
    pytest.param(
        "everyone on the team had a can't-do attitude about it",
        "everyone on the team had a can-do attitude about it",
        (0, 9, 0.917),
        id="drops-the-not-of-cant-do",
    ),
    # A number word in the plural, past "twelfth", or ending in "-ed".
    pytest.param(
        "we migrated hundreds of customers to the new billing service",
        "we migrated thousands of customers to the new billing service",
        (0, 9, 0.9),
        id="thousands-for-hundreds",
    ),
    pytest.param(
        "We doubled the throughput of the billing service in two weeks",
        "We tripled the throughput of the billing service in two weeks",
        (0, 10, 0.909),
        id="tripled-for-doubled",
    ),
    pytest.param(
        "it was the thirteenth release of the billing service this year",
        "it was the fourteenth release of the billing service this year",
        (0, 10, 0.909),
        id="fourteenth-for-thirteenth",
    ),
    # Two words swapped, and the meaning with them. One cheapest alignment changes "not" and
    # "just" into each other. Another reads "just" as moved and never touches "not", which is why
    # the guard reads every cheapest alignment.
    pytest.param(
        "The old billing service was just not fast enough for us",
        "The old billing service was not just fast enough for us",
        (0, 10, 0.818),
        id="not-just-for-just-not",
    ),
    # The match is whole words, so a quote that starts or stops inside a word pays for the rest
    # of it: "couldn't" is "could not", "0.4%" is "0 4 percent", "1.5" is "1 5".
    pytest.param("I knew we couldn't.", "I knew we could", (0, 3, 0.8), id="stops-inside-couldnt"),
    pytest.param(
        "0.4% of users saw errors", "4% of users saw errors", (0, 4, 0.857), id="starts-inside-0.4"
    ),
    pytest.param(
        "1.5 million dollars in revenue",
        "5 million dollars in revenue",
        (0, 4, 0.833),
        id="starts-inside-1.5",
    ),
    # The cheapest match is "revenue grew", with "12" left out: one edit in 3 tokens, under the
    # 80% line. Running on to the end of "12.5%" costs two edits, so it is not the cheapest.
    pytest.param("revenue grew 12.5%", "revenue grew 12", None, id="stops-inside-12.5"),
    # The cheapest match stops short of the changed word, so only the edge rule reads it.
    pytest.param(
        "We-we led the migration of our billing service",
        "They led the migration of our billing service",
        (1, 7, 0.875),
        id="they-for-we-we",
    ),
    pytest.param(
        "When they asked if we could ship it in two weeks, my honest answer was no.",
        "When they asked if we could ship it in two weeks, my answer was yes",
        (0, 14, 0.867),
        id="yes-for-no-after-a-left-out-word",
    ),
    pytest.param(
        "I really owned the fix end to end.",
        "He owned the fix end to end",
        (1, 7, 0.857),
        id="he-for-i-before-a-left-out-word",
    ),
    # The word just outside the match is a contraction whose guarded token comes first: "I'm" is
    # "i", "am". The match is whole words, so the edge rule reads every token of that word.
    pytest.param(
        "I'm proud that I owned the fix end to end.",
        "He's proud that I owned the fix end to end",
        (1, 9, 0.9),
        id="hes-for-the-i-of-im",
    ),
    pytest.param(
        "We've shipped every release on time since March.",
        "Everyone's shipped every release on time since March",
        (1, 7, 0.875),
        id="everyones-for-the-we-of-weve",
    ),
    # The same past the match's end, where the guarded token comes second: "couldn't" is
    # "could", "not". The match stops at "said", with "yes" left out, so a rule that read only
    # the next token would read "could" and keep "I said yes" for "I said couldn't".
    pytest.param(
        "When they asked if we could ship it in two weeks, I said couldn't.",
        "When they asked if we could ship it in two weeks, I said yes",
        (0, 12, 0.929),
        id="yes-for-the-not-of-couldnt",
    ),
    # A plain word in place of a guarded first or last word, with the word beside it left out.
    # The match reads "Yes" as a change to "honestly", so only the edge rule reads the "No".
    # Neither "yes" nor "honestly" is guarded, so to the guard these look the same as a word the
    # grader added with a guarded word two words out, which it drops on purpose (see below).
    # Keeping those without keeping these would take a new list of words.
    pytest.param(
        "No, honestly, the cutover went fine for the billing team that week.",
        "Yes, the cutover went fine for the billing team that week.",
        (1, 11, 0.909),
        id="yes-for-a-first-no-with-the-next-word-left-out",
    ),
    pytest.param(
        "When they asked if we could ship it in two weeks, my answer was, honestly, no.",
        "When they asked if we could ship it in two weeks, my answer was yes",
        (0, 14, 0.933),
        id="yes-for-a-last-no-with-the-word-before-left-out",
    ),
    # A filler in between changes nothing. Without it, each is None as well.
    pytest.param(
        "I, uh, owned the fix end to end.",
        "He owned the fix end to end",
        (0, 7, 0.857),
        id="he-for-i-before-a-filler",
    ),
    pytest.param(
        "That part was, um, owned by me.",
        "That part was owned by him",
        (0, 6, 0.833),
        id="him-for-me-after-a-filler",
    ),
]


@pytest.mark.parametrize(("said", "quote", "unguarded"), NOT_FOUND_IN_OWN_WORDS)
def test_a_quote_that_changes_the_meaning_is_none_in_its_own_words(
    said, quote, unguarded, monkeypatch
):
    _assert_none_and_only_the_guard_says_so(quote, said.split(), unguarded, monkeypatch)


# Three kinds of faithful quote the guard drops on purpose (the module's docstring says why). Each
# costs a kept quote and never shows a wrong one. They are pinned so that a change to any of them
# is a choice.
@pytest.mark.parametrize(
    ("said", "quote", "unguarded"),
    [
        pytest.param(
            "and then I I led the migration of our billing service",
            "and then I led the migration of our billing service",
            (0, 10, 0.909),
            id="a-guarded-word-said-twice",
        ),
        # A word the grader added at the quote's start or end reads as a change to the transcript
        # word beside the match. Here that word is the guarded one.
        pytest.param(
            "No, I led the migration of our billing service",
            "Honestly, I led the migration of our billing service",
            (0, 8, 0.889),
            id="an-edge-word-beside-a-guarded-one",
        ),
        # Here it is not, and the edge rule reads the word past it, two words out. "Overall" reads
        # as a change to "planned." because the earlier start wins a tie, so the rule reads "we".
        # "overall" reads as a change to "and" because the run as long as the quote wins a tie, so
        # the rule reads the "I" after "uh". To the guard these look the same as the two
        # yes-for-no rows with a left-out word above, which must stay None.
        pytest.param(
            ANSWER,
            "Overall latency dropped by twenty-five percent after the second release",
            (42, 52, 0.909),
            id="a-first-word-added-two-words-from-a-guarded-one",
        ),
        # The same with "And", the plainest word a grader puts in front of a quote.
        pytest.param(
            ANSWER,
            "And latency dropped by twenty-five percent after the second release",
            (42, 52, 0.909),
            id="a-leading-and-added-two-words-from-a-guarded-one",
        ),
        pytest.param(
            ANSWER,
            "I led the migration of our billing service to the new queue, overall",
            (2, 14, 0.923),
            id="a-last-word-added-two-words-from-a-guarded-one",
        ),
        # Or the added word reads as left out, and the word beside the match is a contraction
        # that holds a guarded token. The edge rule reads all of "I'm", so it reads the "i".
        pytest.param(
            "I'm proud that I owned the fix end to end.",
            "So proud that I owned the fix end to end",
            (1, 9, 0.9),
            id="a-first-word-added-beside-the-i-of-im",
        ),
        # A closed compound for a hyphenated word that holds a guarded one. A hyphen splits, so
        # "five-person" is "five", "person", and "fiveperson" reads as a change to "five".
        pytest.param(
            "We had a five-person team for the whole migration.",
            "We had a fiveperson team for the whole migration",
            (0, 8, 0.8),
            id="a-closed-compound-for-a-hyphenated-number",
        ),
    ],
)
def test_the_guard_drops_three_kinds_of_faithful_quote_on_purpose(
    said, quote, unguarded, monkeypatch
):
    _assert_none_and_only_the_guard_says_so(quote, said.split(), unguarded, monkeypatch)


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


# Spelled out here rather than read from the module, so a word dropped from its lists fails.
UNITS = (
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
    "fifteen sixteen seventeen eighteen nineteen twenty thirty forty fifty sixty seventy eighty "
    "ninety hundred thousand million billion trillion"
).split()
ORDINALS = (
    "first second third fourth fifth sixth seventh eighth ninth tenth eleventh twelfth "
    "thirteenth fourteenth fifteenth sixteenth seventeenth eighteenth nineteenth twentieth "
    "thirtieth fortieth fiftieth sixtieth seventieth eightieth ninetieth hundredth thousandth "
    "millionth billionth trillionth"
).split()
OTHERS = (
    "once twice thrice half halves halve halved double doubled triple tripled quadruple "
    "quadrupled dozen percent"
).split()
DECADES = "twenties thirties forties fifties sixties seventies eighties nineties".split()


def test_every_number_word_is_guarded():
    said = "we shipped the fix on friday".split()
    # One word added at the end is one edit in 7 tokens, so an unguarded one is kept.
    assert locate_quote("we shipped the fix on friday today", said) == QuoteMatch(0, 5, 0.857)
    # The plural the rule reads is an added "s" ("hundreds", "tens"). The tens make theirs with
    # "ies", which is why the decades are listed. A compound written as one word is guarded too.
    number_words = [
        *UNITS,
        *ORDINALS,
        *OTHERS,
        *(f"{unit}s" for unit in UNITS),
        *DECADES,
        "twentyfive",
    ]
    kept = [
        word
        for word in number_words
        if locate_quote(f"we shipped the fix on friday {word}", said) is not None
    ]
    assert kept == []


# Fillers never count, on either side, so an "um," put anywhere in the answer changes no row:
# not whether it is found, not its ratio. Its first and last word move only to make room.
@pytest.mark.parametrize(
    "quote", [pytest.param(row.values[0], id=row.id) for row in FOUND + NOT_FOUND]
)
def test_an_um_anywhere_in_the_answer_changes_nothing(quote):
    found = locate_quote(quote, WORDS)
    for at in range(len(WORDS) + 1):  # before each word, and after the last
        words = [*WORDS[:at], "um,", *WORDS[at:]]
        if found is None:
            expected = None
        else:
            expected = QuoteMatch(
                first=found.first + 1 if at <= found.first else found.first,
                last=found.last + 1 if at <= found.last else found.last,
                ratio=found.ratio,
            )
        assert locate_quote(quote, words) == expected, f"um, before word {at}"


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


def test_a_match_cannot_be_changed():
    match = QuoteMatch(first=2, last=9, ratio=1.0)
    with pytest.raises(dataclasses.FrozenInstanceError):
        match.first = 3


def test_the_word_lists_are_the_plans():
    assert FILLERS == set("um uh uhm erm er ah hmm mm mhm".split())
    assert NEGATIONS == set("not no never nothing nobody none neither nor without".split())
    # "you" is not first person, so a dropped "you know" still passes.
    assert FIRST_PERSON == set("i me my mine myself we us our ours ourselves".split())


def test_the_module_is_pure():
    # Standard library only, with no logging and no I/O, so no quote can reach a log or a file
    # from here.
    module_path = Path(sys.modules["mockingbird.quotes"].__file__)
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    imported, called = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            # A relative import is one of ours, never the standard library.
            imported.add("." if node.level else node.module.split(".")[0])
        elif isinstance(node, ast.Call):
            # print(...) or open(...), and the same through a module: io.open(...).
            called.add(getattr(node.func, "id", getattr(node.func, "attr", None)))
    assert imported - sys.stdlib_module_names == set()
    assert imported & {"logging", "io", "os"} == set()
    assert called & {"print", "open"} == set()
