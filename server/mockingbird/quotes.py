"""Finding a quote in the words the candidate said, so the report shows those words and can play
them.

The grader cites quotes as evidence for each score, and a quote counts only if the candidate
really said it. After the call, the recording's transcript is a list of timed words, and a grader
copying from it still slips: it drops an "um", writes "I am" for "I'm", or leaves out a "you
know". `locate_quote` finds where a quote sits in that list and how close it is, so the report
can show the transcript's own words and seek to them.

It compares tokens, not words. `tokenize` runs the same rule on the quote and on each transcript
word, so "I'm" and "I am" both become "i am". Each transcript token remembers the word it came
from, so a match comes back as the first and last word of the transcript (`QuoteMatch`), and
`span_text` gives those words as the transcriber wrote them.

The match is the stretch of the transcript that is cheapest to turn into the quote: a changed,
added or left-out word costs 1, and a skipped filler such as "um" costs nothing. That is edit
distance with a free start and end in the transcript (Sellers' algorithm). A match that differs
only by fillers is always kept. Any other is kept only when it needs at most one edit for every
five words, and no edit touches a word that can flip what the quote claims: a negation, a number,
or a first-person pronoun such as "I" or "we" (Review Focus 2). "rose" for "dropped" still
passes, because no list of words can know an antonym. That is why the report shows the
transcript's words and never the grader's.

The module is pure: standard library only, no I/O and no logging, so no quote can reach a log
from here.
"""

import re
import string
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from operator import itemgetter

FILLERS = frozenset({"um", "uh", "uhm", "erm", "er", "ah", "hmm", "mm", "mhm"})
NEGATIONS = frozenset(
    {"not", "no", "never", "nothing", "nobody", "none", "neither", "nor", "without"}
)
# "you" is not first person, so a quote that leaves out "you know" still passes.
FIRST_PERSON = frozenset(
    {"i", "me", "my", "mine", "myself", "we", "us", "our", "ours", "ourselves"}
)

# A number written as a word, a compound included once its hyphen is gone ("twentyfive"): any
# run of units, ending in a unit or in one of the other words.
_UNITS = (
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
    "fifteen sixteen seventeen eighteen nineteen twenty thirty forty fifty sixty seventy eighty "
    "ninety hundred thousand million billion"
).split()
_OTHERS = (
    "first second third fourth fifth sixth seventh eighth ninth tenth eleventh twelfth "
    "twentieth thirtieth hundredth thousandth once twice half double triple dozen percent"
).split()
_NUMBER_WORD = re.compile(f"(?:{'|'.join(_UNITS)})*(?:{'|'.join(_UNITS + _OTHERS)})")

# Straight, right single quote, left single quote, and the modifier letter apostrophe. Unicode
# counts the last one as a letter, not punctuation, so only this list deletes it.
_APOSTROPHES = "'\u2019\u2018\u02bc"
_A = f"[{_APOSTROPHES}]"
# Expanded where they end a word, so "I'm" in the transcript and "I am" in a quote agree, and so
# "didn't" shows its "not" to the guard: as "didnt" against "did", dropping the "not" would be an
# edit on two unguarded words. "can't" and "won't" come before the general rule, which would make
# them "ca not" and "wo not". A quote mark before a word is not a contraction ("'my" stays "my"),
# because every pattern ends where the word does.
_CONTRACTIONS = [
    (re.compile(rf"\bcan{_A}t\b"), "can not"),
    (re.compile(r"\bcannot\b"), "can not"),
    (re.compile(rf"\bwon{_A}t\b"), "will not"),
    (re.compile(rf"n{_A}t\b"), " not"),
    (re.compile(rf"{_A}m\b"), " am"),
    (re.compile(rf"{_A}re\b"), " are"),
    (re.compile(rf"{_A}ve\b"), " have"),
    (re.compile(rf"{_A}ll\b"), " will"),
]
# A hyphen (ASCII, Unicode or non-breaking) between two word characters is deleted, so a compound
# agrees with or without it: "e-commerce" and "ecommerce". A dash, or a hyphen beside a space,
# splits words like any other punctuation.
_HYPHEN_IN_WORD = re.compile(r"(?<=\w)[-\u2010\u2011](?=\w)")

