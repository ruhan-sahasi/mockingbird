"""Finding a quote in the words the candidate said, so the report shows those words and can play
them.

The grader cites quotes as evidence for each score, and a quote counts only if the candidate
really said it. After the call, the recording's transcript is a list of timed words, and a grader
copying from it still slips: it drops an "um", writes "I am" for "I'm", or leaves out a "you
know". `locate_quote` finds where a quote sits in that list and how close it is, so the report
can show the transcript's own words and seek to them.

It compares tokens, not words. `tokenize` runs the same rule on the quote and on each transcript
word, so "I'm" and "I am" both become "i am". Fillers such as "um" never count, on either side:
they are dropped from the quote and from the transcript before the search, so a filler can never
move a match or decide whether it is kept. Each transcript token remembers the word it came from,
so a match comes back as the first and last word of the transcript (`QuoteMatch`), and
`span_text` gives those words as the transcriber wrote them.

The match is the run of whole words that is cheapest to turn into the quote, where a changed,
added or left-out token costs 1. That is edit distance with a free start and end in the
transcript (Sellers' algorithm), held to the edges of words: a quote that stops inside a word
pays for the rest of it, so "I knew we could" is not an exact match for "I knew we couldn't".

A match with no edits is always kept. Any other is kept only when it needs at most one edit for
every five tokens, and no edit touches a word that can flip what the quote claims: a negation, a
number, or a first-person pronoun such as "I" or "we" (Review Focus 2). The guard reads every
cheapest way to line the quote up with the match, not just one. It also reads the transcript word
just outside the match when the quote changes or leaves out its own first or last word, because
the cheapest match can stop short of the word that changed. "rose" for "dropped" still passes,
because no list of words can know an antonym. That is why the report shows the transcript's
words and never the grader's.

The guard errs toward dropping a quote. It drops two kinds of faithful quote on purpose, and the
tests pin both, since each costs a kept quote and never shows a wrong one:
- A guarded word the transcript repeats. "and then I led" against "and then I I led" is one
  edit, and the edit is an "I".
- A word the grader added at the quote's start or end, with a guarded word one or two words out
  in the transcript. The added word can read as a change to the word beside the match, and the
  edge rule then reads the word past that one. "Honestly, I led" against "No, I led" reads as
  "Honestly" in place of "No". "Overall latency dropped" against "we planned. Latency dropped"
  reads as "Overall" in place of "planned", and past it is "we".

The module is pure: standard library only, no I/O and no logging, so no quote can reach a log
from here.
"""

import re
import string
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass

FILLERS = frozenset({"um", "uh", "uhm", "erm", "er", "ah", "hmm", "mm", "mhm"})
NEGATIONS = frozenset(
    {"not", "no", "never", "nothing", "nobody", "none", "neither", "nor", "without"}
)
# "you" is not first person, so a quote that leaves out "you know" still passes.
FIRST_PERSON = frozenset(
    {"i", "me", "my", "mine", "myself", "we", "us", "our", "ours", "ourselves"}
)

# A number written as a word: a run of units that ends in a unit, an ordinal or one of the other
# words, with or without a plural "s" ("five", "twentyfive", "hundreds", "fourteenth", "seconds",
# "doubled"), or a decade ("twenties"). Hyphens split before this is read, so "twenty-five"
# reaches it as "twenty" and "five", each a number word on its own.
_UNITS = (
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
    "fifteen sixteen seventeen eighteen nineteen twenty thirty forty fifty sixty seventy eighty "
    "ninety hundred thousand million billion trillion"
).split()
_ORDINALS = (
    "first second third fourth fifth sixth seventh eighth ninth tenth eleventh twelfth "
    "thirteenth fourteenth fifteenth sixteenth seventeenth eighteenth nineteenth twentieth "
    "thirtieth fortieth fiftieth sixtieth seventieth eightieth ninetieth hundredth thousandth "
    "millionth billionth trillionth"
).split()
_OTHERS = (
    "once twice thrice half halves halve halved double doubled triple tripled quadruple "
    "quadrupled dozen percent"
).split()
_DECADES = "twenties thirties forties fifties sixties seventies eighties nineties".split()
_NUMBER_WORD = re.compile(
    f"(?:{'|'.join(_UNITS)})*(?:{'|'.join(_UNITS + _ORDINALS + _OTHERS)})s?|{'|'.join(_DECADES)}"
)