_cost_then_matched = itemgetter(0, 1)  # what two cells of the alignment table compare on


@dataclass(frozen=True)
class QuoteMatch:
    """Where a quote was found: from `words[first]` to `words[last]`, both included."""

    first: int
    last: int
    ratio: float  # 1 - edits / max(quote tokens, tokens said), to 3 places; 1.0 when exact


def tokenize(text: str) -> list[str]:
    """The words of `text` as comparable tokens: lowercased, contractions expanded, a hyphen
    inside a word deleted, "%" spelled out, apostrophes deleted, other punctuation split on.

    The one tokenizer for both sides, the quote and each transcript word. Punctuation becomes a
    space rather than vanishing. Phase 1's `normalize` deleted it, so two words with a dash
    between them and no spaces became one word.
    """
    text = text.lower()
    for pattern, expansion in _CONTRACTIONS:
        text = pattern.sub(expansion, text)
    text = _HYPHEN_IN_WORD.sub("", text)
    text = text.replace("%", " percent ")
    text = "".join("" if ch in _APOSTROPHES else " " if _is_punctuation(ch) else ch for ch in text)
    return text.split()


def locate_quote(quote: str, words: Sequence[str]) -> QuoteMatch | None:
    """Where `quote` sits in `words`, or None when it is not close enough to what was said.

    `words` are the transcript's words in time order, as the transcriber wrote them. A match
    that differs only by fillers is kept at any length. Any other needs a ratio of 0.80 or more,
    and no edit may touch a negation, a first-person pronoun, a digit or a number word.
    """
    q = tokenize(quote)
    tokens: list[str] = []
    owner: list[int] = []  # owner[k] is the index in `words` of the word tokens[k] came from
    for index, word in enumerate(words):
        for token in tokenize(word):  # a lone dash gives none
            tokens.append(token)
            owner.append(index)
    if not q or not tokens:
        return None

    cost, start, end = _cheapest_span(q, tokens)
    # A span can begin or end on a filler. A filler is no evidence of anything, so what the
    # report shows never begins or ends with one.
    while start < end and tokens[start] in FILLERS:
        start += 1
    while end > start and tokens[end - 1] in FILLERS:
        end -= 1
    if start == end:
        return None  # the span was all fillers

    said = [token for token in tokens[start:end] if token not in FILLERS]
    denominator = max(len(q), len(said))
    if cost > 0:
        # A ratio of 0.80 or more, compared in integers so a match exactly on the line is kept.
        # It needs no length rule of its own: with 3 quote tokens or fewer, one edit is already
        # under 0.80.
        if 5 * cost > denominator:
            return None
        if any(_guarded(token) for token in _touched(q, said)):
            return None
    return QuoteMatch(
        first=owner[start], last=owner[end - 1], ratio=round(1 - cost / denominator, 3)
    )


def span_text(words: Sequence[str], match: QuoteMatch) -> str:
    """The matched words as the transcriber wrote them, joined by single spaces. This is what the
    report shows, never the grader's wording."""
    return " ".join(words[match.first : match.last + 1])


def _is_punctuation(ch: str) -> bool:
    return ch in string.punctuation or unicodedata.category(ch).startswith("P")


def _cheapest_span(q: list[str], tokens: list[str]) -> tuple[int, int, int]:
    """The cost of aligning `q` with the stretch of `tokens` that is cheapest to turn into it,
    and that stretch's start and end (the end excluded).

    Sellers' algorithm: a Levenshtein table with a row per quote token and a column per
    transcript token, whose first row is all zeros, so a match may start at any token. A
    substitution, an insertion and a deletion each cost 1, and skipping a filler in the
    transcript costs 0. A long answer makes a wide table, so only two rows are kept, and each
    cell carries where its stretch starts instead of a path back.
    """
    # A cell is (cost, -matched, start), compared on (cost, -matched): of two equally cheap
    # alignments, the one that matches more words wins. On a full tie, min keeps the first
    # candidate: a step from the diagonal, then from above, then from the left.
    previous = [(0, 0, start) for start in range(len(tokens) + 1)]
    for i, quoted in enumerate(q, start=1):
        current = [(i, 0, 0)]  # the quote's first i tokens, none of them said
        for j, token in enumerate(tokens, start=1):
            cost, minus_matched, start = previous[j - 1]
            if quoted == token:
                diagonal = (cost, minus_matched - 1, start)
            else:
                diagonal = (cost + 1, minus_matched, start)
            cost, minus_matched, start = previous[j]
            above = (cost + 1, minus_matched, start)  # quoted but not said
            cost, minus_matched, start = current[j - 1]
            skip = 0 if token in FILLERS else 1
            left = (cost + skip, minus_matched, start)  # said but not quoted
            current.append(min(diagonal, above, left, key=_cost_then_matched))
        previous = current

    def end_rank(j: int) -> tuple[int, int, int, int]:
        # The cheapest end, then the one that matched most, then the span nearest the quote's
        # length (so a changed last word is shown, not cut off), then the earliest (so a phrase
        # said twice is found where it was first said).
        cost, minus_matched, start = previous[j]
        return cost, minus_matched, abs((j - start) - len(q)), j

    end = min(range(1, len(tokens) + 1), key=end_rank)
    cost, _, start = previous[end]
    return cost, start, end


def _touched(q: list[str], said: list[str]) -> list[str]:
    """Every token an edit touches in a cheapest alignment of `q` with `said`: both sides of a
    substitution, and the one token of an insertion or a deletion.

    A plain Levenshtein table, kept whole this time so it can be walked back. `said` is about as
    long as the quote, so the table is small.
    """
    table = [[0] * (len(said) + 1) for _ in range(len(q) + 1)]
    for i in range(len(q) + 1):
        table[i][0] = i  # the quote's first i tokens, none of them said
    for j in range(len(said) + 1):
        table[0][j] = j  # the first j tokens said, none of them quoted
    for i in range(1, len(q) + 1):
        for j in range(1, len(said) + 1):
            table[i][j] = min(
                table[i - 1][j - 1] + _change(q[i - 1], said[j - 1]),
                table[i - 1][j] + 1,
                table[i][j - 1] + 1,
            )

    touched: list[str] = []
    i, j = len(q), len(said)
    # Walk back to the corner, not just to an edge: an edit at the very start of the quote, like
    # the "No" of "No, so I led", is a step along the table's edge.
    while i > 0 or j > 0:
        diagonal = i > 0 and j > 0
        if diagonal and table[i][j] == table[i - 1][j - 1] + _change(q[i - 1], said[j - 1]):
            if q[i - 1] != said[j - 1]:
                touched += [q[i - 1], said[j - 1]]
            i, j = i - 1, j - 1
        elif i > 0 and table[i][j] == table[i - 1][j] + 1:
            touched.append(q[i - 1])  # quoted but not said
            i -= 1
        else:
            touched.append(said[j - 1])  # said but not quoted
            j -= 1
    return touched


def _change(a: str, b: str) -> int:
    """What a diagonal step costs: 0 when the two tokens match, 1 for a substitution."""
    return 0 if a == b else 1


def _guarded(token: str) -> bool:
    """A word whose change can flip what a quote claims: "not" dropped, 12% made 15%, "I" made
    "we"."""
    return (
        token in NEGATIONS
        or token in FIRST_PERSON
        or any(ch.isdigit() for ch in token)
        or _NUMBER_WORD.fullmatch(token) is not None
    )