# Straight, right single quote, left single quote, and the modifier letter apostrophe. Unicode
# counts the last one as a letter, not punctuation, so only this list deletes it.
_APOSTROPHES = "'\u2019\u2018\u02bc"
_A = f"[{_APOSTROPHES}]"
# Expanded where they end a word, so "I'm" in the transcript and "I am" in a quote agree, and so
# a guarded word inside a contraction stands alone where the guard can read it: the "not" of
# "didn't", the "us" of "let's", the pronoun of "I'd" or "we'd". As "didnt" against "did",
# dropping the "not" would be an edit on two unguarded words. "'d" becomes a bare "d", since it
# can stand for "would" or "had" and nothing here can tell which. "let's" is matched as a whole
# word, because "'s" is expanded nowhere else ("O'Brien's" stays one token). "can't" and "won't"
# come before the general rule, which would make them "ca not" and "wo not". A quote mark before
# a word is not a contraction ("'my" stays "my"), because every pattern ends where the word does.
_CONTRACTIONS = [
    (re.compile(rf"\blet{_A}s\b"), "let us"),
    (re.compile(rf"\bcan{_A}t\b"), "can not"),
    (re.compile(r"\bcannot\b"), "can not"),
    (re.compile(rf"\bwon{_A}t\b"), "will not"),
    (re.compile(rf"n{_A}t\b"), " not"),
    (re.compile(rf"{_A}m\b"), " am"),
    (re.compile(rf"{_A}re\b"), " are"),
    (re.compile(rf"{_A}ve\b"), " have"),
    (re.compile(rf"{_A}ll\b"), " will"),
    (re.compile(rf"{_A}d\b"), " d"),
]


@dataclass(frozen=True)
class QuoteMatch:
    """Where a quote was found: from `words[first]` to `words[last]`, both included."""

    first: int
    last: int
    ratio: float  # 1 - edits / max(quote tokens, tokens said), to 3 places; 1.0 when exact


def tokenize(text: str) -> list[str]:
    """The words of `text` as comparable tokens: lowercased, contractions expanded, "%" spelled
    out, apostrophes deleted, and split on every other punctuation mark, a hyphen included.

    The one tokenizer for both sides, the quote and each transcript word. Punctuation becomes a
    space rather than vanishing. Phase 1's `normalize` deleted it, so two words with a dash
    between them and no spaces became one word. A hyphen splits as well, so "twenty-five" is
    "twenty", "five" and "can't-do" is "can", "not", "do": joined into one token, a guarded word
    would hide from the guard.
    """
    text = text.lower()
    for pattern, expansion in _CONTRACTIONS:
        text = pattern.sub(expansion, text)
    text = text.replace("%", " percent ")
    text = "".join("" if ch in _APOSTROPHES else " " if _is_punctuation(ch) else ch for ch in text)
    return text.split()


def locate_quote(quote: str, words: Sequence[str]) -> QuoteMatch | None:
    """Where `quote` sits in `words`, or None when it is not close enough to what was said.

    `words` are the transcript's words in time order, as the transcriber wrote them. Fillers
    never count, in the quote or in `words`, and the match is a run of whole words. A match with
    no edits is kept at any length. Any other needs a ratio of 0.80 or more, and no edit on any
    cheapest alignment may touch a negation, a first-person pronoun, a digit or a number word,
    nor may an edit to the quote's first or last token sit beside one just outside the match.
    """
    q = [token for token in tokenize(quote) if token not in FILLERS]
    tokens: list[str] = []
    owner: list[int] = []  # owner[k] is the index in `words` of the word tokens[k] came from
    for index, word in enumerate(words):
        for token in tokenize(word):  # a lone dash gives none
            if token not in FILLERS:
                tokens.append(token)
                owner.append(index)
    if not q or not tokens:
        return None

    cost, start, end = _cheapest_span(q, tokens, owner)
    said = tokens[start:end]
    denominator = max(len(q), len(said))
    if cost > 0:
        # A ratio of 0.80 or more, compared in integers so a match exactly on the line is kept.
        # It needs no length rule of its own: with 3 quote tokens or fewer, one edit is already
        # under 0.80. An empty run, the whole quote left unsaid, costs every quote token, so it
        # never passes either.
        if 5 * cost > denominator:
            return None
        before = tokens[start - 1] if start > 0 else None
        after = tokens[end] if end < len(tokens) else None
        if any(_guarded(token) for token in _touched(q, said, before, after)):
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


def _cheapest_span(q: list[str], tokens: list[str], owner: list[int]) -> tuple[int, int, int]:
    """The cost of aligning `q` with the run of whole words in `tokens` that is cheapest to turn
    into it, and that run's start and end (the end excluded). The run is empty only when leaving
    the whole quote unsaid is cheapest.

    Sellers' algorithm: a Levenshtein table with a row per quote token and a column per
    transcript token, whose first row lets a match start anywhere. A substitution, an insertion
    and a deletion each cost 1. A long answer makes a wide table, so only two rows are kept, and
    each cell carries where its run starts instead of a path back.

    A run starts on the first token of a word and ends on the last token of one. In the first
    row, a column inside a word costs the tokens of that word before it, which the run then holds
    as said but not quoted, and only a column at the end of a word may end a run. Without that,
    "I knew we could" would be an exact match for the first three words of "I knew we couldn't"
    ("i knew we could not"), and "4% of users" for the end of "0.4% of users".
    """
    n = len(tokens)
    # edge[j]: tokens j - 1 and j belong to different words, so a run may start or end between
    # them (or j is the start or the end of the transcript).
    edge = [j == 0 or j == n or owner[j - 1] != owner[j] for j in range(n + 1)]

    # A cell is (cost, -matched, start), and cells compare as whole tuples: the cheaper
    # alignment wins, then the one that matches more tokens, then the one that starts earlier.
    previous: list[tuple[int, int, int]] = []
    word_start = 0
    for j in range(n + 1):
        if edge[j]:
            word_start = j
        previous.append((j - word_start, 0, word_start))  # free only at the start of a word
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
            left = (cost + 1, minus_matched, start)  # said but not quoted
            current.append(min(diagonal, above, left))
        previous = current

    def end_rank(j: int) -> tuple[int, int, int, int]:
        # The cheapest end, then the one that matched most, then the run nearest the quote's
        # length, then the earliest (so a phrase said twice is found where it was first said).
        # The length only picks what the report shows. The guard does not rely on it to keep a
        # changed word inside the run: it reads the word just past either end (see `_touched`).
        cost, minus_matched, start = previous[j]
        return cost, minus_matched, abs(j - start - len(q)), j

    end = min((j for j in range(1, n + 1) if edge[j]), key=end_rank)
    cost, _, start = previous[end]
    return cost, start, end


def _touched(q: list[str], said: list[str], before: str | None, after: str | None) -> set[str]:
    """Every token an edit touches on any cheapest alignment of `q` with `said`, plus the
    transcript tokens just outside the match that the edge rule adds. `before` and `after` are
    those tokens, or None at the transcript's start or end.

    An edit touches both tokens of a substitution, and the one token of a deletion (quoted but
    not said) or an insertion (said but not quoted). Two alignments can cost the same and touch
    different tokens, so every one is read: `forward[i][j]` is the cost of aligning the quote's
    first i tokens with the first j said, `backward[i][j]` the cost of aligning the rest, and an
    edit lies on a cheapest alignment when the cost before it, plus 1, plus the cost after it,
    is the total.

    The edge rule covers a word the match stopped short of. "He owned the fix end to end"
    against "I really owned the fix end to end" is cheapest from "really", with "He" in place of
    "really", so no edit touches "I". When a cheapest alignment's first step changes or leaves
    out the quote's first token, the token just before the match counts as touched, and likewise
    the token just after it when the last step changes or leaves out the quote's last token.
    """
    m, n = len(q), len(said)
    forward = _distances(q, said)
    # The same table over both lists reversed, turned back around: the cost of aligning q[i:]
    # with said[j:].
    backward = [row[::-1] for row in reversed(_distances(q[::-1], said[::-1]))]
    total = forward[m][n]

    touched: set[str] = set()
    for i in range(m + 1):
        for j in range(n + 1):
            reached = forward[i][j] + 1  # the cheapest way to (i, j), then one edit
            if i < m and j < n and q[i] != said[j] and reached + backward[i + 1][j + 1] == total:
                touched.update((q[i], said[j]))  # one changed into the other
            if i < m and reached + backward[i + 1][j] == total:
                touched.add(q[i])  # quoted but not said
            if j < n and reached + backward[i][j + 1] == total:
                touched.add(said[j])  # said but not quoted

    first_step_edits_quote = (q[0] != said[0] and 1 + backward[1][1] == total) or (
        1 + backward[1][0] == total
    )
    last_step_edits_quote = (q[-1] != said[-1] and forward[m - 1][n - 1] + 1 == total) or (
        forward[m - 1][n] + 1 == total
    )
    if first_step_edits_quote and before is not None:
        touched.add(before)
    if last_step_edits_quote and after is not None:
        touched.add(after)
    return touched


def _distances(a: list[str], b: list[str]) -> list[list[int]]:
    """A plain Levenshtein table, kept whole: `table[i][j]` is the fewest edits that turn the
    first i tokens of `a` into the first j of `b`. Both lists are about as long as the quote, so
    the table is small."""
    table = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i in range(len(a) + 1):
        table[i][0] = i  # the first i tokens of `a`, none of them in `b`
    for j in range(len(b) + 1):
        table[0][j] = j  # the first j tokens of `b`, none of them in `a`
    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            table[i][j] = min(
                table[i - 1][j - 1] + (a[i - 1] != b[j - 1]),
                table[i - 1][j] + 1,
                table[i][j - 1] + 1,
            )
    return table


def _guarded(token: str) -> bool:
    """A word whose change can flip what a quote claims: "not" dropped, 12% made 15%, "I" made
    "we", "hundreds" made "thousands"."""
    return (
        token in NEGATIONS
        or token in FIRST_PERSON
        or any(ch.isdigit() for ch in token)
        or _NUMBER_WORD.fullmatch(token) is not None
    )
