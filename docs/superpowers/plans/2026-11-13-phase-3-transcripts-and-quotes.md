# Mockingbird Phase 3 (Transcripts, Checked Quotes and Playback) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** After each call, turn the candidate's recording into words with
timestamps, grade from that transcript, keep only the quotes found in it,
and let the report page play the recording from each quote.

**Architecture:**
- When a call ends, the server uploads the recording, writes a job row in
  Postgres and starts one worker for that interview. The worker is a one-off
  ECS task in production, and a subprocess or an in-process task in
  development.
- The worker downloads the candidate's WAV and runs faster-whisper
  (`small.en`) to get words with start and end times, placed on the call's
  clock. The start time of each interviewer reply splits those words by main
  question.
- Claude Opus grades a transcript built from those words. Each quote it
  cites is located in that question's words: an exact match first, then a
  word-level fuzzy alignment with guards. A located quote takes the
  transcript's own words and their start and end time. Any other quote is
  dropped.
- The report page asks the server for a short-lived signed link to the
  candidate's WAV, and seeks to a quote's time when the quote is clicked.
- The existing sweeper retries a job whose worker died or never started. If
  no worker can finish, the server grades the live text itself, with an
  exact whole-word check and no playback, so a report always arrives.

**Tech Stack:** Python 3.12, FastAPI, asyncpg, httpx, faster-whisper
(CTranslate2), numpy, boto3, pytest. React, TypeScript, Vitest. Terraform,
ECS Fargate ARM64, ECR. GitHub Actions, with an ARM64 runner for the worker.

**Spec:** `docs/superpowers/specs/2026-10-19-mockingbird-design.md`. Read
"Transcripts and grading", "Data model", "Cost: no out-of-pocket spending"
and "Deployment", and the decisions table (phase 3 rows, added with this
plan). The phase 2 plan describes the recorder, the audio store, the tracer
and the sweeper that this plan extends:
`docs/superpowers/plans/2026-11-11-phase-2-recording-and-latency.md`.

## Global Constraints

- **Ruhan's phase 3 decisions:**
  - Speech-to-text is faster-whisper, self-hosted, in the project's own AWS
    task. No recording goes to a new third party.
  - Post-call work runs as one ECS task per interview (RunTask), never in
    the process that carries live audio.
  - A quote is checked by an exact match first, then a word-level fuzzy
    match at 80% or better. An edit that touches a negation, a number or a
    first-person pronoun rejects the quote. The report shows the
    transcript's own words, never the grader's wording.
  - Clicking a quote seeks into the candidate's whole track through a
    short-lived signed link. No clips are cut, and no audio is streamed
    through the server in production.
- **One clock.** Every stored time (a turn's `start_ms`, a word's
  `start_ms` and `end_ms`, a quote's `start_ms` and `end_ms`) is in ms since
  the call went live, the recorder's clock. A track's place on that clock is
  `interviews.recording["candidate"]["start_ms"]`, which can be negative.
  The offset into the WAV file is the stored time minus that start.
- **Only the candidate's track is transcribed.** The interviewer's lines
  are already known as text.
- **Whisper settings:** model `small.en`, `compute_type="int8"`,
  `language="en"`, `word_timestamps=True`, `vad_filter=True`,
  `condition_on_previous_text=False`, `beam_size` 1 by default. Model
  directory, beam size and thread count are settings, not code.
- **The server image never holds faster-whisper or a model.** In
  production only the worker transcribes. The server's own grading path
  reads the live text.
- **A report always arrives.** The order is: grade the recording's
  transcript; if that cannot be made, grade the live text with an exact
  whole-word check and no playback; if grading itself fails, save a
  `grading_failed` report. The report says which transcript it used
  (`"transcript": "recording"` or `"live"`).
- **Logs never hold what the candidate said.** No word, quote, transcript
  line or grader output reaches a log. Quotes are counted, never logged:
  one line per report gives how many were kept exactly, kept by the fuzzy
  match, and dropped. The spec says an unmatched quote is "dropped and
  logged": that line is the log.
- **Ruling R19 carries over.** The Vapi call id, socket URLs, one-time
  tokens and every key stay out of logs, error text and the browser. A
  signed recording link is a credential of the same kind: it is sent only
  in the recording response, with `Cache-Control: no-store`, and is never
  logged.
- **Retention.** `words` and `post_call_jobs` rows go with their interview
  (`on delete cascade`), so the 7-day purge needs no new step. Phase 3 adds
  no storage objects.
- **Migrations:** `005_words.sql` and `006_post_call_jobs.sql`. Every new
  table enables row level security with no policies, like `001_init.sql`.
- **Dependencies:**
  - faster-whisper and numpy go in an optional extra named `worker`, and
    boto3 in one named `aws`. The server image installs `aws` only. The
    worker image installs `worker` only.
  - `pyproject.toml` gains `[tool.uv] exclude-newer`, a date about two
    weeks before the lock is made, so no release younger than that is
    locked. ctranslate2 4.9.0 and onnxruntime 1.32.0 came out in the last
    few days. `uv lock` must add packages only and change no existing pin.
  - The worker hands faster-whisper a numpy array, never a file path. The
    1.2.1 release cannot open files with PyAV 19, and our WAVs need no
    decoder.
- **No unbounded bill.** A worker task stops itself when its job's lease
  runs out, and in any case within an hour of starting. The worker runs on
  2 vCPU and 4 GB by default, because a new AWS account allows about 6
  Fargate vCPUs at once.
- **Tests stay hermetic.** No test downloads a model, calls AWS, or writes
  inside the repo. A test that needs numpy, faster-whisper or boto3 starts
  with `pytest.importorskip`, and CI installs every extra so it runs there.
- **This machine** (where the tasks are built):
  - Do not install the `worker` extra or download a model: the disk is
    nearly full. Run the numpy tests with `uv run --with numpy pytest -q
    server/tests/test_transcribe.py`. The real transcription first runs in
    CI's `worker` job.
  - Docker, shellcheck and `terraform init` are not available. CI's docker
    and terraform jobs run them. `terraform fmt` works.
  - The Postgres tests skip without `TEST_DATABASE_URL`, so a local PASS
    says nothing about them. If a local Postgres is up, run them with
    `TEST_DATABASE_URL=<its url>` before committing; if not, the PR's
    python job is their first run.
- **README:** each task fixes the README lines it makes false. Task 7
  writes the new sections and the test counts.
- **Commits and PRs:**
  - Every commit has an imperative subject and a prose body, and no AI
    attribution of any kind.
  - Each task is one PR, stacked on the previous one, with the four-part
    walkthrough: What changed / Why it is built this way / How to explain it
    in an interview / How to check it.
  - Branches: `p3/01-quote-locator`, `p3/02-word-storage`,
    `p3/03-transcriber`, `p3/04-grade-recording`, `p3/05-post-call-jobs`,
    `p3/06-ecs-worker`, `p3/07-playback`.
- **Copy:** README and UI copy are plain and specific, with no em or en
  dashes and no apostrophes.
  - Consent, on the Landing page: "Your voice is recorded and sent to Vapi
    and Claude to run and grade this interview. Recordings are deleted after
    7 days. Nothing is shared publicly, but anyone you give your report link
    to can read it and play the recording."
  - While grading, on the Report page: "Grading your answers. This can take
    a few minutes. This page updates by itself when the report is ready."

## My calls (flagged for Ruhan to check)

1. Only the candidate's track is transcribed. It halves the compute and the
   Supabase downloads. It also drops the spec's plan to correct an
   interrupted interviewer line from the interviewer's track after the
   call. That line keeps what is written during the call: the words Vapi
   reported as spoken in its next request, or the whole generated line when
   the call ended while it played. The grader reads interviewer lines only
   as context, so a line a few words too long costs little.
2. A new `turns.start_ms` on each interviewer reply splits the candidate's
   words by question. The latency traces hold the same stamp, but grading
   should not depend on a stats table.
3. When the recording cannot be transcribed, or no worker can finish, the
   report is graded from the live text with no playback, and says so,
   instead of failing. The server does that grade itself: one Claude call
   and a plain word scan, light enough for the process that carries live
   audio. The word-by-word alignment never runs there.
4. The worker starts at 2 vCPU and 4 GB with `small.en` and greedy decoding.
   Time to report grows from about a minute to an estimated 3 to 5 minutes
   for a demo: a cold start, the transcription, then the grade. The worker
   logs each stage's seconds so the first live runs can tune it. The
   interviewer's last line and the report page now say "a few minutes".
5. A job gets 2 worker attempts and 3 launches. A job waits 180 s for the
   worker it was last given before another is started. A lease lasts 600 s
   plus twice the call's cap: 20 minutes for a demo, 50 for a full
   interview. A worker that dies is noticed only when its lease runs out,
   so the worst case is long: a fault that kills every worker takes two
   leases before the server grades the live text, about 45 minutes for a
   demo and 105 for a full interview. A save that fails also waits one
   lease before it is retried. The server gets two tries of its own, so a
   failed save or a deploy during its grade is retried once more, and only
   a second failure ends in a `grading_failed` report. A worker that renews
   a short lease would find a death within a minute or two. It is left out
   until the first live runs show workers dying.
6. **Accepted by Ruhan on 2026-11-13, for a demo.** A signed link lasts
   30 minutes, and one interview can ask for at most 10 a minute. That limit counts links, not
   downloads. A link can be downloaded any number of times until it
   expires, straight from Supabase, where the server can neither see nor
   throttle it, and one link cannot be revoked. So anyone who finishes a
   demo, or holds a report link, could loop the download and use up the
   free plan's 5 GB of monthly egress: about 520 pulls of a 9.6 MB demo
   track. No money is charged, but Supabase can then restrict the project,
   which stops recordings and may stop the whole app, and the one way out
   the research found is a paid plan. Nothing inside the signed-link
   decision bounds this. The route gives a link only for a graded report
   built from the recording, which narrows who can ask but not what a link
   allows. The options are to accept it for a demo; to serve audio through
   the server, which can count bytes per interview (the option set aside
   in the decisions table); or to move audio to S3 behind CloudFront, whose
   free tier is 1 TB a month, so a loop would have to run far longer to
   matter.
7. The consent sentence above now says who can play the recording.
8. Recordings stay in Supabase Storage. At about 8 demos a day, sustained
   for a month, the worker's downloads plus playback would pass the free
   plan's 5 GB. That is about the same usage as the storage limit accepted
   in phase 2. Moving audio to S3 is one new class behind `AudioStore`.
   This counts honest use only. Call 6 covers a visitor who loops a
   download.
9. The spec's Unknown 7 (is `small.en` accurate and fast enough) is
   measured after the build, on the first live runs and with phase 5's eval
   set, not before. If it is too slow, the fixes are a larger task or a
   smaller model. If it is not accurate enough, a larger model on a larger
   task.
10. The spec's full-mode summary across questions is not in this phase or
    in any planned one. It would be one more grader call after the
    per-question grades.

## A check for Ruhan before Task 7

Task 7 rests on one thing nobody has confirmed: that a Supabase signed link
answers a byte-range request with 206, so the browser can seek into the
file. It needs a Supabase project and its dashboard, so it is Ruhan's to
run, and it can run while Tasks 1 to 6 are built:

1. In the Supabase dashboard, upload any small file to a private bucket and
   create a signed URL for it.
2. Run `curl -sD - -r 0-1 -o /dev/null '<signed URL>'`.

The answer should start with `HTTP/2 206` and carry a `content-range`
header. If it is 200, Task 7 waits and the playback decision goes back to
Ruhan.

## Review Focus

1. **Whisper writes words for silence.** The candidate's track is silent
   while the interviewer talks, and Whisper can turn silence into phrases
   such as "Thank you." The voice-activity filter must be on and no window
   may be conditioned on the one before. Pinned in Task 3.
2. **A quote whose meaning changed scores high.** Dropping "not", changing
   12% to 15% or "I" to "we" is one edit in ten words. Each must be
   rejected. A changed word the guards cannot know (an antonym) must at
   least show the transcript's words. Pinned in Tasks 1 and 4.
3. **The worker dies, never starts, or starts twice.** The report still
   arrives, a second worker does nothing, and a finished step is never paid
   for again. Pinned in Task 5.
4. **There is no usable recording:** the upload failed, the file is gone,
   or it holds no speech. The report is graded from the live text and shows
   no play buttons. A track that starts before the call went live (a
   negative `start_ms`) still seeks to the right place. Pinned in Tasks 4
   and 7.
5. **The signed link expires, or the recording is purged, while the report
   page is open.** The next click still plays after one renewal, and a
   recording that is gone leaves plain quotes, not a broken player. Pinned
   in Task 7.

---

## File Structure

```
server/mockingbird/quotes.py               tokenize(), locate_quote(), QuoteMatch, span_text()
server/mockingbird/models.py               + Turn.start_ms, Word, PostCallJob
server/migrations/005_words.sql            turns.start_ms, words table, quotes.match_ratio
server/migrations/006_post_call_jobs.sql   post_call_jobs table
server/mockingbird/store.py                + words, turn start times, timed quotes, job methods
server/mockingbird/brain.py                commit_spoken(at_ms=None) stamps the reply's start; wrap-up line
server/mockingbird/recording.py            + CallRecorder.now_ms()
server/mockingbird/audio_store.py          + get_wav, playback_url, LocalAudioStore.file_path
server/mockingbird/transcribe.py           Transcriber, FasterWhisperTranscriber, to_call_clock, CLI
server/mockingbird/postcall.py             split_by_question(), recording_turns(), run_post_call()
server/mockingbird/report.py               quote objects, whole-word verify_quotes(), verify_quotes_timed()
server/mockingbird/jobs.py                 run_job(), start_job(), reconcile_once(), fall_back(), give_up()
server/mockingbird/launch.py               Launcher, InProcessLauncher, SubprocessLauncher, EcsLauncher
server/mockingbird/worker.py               python -m mockingbird.worker <interview_id>
server/mockingbird/logs.py                 configure_logging(), for the worker and the server
server/mockingbird/config.py               + WorkerSettings, Settings.worker_ecs
server/mockingbird/app.py                  wire transcriber, launcher and the reconciler
server/mockingbird/sessions.py             run_sweeper calls the reconciler
server/mockingbird/routes_ws.py            stamp reply starts; upload, then start the job
server/mockingbird/routes_api.py           report without dropped_quotes; + GET /api/interviews/{id}/recording
worker.Dockerfile                          the worker image, model baked in
infra/worker.tf, infra/iam.tf, infra/ecs.tf, infra/outputs.tf, infra/variables.tf
scripts/deploy.sh, scripts/smoke_call.py, .github/workflows/ci.yml, pyproject.toml, uv.lock
web/src/api.ts                             Quote type, Recording, getRecording()
web/src/recordingLink.ts                   keepRecording(): the signed link, renewed before it expires
web/src/quotePlayer.ts                     playbackWindow(), QuotePlayer, playQuote()
web/src/pages/Report.tsx, Landing.tsx, web/src/styles.css
README.md
```

---

### Task 1: Locate a quote in a list of words

**Files:**
- Create: `server/mockingbird/quotes.py`
- Test: `server/tests/test_quotes.py`

**Interfaces:**
- Produces:
  - `tokenize(text: str) -> list[str]`: the one tokenizer used for both the
    quote and the transcript.
  - `@dataclass(frozen=True) class QuoteMatch`: `first: int`, `last: int`
    (indices into `words`, inclusive), `ratio: float` (1.0 for an exact
    match).
  - `locate_quote(quote: str, words: Sequence[str]) -> QuoteMatch | None`:
    `words` are the transcript's words in time order, as the transcriber
    wrote them.
  - `span_text(words: Sequence[str], match: QuoteMatch) -> str`: the words
    from `first` to `last`, joined by single spaces.
  - Constants: `FILLERS`, `NEGATIONS`, `FIRST_PERSON`.

**The algorithm.** The tests pin its results. This is the rule they pin.
Ruling P3-R8 tightened it during the build: hyphens split, `'d` and `let's`
expand, number words gained plurals and more ordinals, fillers never count
on either side, a match is whole words, and the guard reads every cheapest
alignment and the words just outside the match. Ruling P3-R11 then made the
guard read every token of those words.

1. `tokenize`, in this order:
   - Lowercase.
   - Expand contractions where they end a word: `let's` to `let us`
     (whole word), `can't` and `cannot` to `can not`, `won't` to
     `will not`, any other `n't` to ` not`, `'m` to ` am`, `'re` to
     ` are`, `'ve` to ` have`, `'ll` to ` will`, and `'d` to ` d` (so the
     pronoun in "I'd", "we'd", "they'd" stands alone; `d` is left undecided
     between would and had). An apostrophe is `'`, `’`, `‘` or `ʼ`. A
     quote mark before a word is not a contraction: `'my` stays `my`.
   - Turn `%` into ` percent `.
   - Delete the remaining apostrophes. Turn every other punctuation
     character into a space (`string.punctuation`, plus any character whose
     Unicode category starts with `P`). This differs from phase 1's
     `normalize`, which deletes punctuation and would join `cause—and` into
     one word. A hyphen splits like any other punctuation, so `twenty-five`
     is `twenty`, `five` and `can't-do` is `can`, `not`, `do`. Joined into
     one token, a guarded word would hide from the guard.
   - Split on whitespace.
2. Fillers never count, on either side. Tokenize the quote and drop its
   fillers: that is `q`. Tokenize each word of `words`, drop fillers, and
   keep `owner[i]`, the index of the word each kept token came from. A word
   with no tokens left (a lone dash, an "um") adds none. If `q` or the
   transcript has no tokens, return None.
3. Find the best span with Sellers' algorithm, a Levenshtein table whose
   first row lets a match start anywhere, aligned to whole words:
   - A span may start only on the first token of a word, and end only on
     the last token of a word. In the table's first row, a column that is
     not a word's start costs the number of that word's tokens before it
     (they are inside the span as insertions), with the word's start as
     the span's start. Only columns at a word's end are candidate ends.
     Without this, a quote that stops inside a word ("I knew we could"
     against "couldn't", "4% of users" against "0.4% of users") scores as
     an exact match.
   - A substitution, an insertion and a deletion each cost 1.
   - Each cell holds `(cost, -matched, start)` and compares as a whole
     tuple: the cheaper alignment wins, then the one that matches more
     tokens, then the one that starts earlier.
   - Pick the end by `(cost, -matched, abs(span_length - len(q)), end)`,
     smallest first, where `span_length` is the span's token count.
4. `said` is the span's tokens. `denominator = max(len(q), len(said))`, and
   `ratio = 1 - cost / denominator`.
5. Accept when `cost == 0`, at any length.
6. Otherwise accept only when both hold:
   - `5 * cost <= denominator` (a ratio of 0.80 or more, compared in
     integers). No length rule is needed: with 3 quote tokens or fewer, one
     edit already puts the ratio under 0.80.
   - No guarded token is touched. Touched tokens are collected over every
     cheapest alignment of `q` against `said` (a forward and a backward
     Levenshtein table; an edit is on a cheapest alignment when
     `forward + 1 + backward` equals the total): both tokens of each
     substitution, the quote token of each deletion, the transcript token
     of each insertion. Plus the edge rule: when a cheapest alignment's
     first step substitutes or drops a quote token, every token of the
     transcript word just before the span (if there is one) counts as
     touched, and likewise every token of the word just after the span for
     the last step. Without it, "He owned the fix end to end" against "I
     really owned the fix end to end." and "...my answer was yes" against
     "...my honest answer was no." are kept, because the cheapest span stops
     short of the changed word. It reads the whole word because the span is
     whole words, so its edge is a whole word, and a contraction such as
     "I'm" holds a guarded token behind an unguarded one: reading only the
     token beside the span, "He's proud" against "I'm proud" would read `am`
     and never `i`. A token is guarded when it is in `NEGATIONS`, in
     `FIRST_PERSON`, holds a digit, or is a number word.
7. `first = owner[span_start]`, `last = owner[span_end - 1]`. Round `ratio`
   to 3 places.

The guard errs toward dropping a quote. Three kinds of faithful quote are
dropped on purpose, and the tests pin all three. Each costs a kept quote
and never shows a wrong one.
- A guarded word the transcript repeats: "and then I led" against "and
  then I I led".
- A word the grader added at the quote's start or end, with a guarded word
  one or two words out in the transcript. The added word can read as a
  change to the transcript word beside the match: at the start because the
  earlier start wins a tie, at the end because the run nearest the quote's
  length wins. In "Honestly, I led" against "No, I led", that word is the
  guarded "No". When it is not guarded, the edge rule reads the word past
  it. On the fixture, "Overall latency dropped by twenty-five percent after
  the second release" reads `Overall` in place of `planned.`, then `we`.
  "I led the migration of our billing service to the new queue, overall"
  reads `overall` in place of `and`, then the `I` after `uh`. A scratch
  check during the build cut 29 runs at clause edges from four answers and
  added each of ten unguarded words (so, then, well, really, also,
  honestly, overall, basically, and, but) to each run: 290 quotes a side.
  With the word added at the start, 80 (28%) were dropped: 20 for a
  guarded word beside the match and 60 for one two words out. With it
  added at the end, 161 (56%) were dropped: 80 beside and 81 two words out.
  Ten of those came with ruling P3-R11, all from one run that ends just
  before "Let's", whose `us` is guarded.
  The same shape can hide a meaning change, which is why these are
  dropped: "Yes, the cutover went fine" against "No, honestly, the cutover
  went fine", or "my answer was yes" against "my answer was, honestly,
  no.", is a plain word in place of a guarded one with the word beside it
  left out. Neither `yes` nor `honestly` is guarded, so to the guard these
  look the same as the added-word quotes. Keeping those without keeping
  these would take a new list of words.
- A closed compound the grader wrote for a hyphenated transcript word that
  holds a guarded word. A hyphen splits, so "fiveperson" against
  "five-person" reads as a change to the guarded `five`.

**Accepted by ruling P3-R10.** The added-word drops, with a guarded word
one or two words out, are the cost of a guard that never shows a wrong
quote, and the rows below pin them. They will be revisited with Task 4's
kept and dropped counts from live calls: the dropped count takes in every
dropped quote, so it bounds this cost but cannot single it out.

The sets:
- `FILLERS`: um, uh, uhm, erm, er, ah, hmm, mm, mhm.
- `NEGATIONS`: not, no, never, nothing, nobody, none, neither, nor,
  without.
- `FIRST_PERSON`: i, me, my, mine, myself, we, us, our, ours, ourselves.
  "you" is not in it, so a dropped "you know" still passes.
- A number word matches `^(?:UNIT)*(?:UNIT|ORDINAL|OTHER)s?$`, or is one of
  the decades twenties, thirties, forties, fifties, sixties, seventies,
  eighties, nineties. So `hundreds`, `thousands`, `dozens`, `tens`,
  `seconds`, `doubled`, `fourteenth` and `twentyfive` are all guarded.
  - `UNIT`: zero to nineteen, the tens from twenty to ninety, hundred,
    thousand, million, billion, trillion.
  - `ORDINAL`: first to twelfth, thirteenth to nineteenth, twentieth,
    thirtieth, fortieth, fiftieth, sixtieth, seventieth, eightieth,
    ninetieth, hundredth, thousandth, millionth, billionth, trillionth.
  - `OTHER`: once, twice, thrice, half, halves, halve, halved, double,
    doubled, triple, tripled, quadruple, quadrupled, dozen, percent.

**The fixture.** The `locate_quote` table below runs against this answer,
split on spaces into 68 words (index 0 is `So,`, index 43 is the lone `-`):

> So, um, I led the migration of our billing service to the new queue, and
> uh I did not skip the load test. We cut errors from 12% to 0.4% in two
> weeks, like, you know, which was much earlier than we planned. - Latency
> dropped by twenty-five percent after the second release. I'm proud that I
> owned the fix end to end. The fix shipped on Friday.

- [ ] **Step 1: Write the failing tests**

  `tokenize`, each exact:

  | Input | Tokens |
  |---|---|
  | `e-commerce` | `e`, `commerce` |
  | `twenty-five percent` | `twenty`, `five`, `percent` |
  | `can't-do` | `can`, `not`, `do` |
  | `We-we` | `we`, `we` |
  | `12-15` | `12`, `15` |
  | `12%` | `12`, `percent` |
  | `0.4%` | `0`, `4`, `percent` |
  | `can't`, `cannot` | `can`, `not` |
  | `won't` | `will`, `not` |
  | `didn't` | `did`, `not` |
  | `I'm` | `i`, `am` |
  | `we've` | `we`, `have` |
  | `they'll` | `they`, `will` |
  | `You're` | `you`, `are` |
  | `I'd` | `i`, `d` |
  | `let's`, `Let’s` | `let`, `us` |
  | `cause—and` | `cause`, `and` |
  | `O’Brien's` | `obriens` |
  | `'my team'` | `my`, `team` |
  | `…` | (none) |

  `locate_quote`, parametrized. "Shown" is `span_text` of the match:

  | Quote | Result |
  |---|---|
  | `I led the migration of our billing service` | first 2, last 9, ratio 1.0 |
  | `“I Led the migration — of our billing service.”` | first 2, last 9, ratio 1.0 |
  | `So I led the migration` (the "um" is dropped) | first 0, last 5, ratio 1.0 |
  | `and I did not skip the load test` | first 14, last 22, ratio 1.0 |
  | `I am proud that I owned the fix` | first 53, last 59, ratio 1.0. Shown starts `I'm proud` |
  | `migration` | first 5, last 5, ratio 1.0 |
  | `the fix` (said twice) | first 58, last 59: the earlier one |
  | `the fix shipped on Friday` | first 63, last 67 |
  | `than we planned. Latency dropped by twenty-five percent` | first 40, last 48, ratio 1.0: the lone `-` costs nothing |
  | `We cut errors from 12% to 0.4% in two weeks, which was much earlier than we planned` | first 23, last 42, ratio 0.87. Shown keeps `like, you know,` |
  | `which was much earlier than we expected` | first 36, last 42, ratio 0.857. Shown ends `planned.` |
  | `I personally owned the fix end to end` | first 56, last 62, ratio 0.875. Shown is `I owned the fix end to end.` |
  | `the migration our billing` | first 4, last 8, ratio 0.8: exactly at the line |
  | `Latency rose by twenty-five percent after the second release` | first 44, last 52, ratio 0.9. Shown says `dropped`. No word list can catch an antonym, which is why the report shows the transcript's words |
  | `So I owned the fix end to end` | first 55, last 62, ratio 0.875. Shown starts `that I owned`: `So` in place of `that` ties with `So` never said, and the earlier start wins |
  | `I did skip the load test` | None: the edit touches `not` |
  | `We cut errors from 15% to 0.4% in two weeks` | None: the edit touches a number |
  | `in three weeks, like, you know, which was much earlier` | None: `three` for `two` |
  | `We led the migration of our billing service to the new queue` | None: `we` for `i` |
  | `Latency dropped by thirty-five percent after the second release` | None: `thirty` for `twenty`, inside a compound |
  | `Latency dropped by twenty-five percent after the third release` | None: an ordinal |
  | `which was not much earlier than we planned` | None: the quote adds `not` |
  | `which was never earlier than we planned` | None: `never` for `much`, guarded on the quote's side only |
  | `which was much earlier than they planned` | None: `they` for `we`, guarded on the transcript's side only |
  | `We cut errors from 0.4% to 12% in two weeks, like, you know, which was much earlier than we planned` | None: the two numbers swapped (ratio 0.826) |
  | `No, so I led the migration of our billing service` | None: the quote adds `no` at its very start (ratio 0.9) |
  | `He owned the fix end to end` | None: `he` for `i` at the quote's start |
  | `They cut errors from 12% to 0.4% in two weeks` | None: `they` for `we` at the quote's start |
  | `We cut errors from 12 to 0.4% in two weeks` | None: the `percent` of `12%` is left out |
  | `in two weeks, which was much earlier than we planned` | None: 3 words left out of 13 is under 80% |
  | `I led the migration and we cut errors` | None: a splice of two places |
  | `I rewrote the scheduler in Rust over a weekend` | None: invented |
  | `skip the test` | None: one word left out of 4 is 75% |
  | `skip the unit test` | None: one edit in 4 is 75% |
  | `um` | None: a quote of fillers only has no tokens |
  | an empty string, and `… —` | None |

  Every None row the guard rejects is found once `_guarded` is patched to
  return False, and the tests pin where: `I did skip the load test`, for
  one, is then first 16, last 22, ratio 0.857. Every other None row stays
  None. So no guarded row can pass by failing the 80% line instead.

  With their own words, split on spaces, or with the fixture where the
  table says so ("the same with" changes only the words named):

  | Transcript | Quote | Result |
  |---|---|---|
  | `I'd have shipped the whole fix end to end by Friday` | `I would have shipped the whole fix end to end by Friday` | first 0, last 10, ratio 0.917 |
  | `Let's say we sharded the queue by region` | `Let us say we sharded the queue by region` | first 0, last 7, ratio 1.0 |
  | `I knew we couldn't. Later I knew we could.` | `I knew we could` | first 5, last 8, ratio 1.0. Shown `I knew we could.` |
  | `we cut latency from 12-15 ms to 4 ms` | the same with an en dash in `12–15` | first 0, last 8, ratio 1.0 |
  | `I led the migration` | `um I led` | first 0, last 1, ratio 1.0: the quote's filler does not count |
  | `I personally owned the rollout plan.` | `I owned the rollout plan` | first 0, last 5, ratio 0.833: a word left out beats `I` changed into `personally` |
  | `I talked to every single customer.` | `I talked to every customer` | first 0, last 5, ratio 0.833. Shown keeps `customer.` |
  | `I'd have shipped the whole fix end to end by Friday` | `We'd have shipped the whole fix end to end by Friday` | None: `we` for `i` |
  | `They'd already shipped the fix by the time I joined` | `We'd already shipped the fix by the time I joined` | None: `we` for `they` |
  | `We had a five-person team` | `We had a six-person team` | None: `six` for `five` |
  | `we finished the whole rewrite in a two-week sprint last spring` | the same with `three-week` | None: `three` for `two` |
  | `the launch was not-so-great for the whole team this quarter` | the same with `so-great` | None: drops `not` |
  | `everyone on the team had a can't-do attitude about it` | the same with `can-do` | None: drops `not` |
  | `we migrated hundreds of customers to the new billing service` | the same with `thousands` | None: a plural number word |
  | `We doubled the throughput of the billing service in two weeks` | the same with `tripled` | None |
  | `it was the thirteenth release of the billing service this year` | the same with `fourteenth` | None |
  | `The old billing service was just not fast enough for us` | the same with `not just` | None: one cheapest alignment never touches `not`, another does |
  | `I knew we couldn't.` | `I knew we could` | None: the match takes all of `couldn't.`, so the edit is `not` |
  | `0.4% of users saw errors` | `4% of users saw errors` | None: the match takes all of `0.4%` |
  | `1.5 million dollars in revenue` | `5 million dollars in revenue` | None: the match takes all of `1.5` |
  | `revenue grew 12.5%` | `revenue grew 12` | None: the cheapest match is `revenue grew`, with `12` left out, one edit in 3. Running on to the end of `12.5%` costs two |
  | `We-we led the migration of our billing service` | `They led the migration of our billing service` | None: the match starts at `led`, and the edge rule reads the `we` before it |
  | `When they asked if we could ship it in two weeks, my honest answer was no.` | `When they asked if we could ship it in two weeks, my answer was yes` | None: the match stops at `was`, and the edge rule reads `no` |
  | `I really owned the fix end to end.` | `He owned the fix end to end` | None: the match starts at `really`, and the edge rule reads `I` |
  | `I'm proud that I owned the fix end to end.` | `He's proud that I owned the fix end to end` | None: the match starts at `proud`, and the edge rule reads all of `I'm`, so its `i` |
  | `We've shipped every release on time since March.` | `Everyone's shipped every release on time since March` | None: the same with the `we` of `We've` |
  | `No, honestly, the cutover went fine for the billing team that week.` | `Yes, the cutover went fine for the billing team that week.` | None: the match starts at `honestly,`, and the edge rule reads `No` |
  | `When they asked if we could ship it in two weeks, my answer was, honestly, no.` | `When they asked if we could ship it in two weeks, my answer was yes` | None: the match stops at `honestly,`, and the edge rule reads `no` |
  | `I, uh, owned the fix end to end.` | `He owned the fix end to end` | None: the filler changes nothing |
  | `That part was, um, owned by me.` | `That part was owned by him` | None: the filler changes nothing |
  | `and then I I led the migration of our billing service` | `and then I led the migration of our billing service` | None, on purpose: a guarded word said twice |
  | `No, I led the migration of our billing service` | `Honestly, I led the migration of our billing service` | None, on purpose: an added edge word beside a guarded one |
  | the fixture | `Overall latency dropped by twenty-five percent after the second release` | None, on purpose: `Overall` reads as a change to `planned.`, and the edge rule reads `we`, two words out |
  | the fixture | `And latency dropped by twenty-five percent after the second release` | None, on purpose: the same with `And`, which reads as a change to `planned.` |
  | the fixture | `I led the migration of our billing service to the new queue, overall` | None, on purpose: `overall` reads as a change to `and`, and the edge rule reads the `I` after `uh`, two words out |
  | `I'm proud that I owned the fix end to end.` | `So proud that I owned the fix end to end` | None, on purpose: `So` reads as left out, and the edge rule reads the `i` of `I'm`, beside the match |
  | `We had a five-person team for the whole migration.` | `We had a fiveperson team for the whole migration` | None, on purpose: a closed compound, which reads as a change to the guarded `five` |

  Each guarded row catches a different wrong build: one that reads only
  the transcript's side of an edit, one that reads only the quote's side,
  one that compares the two sides as bags of words, one that misses an edit
  on the alignment table's edge, one whose match can start or stop inside a
  word, one without the edge rule, one whose edge rule reads past a quote
  word left out but not past a changed one, one whose edge rule reads a
  single token of the word outside the match, and one that reads a single
  cheapest alignment, the one that leaves a swapped `not` alone.

  Also:
  - Every number word is guarded: for each word of `UNIT`, `ORDINAL` and
    `OTHER`, each `UNIT` word with an `s` added, every decade, and
    `twentyfive`, `locate_quote(f"we shipped the fix on friday {w}",
    "we shipped the fix on friday".split())` is None. With `today` it is
    first 0, last 5, ratio 0.857.
  - Putting `um,` before any word of the fixture, or at its end, changes no
    row of the fixture table: not its verdict, not its ratio. First and
    last shift by the inserted word only.
  - `locate_quote("anything", [])` is None.
  - A quote of 30 invented words against a 5-word transcript is None.
  - `span_text` joins with single spaces and keeps the words' own
    punctuation and capitals.
  - Assigning to a field of a `QuoteMatch` raises
    `dataclasses.FrozenInstanceError`.
  - The module imports nothing outside the standard library, and not
    `logging`, `io` or `os`, and never calls `print` or `open`.

- [ ] **Step 2: Run them.** `uv run pytest -q server/tests/test_quotes.py`.
  Expected: FAIL, no module named `mockingbird.quotes`.
- [ ] **Step 3: Implement `quotes.py`** as the rule above describes. Keep
  it pure: no I/O, no logging.
- [ ] **Step 4: Run them.** `uv run pytest -q` and `uv run ruff check .`.
  Expected: PASS.
- [ ] **Step 5: Commit**

  `git commit -m "Locate a quote in a transcript word by word"`

---

### Task 2: Reply start times and a table of words

**Files:**
- Create: `server/migrations/005_words.sql`
- Modify: `server/mockingbird/models.py`, `server/mockingbird/store.py`,
  `server/mockingbird/brain.py`, `server/mockingbird/recording.py`,
  `server/mockingbird/routes_ws.py`
- Test: `server/tests/test_store.py`, `server/tests/test_brain.py`,
  `server/tests/test_recording.py`, `server/tests/test_routes.py`

**Interfaces:**
- Produces:
  - `Turn.start_ms: float | None = None`. For an interviewer reply: when
    Vapi began speaking it, in ms since the call went live. None for the
    opening line, for every candidate turn, and for turns saved before this
    change.
  - `@dataclass(frozen=True) class Word` in `models.py`: `idx: int` (its
    place in the track's transcript, from 0), `text: str` (as the
    transcriber wrote it), `start_ms: int`, `end_ms: int`.
  - `CallRecorder.now_ms() -> float | None`: ms since `start()`, or None
    before it.
  - `Brain.commit_spoken(at_ms: float | None = None) -> bool`. The draft it
    commits remembers `at_ms`, and the interviewer turn written for that
    draft later carries it as `start_ms`.
  - `Store.add_turn` and `Store.list_turns` keep and return `start_ms`.
  - `Store.add_words(interview_id: str, speaker: str, words: list[Word])
    -> None`: replaces any words that interview already has for that
    speaker, all or nothing. KeyError for an unknown interview.
  - `Store.list_words(interview_id: str, speaker: str) -> list[Word]`, in
    order of `idx`. Empty for an unknown interview.
- `005_words.sql`:
  - `alter table turns add column start_ms double precision`
  - `words`: `interview_id text not null references interviews (id) on
    delete cascade`, `speaker text not null` checked to be `interviewer` or
    `candidate`, `idx integer not null`, `word text not null`, `start_ms
    integer not null`, `end_ms integer not null`, primary key
    `(interview_id, speaker, idx)`. Row level security on, no policies.
  - `alter table quotes add column match_ratio double precision`

- [ ] **Step 1: Write the failing tests**
  - Brain:
    - `commit_spoken(at_ms=1234.5)`, then the next `draft()` or
      `finalize()`: the interviewer turn it writes has `start_ms == 1234.5`.
    - The opening line and every candidate turn have `start_ms` None.
    - `commit_spoken()` with no argument leaves `start_ms` None.
    - `commit_spoken(at_ms=...)` with no draft pending returns False and
      changes no turn.
  - Recorder: `now_ms()` is None before `start()`, and follows the injected
    clock after it.
  - Store, on `MemoryStore` and `PostgresStore`:
    - a turn's `start_ms` round-trips, None included
    - `add_words` then `list_words` round-trips in `idx` order
    - a second `add_words` for the same speaker replaces the first
    - words of one speaker do not show up for the other
    - `add_words` for an unknown interview raises KeyError
    - deleting old demo data deletes its words
  - Migrations:
    - `apply_migrations` on a database that already holds turns adds the
      column and leaves the rows.
    - add `words` to the set in `test_the_real_migrations_create_every_table`
      and `"words": True` to the map in
      `test_row_level_security_is_on_for_every_table`. Both compare every
      table in the schema, so any new table fails them, and the second is
      what proves the candidate's words are not served by Supabase's REST
      API.
  - Route: after a fake call with one reply, the saved interviewer reply
    turn has a `start_ms` taken from the recorder's clock, and the opening
    line has None.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.** In `routes_ws._live_events`, the
  assistant-started branch passes `session.recorder.now_ms()` (None when
  there is no recorder) to `commit_spoken`. The recorder's clock is the one
  the WAV sits on, so it is the one to use.
- [ ] **Step 4: Run them.** `uv run pytest -q` and `uv run ruff check .`.
  Expected: PASS. The Postgres cases, the two schema tests among them, skip
  without `TEST_DATABASE_URL` (see "This machine" in Global Constraints).
- [ ] **Step 5: Commit**

  `git commit -m "Stamp when each reply starts, and keep a transcript's words"`

---

### Task 3: Transcribe a recording into timed words

**Files:**
- Create: `server/mockingbird/transcribe.py`
- Modify: `server/mockingbird/audio_store.py`, `pyproject.toml`, `uv.lock`,
  `.github/workflows/ci.yml`, `README.md`
- Test: `server/tests/test_transcribe.py`,
  `server/tests/test_audio_store.py`

**Interfaces:**
- Consumes: `Word` (Task 2), `recording.wav_bytes`.
- Produces:
  - `AudioStore.get_wav(path: str) -> bytes`.
    - `LocalAudioStore`: reads the file in a thread. A missing file raises
      `FileNotFoundError`. A path that escapes the root raises `ValueError`.
    - `SupabaseAudioStore`: `GET {url}/storage/v1/object/{bucket}/{path}`
      with the same two key headers as every other call. A non-2xx raises
      `AudioStoreError(status)`.
  - `@dataclass(frozen=True) class TimedWord`: `text: str`,
    `start_s: float`, `end_s: float`, offsets into the file.
  - `class Transcriber(Protocol)`:
    `transcribe(self, wav: bytes) -> list[TimedWord]`. Synchronous and
    CPU-bound: callers run it in a thread.
  - `pcm_frames(wav: bytes) -> bytes`: the raw s16le samples of a WAV, read
    with the standard library's `wave`. Raises `ValueError` unless the file
    is mono, 16-bit and 16 kHz.
  - `class FasterWhisperTranscriber(model_dir: str, *, cpu_threads: int =
    0, beam_size: int = 1, model_factory: Callable[..., Any] | None =
    None)`:
    - It imports `faster_whisper` and `numpy` inside itself, so importing
      the module never needs them.
    - The model is built once, as `WhisperModel(model_dir, device="cpu",
      compute_type="int8", cpu_threads=cpu_threads)`, or by `model_factory`
      with the same arguments.
    - `transcribe` passes a float32 array (samples divided by 32768) and
      the Global Constraints' settings.
    - What faster-whisper returns: `model.transcribe(audio, ...)` gives
      `(segments, info)`. `segments` is a generator, so `transcribe` reads
      it to the end. `segment.words` is a list, or None, of objects with
      `.word`, `.start` and `.end` (seconds). `.word` is raw text that
      starts with a space when it opens a new written word. The library
      opens a new entry at a hyphen, `%`, `/`, decimal point, comma or
      colon inside a word, so `twenty-five` arrives as ` twenty`, `-five`,
      and `0.4%` as ` 0`, `.4`, `%`.
    - Within one segment, an entry whose `.word` does not start with a
      space is joined to the entry before it: its text is appended and its
      end becomes the word's end. The first entry of a segment always
      opens a word, so nothing is joined across the silence between
      segments.
    - After joining, it strips each text, leaves out a blank one, and never
      returns an `end_s` below its `start_s`.
  - `to_call_clock(words: list[TimedWord], track_start_ms: float) ->
    list[Word]`: `idx` from 0, times rounded to whole ms on the call's
    clock.
  - `python -m mockingbird.transcribe <wav> [--model-dir DIR]`: prints one
    JSON object per word (`text`, `start_s`, `end_s`) to stdout, and one
    line to stderr with the audio seconds, the wall seconds and their
    ratio. `--model-dir` defaults to the `WHISPER_MODEL_DIR` environment
    variable.
- `pyproject.toml`:
  - `[project.optional-dependencies]` `worker = ["faster-whisper==1.2.1",
    "numpy>=2"]`.
  - `[tool.uv] exclude-newer = "2026-10-31T00:00:00Z"`, with a comment that
    says why and that the date is raised when dependencies are upgraded.
  - If `uv lock` would change an existing pin, move the date until it does
    not, and say which date was used in the report.
- CI: the python job runs `uv sync --locked --all-extras`. README's CI
  section says so.

- [ ] **Step 1: Write the failing tests**
  - Local store: `get_wav` returns what `put_wav` wrote; a missing file
    raises `FileNotFoundError`; `"../x"` raises `ValueError`.
  - Supabase store, with `respx`: the exact URL, method and headers; a 404
    raises `AudioStoreError` whose text holds neither the key nor
    `/storage/v1`.
  - `pcm_frames`: accepts `wav_bytes(pcm)` and returns `pcm`. A stereo
    file, an 8-bit file, a 44.1 kHz file and bytes that are not a WAV each
    raise `ValueError`.
  - `to_call_clock`: adds the track start, including a negative one;
    rounds to whole ms; numbers `idx` from 0; an empty list gives an empty
    list.
  - `FasterWhisperTranscriber`, behind `pytest.importorskip("numpy")`, with
    a fake `model_factory`:
    - the factory gets `device="cpu"`, `compute_type="int8"` and the thread
      count
    - `transcribe` is called with `word_timestamps=True`,
      `vad_filter=True`, `language="en"`,
      `condition_on_previous_text=False` and the beam size (Review Focus 1)
    - the audio it gets is float32, as long as the sample count, and within
      -1 to 1
    - words from several segments come back in order, stripped, blank ones
      left out. The fake's words carry leading spaces, as the library's do.
    - one fake segment yielding ` twenty`, `-five`, ` 12`, `%`, ` 0`, `.4`,
      `%` comes back as `twenty-five`, `12%`, `0.4%`, each with its first
      piece's start and its last piece's end
    - a segment whose first word has no leading space keeps it as a word of
      its own, not joined to the segment before
    - a word whose end is below its start comes back with its end equal to
      its start
    - a WAV with no samples returns no words without calling the model
  - Behind `pytest.importorskip("faster_whisper")` (it runs in CI, which
    installs every extra): `WhisperModel.transcribe` takes every keyword
    the transcriber passes. A cheap guard against a mistyped keyword.
  - Importing `mockingbird.transcribe` works with neither numpy nor
    faster-whisper installed (run in a subprocess with both blocked through
    `sys.modules`).
  - The CLI, with the fake transcriber injected into `main`: prints one
    JSON line per word and exits 0; a missing file exits 2 with a one-line
    message.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.** Run `uv lock`, and check its diff adds
  packages only. Install neither the `worker` extra nor a model here (see
  "This machine" in Global Constraints); run the numpy tests with
  `uv run --with numpy pytest -q server/tests/test_transcribe.py`.
- [ ] **Step 4: Run them.** `uv run pytest -q` and `uv run ruff check .`.
  Expected: PASS.
- [ ] **Step 5: Commit**

  `git commit -m "Transcribe a recording into words with timestamps"`

---

### Task 4: Grade from the recording's transcript

**Files:**
- Create: `server/mockingbird/postcall.py`
- Modify: `server/mockingbird/report.py`, `server/mockingbird/store.py`,
  `server/mockingbird/app.py`, `server/mockingbird/routes_ws.py`,
  `server/mockingbird/routes_api.py`, `web/src/api.ts`,
  `web/src/pages/Report.tsx`, `README.md`
- Test: `server/tests/test_postcall.py`, `server/tests/test_report.py`,
  `server/tests/test_store.py`, `server/tests/test_routes.py`,
  `server/tests/test_smoke_call.py`, `web/src/api.test.ts`,
  `web/src/pages/Report.test.tsx`

**Interfaces:**
- Consumes: `tokenize`, `locate_quote`, `span_text` (Task 1); `Word`,
  `Turn.start_ms`, `add_words`, `list_words` (Task 2); `Transcriber`,
  `to_call_clock`, `AudioStore.get_wav` (Task 3).
- Produces:
  - A quote in a report is an object: `{"text": str, "start_ms": int |
    None, "end_ms": int | None, "match_ratio": float | None}`.
    `dropped_quotes` stay plain strings, the grader's wording. They are
    saved for the eval and never sent to the browser.
  - A graded report gains `"transcript": "recording" | "live"`.
  - `report.verify_quotes(result, candidate_text_by_q, question_texts)`:
    an exact whole-word check on the live text. It gives quote objects with
    None times and ratio, and `"transcript": "live"`.
    - Each question's answer is split on whitespace into words and
      tokenized once with `tokenize`, keeping the index of the word each
      token came from, as `locate_quote` does. It is not tokenized again
      for each quote.
    - A quote is kept only when its tokens are one contiguous run of that
      answer's tokens, the earliest run if there are several. It is a plain
      scan: no alignment table, no filler skipping, no fuzzy step. A quote
      with no tokens is dropped.
    - A kept quote's `text` is `span_text` over the words that own the run,
      so the report shows the live text's own words.
    - `normalize` and `_is_punctuation` are deleted with their tests.
      Nothing else calls them.
  - `report.verify_quotes_timed(result: GradeResult, words_by_q:
    dict[int, list[Word]], question_texts: list[str] | None = None) ->
    dict`: each quote goes through `locate_quote` against the words of the
    question it was cited for, and that question only.
    - A located quote's `text` is `span_text`, and its `start_ms` and
      `end_ms` are the first word's start and the last word's end.
    - Two quotes of one item that land on the same span are kept once.
    - An item with no kept quote has `score` None and the status
      `not_enough_evidence`.
    - The report says `"transcript": "recording"`.
  - `GRADER_SYSTEM` gains three sentences: the candidate's lines come from
    a speech-to-text transcript, so copy them exactly as written even where
    a word looks misheard; quote a full clause, about five words or more,
    where there is one; and a rationale never quotes the candidate, whose
    words go only in `quotes`.
  - `postcall.split_by_question(turns: list[Turn], words: list[Word]) ->
    dict[int, list[Word]]`: each main question's candidate words, in `idx`
    order.
    - The boundaries are the interviewer turns that have a `start_ms`.
    - A word belongs to the question of the latest such turn that started
      at or before the word's `start_ms`. When two replies start at the
      same ms, a word at that ms goes to the later one.
    - Words before the first boundary belong to the first interviewer
      turn's question.
    - Words keep `idx` order everywhere and are never sorted by time. Each
      is placed by its own `start_ms`, so words whose times run backwards
      stay in transcript order inside each question.
  - `postcall.recording_turns(turns: list[Turn], words: list[Word]) ->
    list[Turn]`: what the grader reads. The interviewer turns as they are,
    each followed by one candidate turn holding the words said from its
    start to the next boundary, joined by spaces. No candidate turn is made
    for an empty stretch. A turn with no `start_ms`, other than the first,
    opens no stretch of its own. Turns are numbered again from 0.
  - `async postcall.run_post_call(store: Store, audio: AudioStore,
    transcriber: Transcriber | None, grader: TurnGrader, interview_id: str)
    -> None`. It replaces `report.build_report`, which is deleted. It never
    raises. It leaves the interview with a saved report and the status
    `graded`, unless the store cannot be written: then it logs the
    exception type and leaves the interview `ended` with no report, as
    `build_report` did, and the job is run again (Task 5). Its steps:
    1. If the interview has a report already, make sure it is marked
       `graded` and stop. Nothing is transcribed or graded twice.
    2. Only when there is a transcriber, read the stored candidate words.
       If there are none, and `recording["status"] == "uploaded"` with a
       `candidate` `start_ms`: download `interviews/{id}/candidate.wav`,
       transcribe it in a thread (`asyncio.to_thread`), convert with
       `to_call_clock`, and save the words. With no transcriber the stored
       words are not read, so the process that carries live audio never
       runs the word-by-word check.
    3. With words: grade `recording_turns(...)` and check with
       `verify_quotes_timed`.
    4. Without words (no transcriber, even when words are stored; no
       upload; a download or transcription that failed; or a recording
       with no speech): if the live turns hold candidate text, grade them
       and check with `verify_quotes`. If they hold none, the report is
       `no_answers`.
    5. `GradingError`, any other error, and a report the store refuses
       (`ValueError`) are handled as `build_report` handled them: a
       `grading_failed` report, and only exception types in the log.
    - As each stage ends it logs one INFO line, numbers only: the
      download's seconds and the audio's length in seconds, the
      transcription's seconds, the grade's seconds. Once the report is
      built it logs one more: how many quotes were kept exactly (a ratio
      of 1.0, or None on the live path), kept below 1.0, and dropped. A
      worker killed mid-job has still
      logged the stages it finished.
  - `Store.save_report` copies each quote object's `start_ms`, `end_ms`
    and `match_ratio` into the `quotes` table. A report that still holds
    plain string quotes saves as before, with those three null.
  - The report route answers with each item's `dropped_quotes` left out.
    The stored report keeps them.
  - `create_app(..., transcriber: Transcriber | None = None)`, kept as
    `state.transcriber`. The server builds none itself.
  - `routes_ws._clean_up` starts one background step where it started two:
    upload the recording, then `run_post_call(store, state.audio_store,
    state.transcriber, state.grader, interview_id)`. Grading used to start
    beside the upload. It now follows it, because it reads the upload's
    result.
- Produces, in the web app. The server sends quote objects from this task
  on, and the page renders each quote as a React child, so it must read
  them here or every graded report with a kept quote goes blank:
  - `api.ts`: `type Quote = { text: string; start_ms: number | null;
    end_ms: number | null; match_ratio: number | null }` and
    `ItemReport.quotes: Quote[]`. `ItemReport.dropped_quotes` goes, since
    the route no longer sends it. `getReport` reads a quote that is a plain
    string (a report stored before this change) as a `Quote` with null
    times and ratio, and passes objects through.
  - `Report.tsx` renders `quote.text` as plain text, as today.
- README: the paragraph on how quotes are checked (lines 92 to 99) says
  the live check is whole-word, loses "It is a character match, not a
  whole-word match, so a quote can start or end inside a word", and says
  dropped quotes are saved but not sent to the page.

- [ ] **Step 1: Write the failing tests**
  - `verify_quotes`, Review Focus 2 on the live path, on the answer `I
    can't say the launch went well. We were unhappy with the result, and I
    didn't test it.`:
    - `happy with the result` is dropped
    - `I can` is kept and shown as `I can't`
    - `and I did` is kept and shown as `and I didn't`
    - `we saved $100` against `In the end we saved $1000 a month.` is
      dropped
    - `I “led” it,  fast!` against `so I led it fast and moved on` is kept
      and shown as `I led it fast`. This replaces the existing test that
      expects the grader's wording back.
    - an item whose only quote is dropped this way is
      `not_enough_evidence`
  - `split_by_question` and `recording_turns`:
    - words before the first stamped reply go to the opening question
    - a word that starts exactly at a boundary goes to the new stretch
    - two replies with the same `start_ms`: a word at that ms goes to the
      later one
    - two replies in one question pool their words under it
    - a reply with no `start_ms` opens no stretch, and its words stay with
      the stretch before
    - words after the wrap-up line belong to the wrap-up turn's question
    - words whose starts run backwards keep `idx` order inside each
      question
    - no words gives the interviewer turns alone
  - `verify_quotes_timed`:
    - a kept quote carries the transcript's words and the right times
    - a quote cited under question 1 that is only in question 0's words is
      dropped
    - the same span cited twice is kept once
    - an item left with no quote is `not_enough_evidence`
    - "Latency rose" from Task 1's fixture is kept and shown as "Latency
      dropped" (Review Focus 2)
  - `GRADER_SYSTEM` holds the three new sentences.
  - `run_post_call`, with `MemoryStore`, a `LocalAudioStore` in `tmp_path`
    holding a real WAV, a fake transcriber and a fake grader:
    - the recording path: the report says `recording`, the words are
      saved, and quote times are on the call's clock with a track start of
      -180.0 applied
    - a second run calls neither the transcriber nor the grader
    - a run with a transcriber, words stored and no report grades from
      them and never calls the transcriber
    - each of these gives a `live` report with null times: `recording` is
      None; its status is `upload_failed`; `get_wav` raises; the
      transcriber raises; the transcriber is None; the transcriber is None
      while words are stored, and `locate_quote` is never called; the
      transcriber returns no words while the live turns hold text (Review
      Focus 4)
    - no words and no live text gives `no_answers`
    - a grader that raises `GradingError`, one that raises `RuntimeError`,
      and a store whose `save_report` raises `ValueError` each give
      `grading_failed`
    - it never raises, and in each case above the interview ends `graded`
    - a store whose `save_report` raises `ConnectionError`: it does not
      raise, no report is saved, and the interview stays `ended`. This is
      the moved `build_report` test that says so.
    - on the recording path, `caplog` at INFO holds the three stage lines
      and the counts line, and every argument of each is a number
    - with a candidate word `zebrafish` in the transcript, the live turns
      and a dropped quote, no log record holds `zebrafish` (`caplog` at
      DEBUG)
  - Store:
    - everywhere: `_score_rows` of a report with quote objects gives
      `(text, verified, start_ms, end_ms, match_ratio)`, and of a report
      with string quotes gives None for the last three (the test at
      `test_store.py:120` runs without a database)
    - Postgres only: the `quotes` rows hold the three values, and
      `dropped_quotes` are saved with `verified` false
    - both kinds: a report with quote objects round-trips
  - Report route: an item's `dropped_quotes` are not in the answer, and
    the stored report still has them.
  - Route: after a fake call the upload finishes before grading starts,
    and a report exists.
  - `build_report`'s existing tests move to `test_postcall.py` and pass
    against `run_post_call` with no transcriber.
  - `count_report` in `scripts/smoke_call.py` counts quote objects as it
    counts strings. This test passes at once: the script needs no change.
  - Web:
    - `api.ts`: a stored report whose quotes are strings is read as quote
      objects with null times; object quotes come back unchanged. The
      existing fixtures in `api.test.ts` and `Report.test.tsx` become quote
      objects and lose `dropped_quotes`.
    - `Report.tsx`: a kept quote object shows its text.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run them.**
  - `uv run pytest -q`
  - `uv run ruff check .`
  - `cd web && npm test && npx tsc --noEmit && npm run lint && npm run build`

  Expected: PASS.
- [ ] **Step 5: Commit**

  `git commit -m "Grade from the recording's transcript and time every quote"`

---

### Task 5: Post-call jobs, the worker and its retries

**Files:**
- Create: `server/migrations/006_post_call_jobs.sql`,
  `server/mockingbird/jobs.py`, `server/mockingbird/launch.py`,
  `server/mockingbird/worker.py`, `server/mockingbird/logs.py`
- Modify: `server/mockingbird/models.py`, `server/mockingbird/store.py`,
  `server/mockingbird/config.py`, `server/mockingbird/app.py`,
  `server/mockingbird/sessions.py`, `server/mockingbird/routes_ws.py`,
  `.env.example`, `scripts/dev.sh`, `README.md`
- Test: `server/tests/test_jobs.py`, `server/tests/test_launch.py`,
  `server/tests/test_worker.py`, `server/tests/test_logs.py`,
  `server/tests/test_store.py`, `server/tests/test_routes.py`,
  `server/tests/test_app_factory.py`, `server/tests/test_config.py`,
  `server/tests/test_infra.py`

**Interfaces:**
- Consumes: `run_post_call` (Task 4), `FasterWhisperTranscriber` (Task 3).
- `006_post_call_jobs.sql`: table `post_call_jobs`, with row level
  security on and no policies:
  - `interview_id text primary key references interviews (id) on delete
    cascade`
  - `status text not null`, one of `queued`, `running`, `done`, `failed`
  - `attempts integer not null default 0`: how many times a worker
    claimed it
  - `launches integer not null default 0`: how many times a worker was
    started for it
  - `launched_at timestamptz`, `lease_until timestamptz`, `created_at
    timestamptz not null`
- Produces, in `models.py`: `@dataclass(frozen=True) class PostCallJob`:
  `interview_id: str`, `status: str`, `attempts: int`, `launches: int`.
- Produces, on `Store`, in both stores, all timed by the store's clock:
  - `enqueue_post_call(interview_id) -> None`: adds a `queued` job. Does
    nothing if the interview has one. KeyError for an unknown interview.
  - `get_post_call_job(interview_id) -> PostCallJob | None`.
  - `note_launch(interview_id) -> int`: adds one to `launches`, sets
    `launched_at` to now, and returns the new count. KeyError if there is
    no job.
  - `claim_post_call(interview_id, *, lease_s: int, max_attempts: int) ->
    bool`: in one statement, if the job is `queued`, or `running` with its
    lease run out, and `attempts < max_attempts`: make it `running`, add
    one to `attempts`, set `lease_until` to now plus `lease_s`, and return
    True. Otherwise change nothing and return False.
  - `finish_post_call(interview_id, status) -> None`, where `status` is
    `done` or `failed`. It clears the lease.
  - `post_call_jobs_due(*, launch_grace_s: int) -> list[PostCallJob]`: the
    jobs that are `queued`, or `running` with their lease run out, and
    whose `launched_at` (or `created_at`, when it was never launched) is
    more than `launch_grace_s` old. The grace holds back another launch
    while the last one may still be starting. It does not delay the first
    recovery: the shortest lease (1200 s) is far longer than the grace.
  - `ended_without_job(*, older_than_s: int) -> list[str]`: ids of
    interviews that are `ended`, have no report and no job, and were
    created more than their `cap_s` plus `older_than_s` ago. A call goes
    live within about 90 s of being created and ends within its cap, so by
    then its own clean-up has long finished.
- Produces, in `jobs.py`:
  - `MAX_ATTEMPTS = 2`, `MAX_LAUNCHES = 3`, `LAUNCH_GRACE_S = 180`,
    `BACKFILL_AFTER_S = 600`.
  - `lease_s(cap_s: int) -> int`: `600 + 2 * cap_s`.
  - `async run_job(store, audio, transcriber, grader, interview_id, *,
    max_attempts: int = MAX_ATTEMPTS) -> bool`: claim the job with the
    interview's lease, run `run_post_call`, then read
    `store.get_report(interview_id)`. With a report, mark the job `done`.
    With none (the store could not be written), leave the job `running`:
    its lease runs out and the reconciler starts it again, or falls back.
    Return True either way. Return False, having done nothing else, when
    the interview is missing or the claim fails.
  - `async start_job(store, launcher, interview_id) -> None`:
    `enqueue_post_call` (which does nothing when the job exists),
    `note_launch`, then `launcher.launch(interview_id,
    f"{interview_id}-{launches}")`. A `LaunchError` passes through.
  - `async give_up(store, interview_id) -> None`: if the interview has no
    report, save a `grading_failed` one and mark it `graded`. Mark the job
    `failed`.
  - `async fall_back(store, audio, grader, interview_id) -> None`:
    `run_job` with no transcriber and `max_attempts=MAX_ATTEMPTS + 2`, so
    the server grades the live text itself (with no transcriber,
    `run_post_call` reads no stored words) and gets two tries: a save that
    fails, or a restart during the grade, is retried one lease later. When
    `run_job` returns False, read the job: call `give_up` only when it is
    neither `done` nor `failed` and its `attempts` is at least
    `MAX_ATTEMPTS + 2`. Otherwise do nothing: a worker holds it, or it has
    finished.
  - `async reconcile_once(store, launcher, spawn_fall_back:
    Callable[[str], None]) -> None`:
    - For every id from `ended_without_job(older_than_s=
      BACKFILL_AFTER_S)`, call `start_job` at once.
    - For each job from `post_call_jobs_due(launch_grace_s=
      LAUNCH_GRACE_S)`: if `attempts < MAX_ATTEMPTS` and `launches <
      MAX_LAUNCHES`, call `start_job`. Otherwise call
      `spawn_fall_back(interview_id)`.
    - Each job is tried on its own. A failure is logged by type and the
      rest go on.
- Produces, in `launch.py`:
  - `class LaunchError(Exception)`: its text names what failed and never
    holds a URL or a key.
  - `class Launcher(Protocol)`:
    `async launch(self, interview_id: str, token: str) -> None`.
  - `InProcessLauncher(run: Callable[[str], Coroutine], spawn:
    Callable[[Coroutine], None])`: runs the job in this process, in the
    background.
  - `SubprocessLauncher(argv: Sequence[str] = (sys.executable, "-m",
    "mockingbird.worker"))`: starts `argv + [interview_id]` with no stdin,
    does not wait for it, and reaps it when it exits. A process that cannot
    start raises `LaunchError`.
- Produces, in `config.py`: `class WorkerSettings(BaseSettings)`, read the
  same way as `Settings`, with `repr=False` on each secret:
  - `anthropic_api_key: str`, `database_url: str` (both required)
  - `supabase_url`, `supabase_service_key`, `recordings_bucket`,
    `recordings_dir`, as in `Settings`
  - `whisper_model_dir: str | None = None`, `whisper_threads: int = 0`,
    `whisper_beam_size: int = 1`
- Produces, in `logs.py`: `configure_logging() -> None`:
  `logging.basicConfig(level=logging.WARNING)`, then
  `logging.getLogger("mockingbird").setLevel(logging.INFO)`. A plain Python
  process drops INFO lines, uvicorn sets up only its own loggers, and only
  `migrate.main` sets logging up today. Other libraries stay at WARNING,
  because httpx logs every request URL at INFO. It lives in its own module
  because both `worker.py` and `app.py` call it, and `worker.py` already
  imports from `app.py`.
- Produces, in `worker.py`:
  - `HARD_LIMIT_S = 3600`, with a comment that it must stay above the
    longest lease (`lease_s(1200)` is 3000 s) plus start-up.
  - `main(argv: list[str] | None = None) -> int`, run by
    `python -m mockingbird.worker <interview_id>`:
    - Its first statement, before any argument, setting or builder work,
      starts a daemon `threading.Timer(HARD_LIMIT_S, ...)` whose callback
      logs one line and calls `os._exit(1)`. It must be a thread timer: an
      asyncio limit around all of `main` does not return while asyncpg
      waits on a connection that has gone silent. The outermost `finally`
      cancels it, so tests that call `main` leave no timer behind.
    - It then calls `logs.configure_logging`, so the stage and count lines
      `run_post_call` writes reach stderr and CloudWatch.
    - An argument that is not a UUID exits 2.
    - A setting still equal to Terraform's placeholder `unset` exits 1 and
      names the variable, never its value.
    - It builds a `PostgresStore`, the audio store by `app.py`'s rule, a
      `FasterWhisperTranscriber` when `whisper_model_dir` is set (no
      transcriber when it is not, or when faster-whisper cannot be
      imported, with one warning line), and the grader with `app.py`'s
      timeout and retry values.
    - It runs `run_job` under a time limit equal to the job's lease. Past
      the limit it logs one line and ends the process with `os._exit(1)`:
      the transcription runs in a thread that cannot be cancelled. The
      hard limit covers what lies outside it: building the store, reading
      the cap, closing the pool.
    - It exits 0 when the job ran, and 0 when another worker held it.
- Wiring:
  - `app()`, the factory uvicorn calls, calls `logs.configure_logging()`
    before `create_app`, so a report the server grades itself (`fall_back`,
    or the in-process launcher in development) logs its stage and count
    lines too. The server's existing INFO lines (the purge count, why a
    relay ended) start to show as well; none holds a word the candidate
    said.
  - `create_app(..., launcher: Launcher | None = None)`, kept as
    `state.launcher`. When none is given: `SubprocessLauncher` if
    `DATABASE_URL` is set, `InProcessLauncher` if not. The default is set
    in `create_app` itself, not in the lifespan, and the in-process one
    reads `state.store`, `state.audio_store`, `state.transcriber` and
    `state.grader` when it launches: an existing test runs `_run_call` on
    an app whose lifespan never started
    (`test_a_call_cancelled_mid_relay_is_still_cleaned_up`).
  - `routes_ws._clean_up`: one background step: upload the recording, then
    `start_job`, which queues the job. The job is queued only after the
    upload has written `uploaded` or `upload_failed`, so no worker starts
    before the recording's status is known. A `LaunchError` or any other
    failure there is logged by type. If the server dies before
    `start_job`, the back-fill queues and starts the job later.
  - `run_sweeper(..., reconcile: Callable[[], Awaitable[None]] | None =
    None)`: called on every loop, after the sweep, through `_attempt`. The
    app passes a function that calls `reconcile_once` with the app's own
    launcher and spawns `fall_back` into `state.background`.
  - `.env.example` lists the worker-only settings under a heading of
    their own, and `test_env_example_lists_every_setting` compares against
    the fields of `Settings` and `WorkerSettings` together.
- README: the Report bullet (line 58) and the diagram (lines 24 to 37) say
  that a separate worker process grades the report when `DATABASE_URL` is
  set, and a background task in the server when it is not.

- [ ] **Step 1: Write the failing tests**
  - Migrations: add `post_call_jobs` to the set in
    `test_the_real_migrations_create_every_table` and
    `"post_call_jobs": True` to the map in
    `test_row_level_security_is_on_for_every_table` (Postgres only).
  - Store jobs, both kinds, with an injected clock:
    - `enqueue_post_call` twice leaves one job; `get_post_call_job` reads
      it, and gives None for an interview with no job
    - a claim on a queued job returns True, and a second claim at once
      returns False (Review Focus 3)
    - a claim after the lease ran out returns True, and `attempts` is 2
    - a claim at `max_attempts` returns False
    - `note_launch` counts up and sets `launched_at`
    - `post_call_jobs_due`:
      - a queued job inside the grace is left out and one past it is
        listed; the grace runs from `launched_at` once that is set
      - a running job is listed only after its lease runs out, and is
        still left out while its `launched_at` is inside the grace (right
        after `note_launch`)
      - a running job that was never launched is listed as soon as its
        lease runs out
      - a `done` or `failed` job never is
    - `ended_without_job` leaves out interviews that are live, abandoned,
      graded, already queued, or created less than their cap plus
      `older_than_s` ago
    - deleting old demo data deletes its job
  - `run_job`:
    - runs the work once and marks the job `done`; uses `lease_s(cap_s)`
    - returns False and calls nothing when the claim fails
    - over a store whose `save_report` raises `ConnectionError`, it
      returns True and leaves the job `running` with no report. With the
      store's clock moved past `lease_s(cap_s)` and the store writable
      again, a second `run_job` saves the report and marks the job `done`
      without transcribing again (`attempts` is 2).
  - `start_job` on an interview with no job queues one first, then
    launches it with `<id>-1`.
  - `reconcile_once`, with `MemoryStore`, a fake launcher and a fake clock:
    - a queued job past the grace is launched with the token
      `<id>-<launches>`
    - a running job whose lease ran out is launched once; a second pass 60
      s later launches nothing and does not call `spawn_fall_back`; a pass
      after the grace launches again
    - a `LaunchError` leaves the job queued, and the next job is still
      tried
    - a job at `MAX_LAUNCHES` or `MAX_ATTEMPTS` is handed to
      `spawn_fall_back` and not launched
    - an ended interview with no job, older than its cap plus
      `BACKFILL_AFTER_S`, gets a job and a launch in the same pass
  - `fall_back`:
    - grades the live text and marks the job `done`, also when candidate
      words are stored: the report says `live`, and `locate_quote` is never
      called
    - after a server try whose save failed, the next `fall_back` one lease
      later claims again (`attempts` is `MAX_ATTEMPTS + 2`) and saves the
      report
    - with the job's attempts used up (`MAX_ATTEMPTS + 2`), it saves a
      `grading_failed` report, marks the interview `graded` and the job
      `failed`, and it leaves an existing report alone
    - on a job a worker holds under a live lease, and on a `done` job, it
      writes no report and changes no status
    - over a store whose `save_report` raises `ConnectionError`, it does
      not raise, leaves the job `running` and saves no `grading_failed`
      report
  - Sweeper loop, in `test_routes.py` beside the sweeper tests, with
    `SweptStore`, `scripted_clock` and `no_wait`:
    - `run_sweeper(..., reconcile=fake)` over clock readings 0, 60 and 120
      awaits the fake once per loop, each time after that loop's sweep,
      the loops that do not purge included
    - a fake that raises on its first call leaves one ERROR line holding
      the exception type only, and the next loop still sweeps and
      reconciles
  - Through the real app, a sync test with the real sweeper (not
    `idle_sweeper`):
    - Seed before start-up with `asyncio.run`, because the sweeper
      reconciles at start and then not for 60 s. A `MemoryStore` with a
      settable clock holds an `ended` interview with a candidate turn and
      a job launched `MAX_LAUNCHES` times. Then move the clock past
      `LAUNCH_GRACE_S`.
    - Build `create_app(...)` with that store, the fake grader and a fake
      launcher. Inside `with TestClient(app)`, wait for the report route
      to answer 200: the report says `"transcript": "live"`, the job is
      `done`, and the fake launcher was never called.
  - Launchers: the in-process one spawns its coroutine; the subprocess one
    runs `argv + [id]` (a tiny script that writes a file into `tmp_path`)
    and raises `LaunchError` for a program that does not exist.
  - Worker:
    - `main`, with its builders patched to fakes: a bad id exits 2;
      `DATABASE_URL=unset` exits 1 and the output holds the name and not a
      value; a job another worker holds exits 0 without grading; a job
      that outlives its time limit ends the process (patch `os._exit`)
    - a store builder that never returns still ends the process, with
      `os._exit` patched and `HARD_LIMIT_S` patched small
    - `HARD_LIMIT_S > lease_s(1200)`
    - `main` calls `logs.configure_logging`. The in-process tests patch it,
      so its level change does not leak into other tests.
  - Logging, in `test_logs.py`: in a subprocess, `python -c` calls
    `logs.configure_logging()`, then logs one INFO line on
    `mockingbird.postcall` and one on `httpx`. stderr holds the first and
    not the second.
  - Routes:
    - after a fake call there is one job, and the launcher was called once
      with `<id>-1`, after the upload finished
    - a launcher that raises leaves the job queued and the clean-up
      finishes
    - a call that never went live queues nothing
  - App factory: the launcher chosen with and without `DATABASE_URL`, on
    an app whose lifespan never started; the sweeper is given a
    reconciler; `app()` calls `logs.configure_logging` before
    `create_app` (patched, as in the worker tests). The `idle_sweeper`
    stand-in takes the new `reconcile` keyword.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.** `scripts/dev.sh` prints which launcher the
  server will use.
- [ ] **Step 4: Run them.** `uv run pytest -q` and `uv run ruff check .`.
  Expected: PASS. The Postgres cases skip without `TEST_DATABASE_URL`, and
  shellcheck for `dev.sh` runs in CI's docker job (see "This machine" in
  Global Constraints).
- [ ] **Step 5: Commit**

  `git commit -m "Run post-call work as a job a worker claims, and retry it"`

---

### Task 6: The worker as an ECS task

**Files:**
- Create: `worker.Dockerfile`, `infra/worker.tf`
- Modify: `server/mockingbird/launch.py`, `server/mockingbird/config.py`,
  `server/mockingbird/app.py`, `pyproject.toml`, `uv.lock`, `Dockerfile`,
  `infra/iam.tf`, `infra/ecs.tf`, `infra/outputs.tf`, `infra/variables.tf`,
  `scripts/deploy.sh`, `scripts/smoke_call.py`, `.github/workflows/ci.yml`,
  `.env.example`, `README.md`
- Test: `server/tests/test_launch.py`, `server/tests/test_config.py`,
  `server/tests/test_app_factory.py`, `server/tests/test_infra.py`,
  `server/tests/test_smoke_call.py`

**Interfaces:**
- Consumes: `Launcher`, `LaunchError` (Task 5); the worker CLI (Task 5);
  the transcribe CLI (Task 3).
- Produces:
  - `EcsLauncher(run_task: Callable[..., dict], *, cluster: str,
    task_definition: str, subnets: list[str], security_groups: list[str],
    container: str = "worker")`. `launch(interview_id, token)` calls
    `run_task` in a thread with exactly:
    - `cluster`, `taskDefinition`, `launchType="FARGATE"`, `count=1`,
      `clientToken=token`
    - `networkConfiguration={"awsvpcConfiguration": {"subnets": ...,
      "securityGroups": ..., "assignPublicIp": "ENABLED"}}`
    - `overrides={"containerOverrides": [{"name": container, "command":
      ["uv", "run", "python", "-m", "mockingbird.worker", interview_id]}]}`

    A response with a non-empty `failures` list raises `LaunchError` naming
    each failure's `reason`. Any exception from `run_task` raises
    `LaunchError` naming its type only.
  - `Settings.worker_ecs: WorkerEcs | None = None`, where `WorkerEcs` is a
    pydantic model with `cluster`, `task_definition`, `subnets:
    list[str]`, `security_groups: list[str]` and `region`. It is read from
    one environment variable, `WORKER_ECS`, holding JSON. It is not a
    secret.
  - App wiring: with `worker_ecs` set, the launcher is an `EcsLauncher`
    over `boto3.client("ecs", region_name=...).run_task`. `boto3` is
    imported only then.
  - `pyproject.toml`: the extra `aws = ["boto3>=1.35"]`. Every `uv sync`
    line in the server `Dockerfile` carries `--extra aws`: `uv sync`
    removes what a line does not name, so a later sync without it would
    drop boto3.
  - `worker.Dockerfile`, built from the repo root with the same
    `.dockerignore`:
    - `python:3.12-slim` and the same pinned uv as the server image, with
      the server image's `ENV` lines (`UV_COMPILE_BYTECODE`,
      `UV_LINK_MODE`, `UV_PYTHON_DOWNLOADS`, `UV_NO_SYNC`, `UV_NO_CACHE`).
      Without the last two, `uv run` as uid 10001 tries to install at
      start.
    - `uv sync --frozen --no-dev --extra worker` on every sync line,
      dependencies first.
    - Build arguments `WHISPER_MODEL` (default `small.en`) and
      `WHISPER_REVISION` (default
      `d1d751a5f8271d482d14ca55d9e2deeebbae577f`, the commit of
      `Systran/faster-whisper-small.en` on Hugging Face). One layer
      downloads that revision into `/models/whisper` with
      `huggingface_hub.snapshot_download`, and fails when the revision is
      empty. For `tiny.en` the revision is
      `0d3d19a32d3338f10357c0889762bd8d64bbdeba`.
    - `ENV WHISPER_MODEL_DIR=/models/whisper HF_HUB_OFFLINE=1`.
    - The code, `server/data`, and the same unprivileged user as the
      server image (uid 10001).
    - No entrypoint script and no migrations.
      `CMD ["uv", "run", "python", "-m", "mockingbird.worker"]`.
  - `infra/worker.tf`:
    - ECR repository `mockingbird-worker`, with the same lifecycle policy
      as the server's.
    - A worker task role with no permissions.
    - A worker execution role, trusted by `ecs-tasks.amazonaws.com`, with
      only: `ecr:GetAuthorizationToken` on `*` (it cannot be scoped); the
      three pull actions on the worker repository; `logs:CreateLogStream`
      and `logs:PutLogEvents` on the existing log group; and
      `ssm:GetParameters` on the worker's three parameters. The three names
      are one local, used for this policy and for the task definition's
      `secrets`, so the two cannot drift.
    - A security group with outbound traffic only.
    - Task definition `mockingbird-worker`: Fargate, ARM64,
      `var.worker_cpu` (default 2048) and `var.worker_memory` (default
      4096), one container named `worker` on the worker image's `:latest`,
      the worker execution role, the environment `SUPABASE_URL`, the
      secrets `ANTHROPIC_API_KEY`, `DATABASE_URL` and
      `SUPABASE_SERVICE_KEY` and no others, and logs to the existing group
      under the prefix `worker`.
  - `infra/iam.tf`:
    - The server's execution role is unchanged.
    - The server's task role gains its first policy:
      - `ecs:RunTask` on both `task-definition/mockingbird-worker` and
        `task-definition/mockingbird-worker:*`: the launcher names the
        family without a revision, and which form IAM then checks is
        unconfirmed. It carries the condition that `ecs:cluster` is this
        cluster, and `NumericLessThanEqualsIfExists` on `ecs:task-cpu` at
        `var.worker_cpu` and on `ecs:task-memory` at `var.worker_memory`
        (`IfExists`, because the launcher sends no size override).
      - `iam:PassRole` on the worker execution role and the worker task
        role and no other role, on the condition that
        `iam:PassedToService` is `ecs-tasks.amazonaws.com`. The server's
        own execution role can read all eight secrets, and RunTask takes a
        role override and a command override, so passing that role would
        hand a task every secret.
    - The comment at the top of the file is brought up to date: four roles
      now, and the server can start worker-sized tasks with any command,
      so after a suspected takeover of the server every task in the
      cluster is stopped before the secrets are rotated.
  - `infra/ecs.tf`: the server container gets `WORKER_ECS`, a `jsonencode`
    of exactly the keys `cluster` (the cluster ARN), `task_definition`
    (the worker task definition's family), `subnets` (the public subnet
    ids), `security_groups` (a list holding the worker security group id)
    and `region`. A key that differs from `WorkerEcs` fails `Settings` at
    startup, and the service stops the old task before it starts the new
    one.
  - `infra/outputs.tf`: `worker_repo_url`.
  - `scripts/deploy.sh`: builds and pushes the worker image for
    `linux/arm64` before it restarts the service.
  - `scripts/smoke_call.py`: `REPORT_WAIT_S = 600.0`, which covers the 5
    minute estimate plus one relaunch. The poll stays at 2 s. The module
    docstring and the `--max-seconds` help say 600 s.
  - README: the CI section lists five jobs, with the new `worker` job; the
    Deploying paragraph says there are two images; the paragraph at lines
    368 to 371 no longer says grading runs in the server process from
    Vapi's text; the smoke call line (259) says 600 s.
  - CI:
    - The existing docker job also checks that the server image can
      `import boto3`.
    - A new job `worker` on `ubuntu-24.04-arm`:
      1. `uv sync --locked --extra worker`.
      2. Make a 16 kHz mono WAV of one spoken sentence with `espeak-ng`
         and `sox`, both installed with apt.
      3. Download `tiny.en` at revision
         `0d3d19a32d3338f10357c0889762bd8d64bbdeba` and run
         `uv run python -m mockingbird.transcribe` on the WAV.
      4. Check the output: at least 5 words, every `start_s` at or above
         0 and at or below its `end_s`, starts in order, and at least one
         of the sentence's distinctive words present.
      5. Build `worker.Dockerfile` with `WHISPER_MODEL=tiny.en` and that
         revision.
      6. Run the same check inside the image with `--network none` and
         the WAV mounted read-only, and check the user id is 10001.

- [ ] **Step 1: Write the failing tests**
  - `EcsLauncher` with a fake `run_task`:
    - the exact keyword arguments above
    - a `failures` entry raises `LaunchError` with its reason
    - an exception whose text holds a URL raises `LaunchError` without
      that URL
    - the call runs off the event loop
  - Settings: `WORKER_ECS` as JSON gives a `WorkerEcs`; unset gives None;
    JSON with a field missing fails at startup.
  - App factory: with `worker_ecs` set and a fake `boto3` in
    `sys.modules`, the launcher is an `EcsLauncher` built for that region.
  - `test_infra.py`:
    - the worker task definition names exactly the three secrets, and each
      is a field of `WorkerSettings`
    - its container is named `worker`, the default `EcsLauncher` uses
    - its `execution_role_arn` is the worker execution role, and that
      role's `ssm:GetParameters` resources are the same three names as the
      task definition's secrets
    - the `iam:PassRole` statement's resources are exactly the worker
      execution role and the worker task role, name neither
      `aws_iam_role.execution` nor `aws_iam_role.task`, and carry
      `iam:PassedToService`
    - the `ecs:RunTask` statement names the worker family in both forms,
      the `ecs:cluster` condition, and both size conditions, tied to
      `var.worker_cpu` and `var.worker_memory`
    - `ecs.tf` sets `WORKER_ECS`, the keys of its `jsonencode` block equal
      `WorkerEcs.model_fields`, and `worker_ecs` is in the list of
      settings that are not secrets
    - every `uv sync` line in the server `Dockerfile` has `--extra aws`
      and none has `--extra worker`; every one in `worker.Dockerfile` has
      `--extra worker`
    - `worker.Dockerfile` sets `HF_HUB_OFFLINE=1`, `UV_NO_SYNC` and
      `UV_NO_CACHE`, its `WHISPER_MODEL_DIR` is the directory the download
      step fills, and its `WHISPER_REVISION` default is 40 hex characters
    - `scripts/deploy.sh` reads the `worker_repo_url` output, and
      `outputs.tf` defines it
  - `test_smoke_call.py`: the four tests that wait for the report expect
    "no report after 600 s (...)"; the three that count asks expect `(301,
    pytest.approx(600.0))`; the tests with `120_s` in their names are
    renamed, and the helper's docstring and comment say 600.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.** Run `uv lock` for boto3. Run `terraform fmt
  -recursive infra` (it needs no providers). `terraform validate`,
  shellcheck, the image builds and the `worker` job run in CI, not here.
- [ ] **Step 4: Run them.** `uv run pytest -q`, `uv run ruff check .` and
  `terraform -chdir=infra fmt -check -recursive`. Expected: PASS.
- [ ] **Step 5: Commit**

  `git commit -m "Run the post-call worker as a one-off ECS task"`

---

### Task 7: Play a quote from the report page

**Before it starts:** Ruhan's byte-range check ("A check for Ruhan before
Task 7") answered 206. If it answered 200, this task waits and the playback
decision goes back to Ruhan.

**Files:**
- Create: `web/src/quotePlayer.ts`, `web/src/recordingLink.ts`
- Modify: `server/mockingbird/audio_store.py`,
  `server/mockingbird/routes_api.py`, `server/mockingbird/brain.py`,
  `web/src/api.ts`, `web/src/pages/Report.tsx`, `web/src/pages/Landing.tsx`,
  `web/src/styles.css`, `README.md`
- Test: `server/tests/test_audio_store.py`, `server/tests/test_routes.py`,
  `server/tests/test_brain.py`, `web/src/quotePlayer.test.ts`,
  `web/src/recordingLink.test.ts`, `web/src/api.test.ts`,
  `web/src/pages/Report.test.tsx`, `web/src/pages/Landing.test.tsx`

**Interfaces:**
- Consumes: quote objects, the `Quote` type and `"transcript"` in the
  report (Task 4); `interviews.recording` (phase 2);
  `sessions.RETENTION_DAYS`.
- Produces, on the server:
  - `AudioStore.playback_url(path: str, ttl_s: int) -> str | None`.
    - `SupabaseAudioStore`: `POST
      {url}/storage/v1/object/sign/{bucket}/{path}` with the JSON body
      `{"expiresIn": ttl_s}`. The reply's `signedURL` is relative, so the
      method returns `{url}/storage/v1` plus it. A reply without one raises
      `AudioStoreError(502)`.
    - `LocalAudioStore`: returns None, which means the server serves the
      file itself.
  - `LocalAudioStore.file_path(path: str) -> Path`: the resolved path, with
    the same escape check as every other method.
  - `GET /api/interviews/{interview_id}/recording`:
    - 200 with `{"url": str, "expires_in_s": int, "track_start_ms":
      float}` and the header `Cache-Control: no-store`.
    - `url` is the signed link, or
      `/api/interviews/{id}/recording/candidate.wav` when the store
      returned None.
    - The path signed is always `interviews/{id}/candidate.wav`, built on
      the server. The interviewer's track is never offered.
    - 404 when the id is not a UUID; the interview does not exist; its
      `recording` is not `uploaded` with a candidate `start_ms`; its saved
      report does not say `"transcript": "recording"` (no report yet, a
      `live` one, `grading_failed`); or it is a demo interview created
      more than `RETENTION_DAYS` days ago. The last rule covers a row the
      purge keeps after deleting the audio, while Vapi's copy is still
      being deleted.
    - 429 when the interview has been given `RECORDING_LINKS_PER_MIN = 10`
      links in the last 60 seconds. The count is kept in memory, per
      interview id, for at most 1000 ids. This bounds sign calls only, not
      downloads: see My call 6.
    - 502 when signing fails, with only the exception type logged.
    - `SIGNED_URL_TTL_S = 1800`.
  - `GET /api/interviews/{interview_id}/recording/candidate.wav`: a
    Starlette `FileResponse` with `media_type="audio/wav"`, which answers
    byte-range requests by itself. It serves only when the audio store is
    a `LocalAudioStore` and the file exists, under the same 404 rules. In
    production it is always 404.
  - `brain.WRAP_UP_LINE` ends "Your report will be ready in a few
    minutes."
- Produces, in the web app:
  - `api.ts`:
    - `Report` gains `transcript?: 'recording' | 'live'`.
    - `type Recording = { url: string; expires_in_s: number;
      track_start_ms: number }`.
    - `getRecording(id: string, signal?: AbortSignal): Promise<Recording |
      null>`: null for a 404, and it throws for anything else that is not
      200.
  - `recordingLink.ts` keeps the page's link fresh with no DOM, so it is
    tested the way `pollReport` is:
    - `RENEW_BEFORE_MS = 60_000`, `RETRY_AFTER_MS = 60_000`.
    - `keepRecording(ask: () => Promise<Recording | null>, timer, onChange:
      (recording: Recording | null) => void): { renew(): Promise<string |
      null>; stop(): void }`, with the same timer shape as `QuotePlayer`.
      It asks once, at once. Every answer is handled the same way, whether
      it came from that first ask, a timed ask or `renew()`:
      - A link: `onChange(link)`, and the next ask is set for
        `expires_in_s * 1000 - RENEW_BEFORE_MS`, in place of any earlier
        timer.
      - Null: `onChange(null)`, so every quote turns back into plain text,
        and nothing is asked again.
      - A throw (a 429, a 502, no network): nothing is told, and the next
        ask is set for `RETRY_AFTER_MS`.
    - `renew()` asks now and resolves the new link's URL, or null after a
      null or a throw. It never rejects. After a null it resolves null
      without asking.
    - `stop()` clears the timer. Nothing is asked or told after it, even
      when an answer arrives late.
  - `quotePlayer.ts`:
    - `PRE_ROLL_MS = 300`, `POST_ROLL_MS = 300`.
    - `playbackWindow(quote: { start_ms: number; end_ms: number },
      trackStartMs: number): { startS: number; endS: number }`: the
      quote's place in the file in seconds, widened by the two rolls, with
      `startS` never below 0.
    - `class QuotePlayer`, built with an audio element (`{ src,
      currentTime, paused, error, play(): Promise<void>, pause(),
      addEventListener }`), a timer (`setTimeout` and `clearTimeout`),
      `renew(): Promise<string | null>` and `onChange(playing: string |
      null)`:
      - `play(key, window, url)` sets `src`, `currentTime` and calls
        `play()`, all in the same call stack, so the click that caused it
        still counts as the user's gesture in Safari, and tells
        `onChange`. It sets `src` only when `url` is not the one the player
        last set, or when the element's `error` is set. It compares with
        the URL it set, not `el.src`, which a browser returns in absolute
        form.
      - It stops by the element's own clock, not by the time since the
        click, because the sound starts late by however long the fetch
        and the seek take. `play` sets a timer for the window's length.
        When the timer fires, the player stops if the element is `paused`
        (the file ended, or the browser refused to play) or its
        `currentTime` is within 0.05 s of `endS` or past it. Otherwise it
        sets the timer again for `endS - currentTime`, at least 50 ms and
        at most the window's length.
      - On `ended` it stops.
      - On `loadedmetadata`, if `currentTime` is more than 0.25 s from
        `startS`, it sets `startS` again. This guards against a browser
        that drops a seek made before the metadata arrived, which is
        unconfirmed for Safari.
      - It always catches the promise `play()` returns. A
        `NotAllowedError` stops the play if it is still the current one.
        Any other rejection is ignored: a load that fails also fires
        `error`, and an `AbortError` is the player's own pause or new
        `src`.
      - On `error` it clears the timer and calls `renew()` once per play.
        A new URL is played from the same place. Null, or a `renew` that
        rejects, stops. The answer is used only if the play that asked is
        still the current one (not paused, stopped or replaced); otherwise
        it sets no `src` and calls no `play()`.
      - `play` on the key that is already playing pauses it.
      - `stop()` pauses and clears the timer. Every stop tells
        `onChange(null)`.
    - `playQuote(player, recording: Recording, key: string, quote: {
      start_ms: number; end_ms: number }): void` calls `player.play(key,
      playbackWindow(quote, recording.track_start_ms), recording.url)`.
  - `Report.tsx`:
    - `wantsRecording(result: Shown | null): boolean`, exported: true only
      for a graded report whose `transcript` is `recording`.
    - `quoteKey(questionIdx: number, itemIdx: number, quoteIdx: number):
      string`, exported.
    - `ReportView` stays a pure view with no state or effect. It gains the
      optional props `recording?: Recording | null`, `playing?: string |
      null`, `onPlay?(key: string, quote: { start_ms: number; end_ms:
      number }): void` (called only for a quote whose times are both set,
      since the web app compiles with `strict`) and `audioRef?:
      Ref<HTMLAudioElement>`:
      - A quote with times is a `<button>` when there is a recording. Its
        accessible name is "Play this quote", or "Pause" while it plays,
        and it has `aria-pressed`.
      - A quote without times, any quote when there is no recording, and
        every quote of a `live` report are plain text, as today.
      - When `wantsRecording(result)`, it renders one hidden `<audio
        preload="none">` with no `crossorigin` attribute, on `audioRef`. It
        serves the whole page.
      - The lede gains: "Select a quote to hear it." It is shown only when
        quotes can be played.
    - `Report`, the default export, only wires these together. When
      `wantsRecording(result)` it starts `keepRecording(() =>
      getRecording(id), window, setRecording)`, builds one `QuotePlayer` on
      the audio element with that link's `renew`, and stops both when the
      page goes. It passes `onPlay={(key, quote) => recording &&
      playQuote(player, recording, key, quote)}`, so the click calls `play`
      in its own call stack with the freshest link. This wiring has no
      unit test, like `Interview.tsx` and the default exports today: Step
      3 checks it by eye.
    - The two sentences from Global Constraints replace the current
      grading and consent copy.
  - README: how grading works now (the worker, the transcript, the quote
    check and its guards, playback), how to run the worker on a laptop
    (`uv sync --extra worker`, the model download command,
    `WHISPER_MODEL_DIR`), what is measured and what is not yet, the Supabase
    free-plan limits from My call 8, and updated test counts. "What is not
    built yet" loses its phase 3 line. The README does not describe how a
    link could be downloaded in a loop (My call 6).

- [ ] **Step 1: Write the failing tests**
  - Supabase store, with `respx`: the exact sign request; the returned URL
    is absolute and under `/storage/v1`; a reply with no `signedURL`
    raises; a 400 raises `AudioStoreError` without the key.
  - Recording route:
    - a happy path for each store, on an interview with a saved
      `recording` report
    - each 404 case: no report yet, a `live` report, a `grading_failed`
      report, and the interview once purged
    - a demo created 8 days ago whose `recording` says `uploaded` is 404,
      and a full-mode interview of the same age is 200
    - the 11th request in a minute is 429, and the first after the minute
      passes is 200
    - a store that raises gives 502 and logs no URL
    - the response has `Cache-Control: no-store`
    - a `recording` whose candidate `start_ms` is -180.0 comes back as
      `track_start_ms` -180.0 (Review Focus 4)
  - Local file route: a `Range: bytes=44-75` request gets 206 with those
    32 bytes; with a Supabase store it is 404; an id that is not a UUID is
    404.
  - Brain: `WRAP_UP_LINE` says "in a few minutes" (`test_brain.py:699`).
  - `playbackWindow`:
    - a quote at 5000 to 7000 ms on a track that starts at -180 ms plays
      from 4.88 s to 7.48 s
    - a quote that starts within the pre-roll of the file start is clamped
      to 0
  - `playQuote`, with a stub player: a quote at 5000 to 7000 ms and a
    recording whose `track_start_ms` is -180 call `play(key, { startS:
    4.88, endS: 7.48 }, recording.url)`.
  - `QuotePlayer`, with a fake element whose `currentTime`, `paused`,
    `error` and `play()` promise the test controls, and fake timers:
    - `src`, `currentTime` and `play()` are set before `play` returns
    - with the clock at `endS` when the timer fires, it pauses and reports
      null
    - sound that starts late: the clock is 0.8 s short of `endS` when the
      timer fires, nothing pauses, and the pause comes 0.8 s later
    - the element is paused when the timer fires: it reports null; `ended`
      before `endS` also stops and reports null
    - the playing key pauses on a second click, and another key switches
      to it
    - an `error` with a `NotSupportedError` rejection, in either order,
      renews once and plays from the same place
    - a second `error`, a renewal that gives null, and a renewal that
      rejects each stop and report null (Review Focus 5)
    - after a renewal, a `play()` rejected with `NotAllowedError` stops and
      reports null
    - a second click on A, a `stop()`, or a click on B while A renews: the
      late URL sets no `src` and calls no `play()`, and B keeps its own
      `currentTime`
    - after a play that ended in `error`, the next click sets `src` again
      although the URL is the same
    - at `loadedmetadata` with the clock at 0 it sets `startS` again; with
      it at `startS` it sets nothing
  - `keepRecording`, with a fake `ask` and fake timers:
    - it asks once, at once, and reports the link
    - with `expires_in_s` 1800 it asks again at 1,740,000 ms and not before
    - a timed ask that gets null reports null, and nothing is asked again
      (Review Focus 5)
    - a `renew()` that gets null reports null and resolves null
    - a `renew()` that gets a link reports it, resolves its URL, and the
      next timed ask is timed from the new link
    - an ask that throws reports nothing and is made again 60 s later, and
      a `renew()` that throws resolves null
    - after `stop()` nothing is asked, and a late answer is not reported
  - `api.ts`: `getRecording` gives null on 404 and throws on 500.
  - `Report.tsx`:
    - `wantsRecording` is true for a graded `recording` report, and false
      for a `live` report, a graded report with no `transcript`,
      `grading`, `not_yet`, `not_found`, `no_answers`, `grading_failed`
      and null
    - `ReportView` markup:
      - a `recording` report given a recording shows a quote with times as
        a `<button>` named "Play this quote" with `aria-pressed="false"`,
        and a quote with null times as plain text
      - with `recording` null there is no `<button>`
      - a `live` report shows no `<button>`, no audio element and no
        "Select a quote to hear it.", even when given a recording
      - with `playing` set to one quote's key, that button alone is named
        "Pause" with `aria-pressed="true"`
      - a `recording` report renders one `<audio preload="none">` with no
        `crossorigin`
  - Landing: the consent sentence, exactly.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.** Then check the page by eye. The dev server
  cannot make a `recording` report (it builds no transcriber, and this
  machine has no model), so seed one with a throwaway script, not
  committed:
  - Make a WAV of a few spoken sentences with `say -o speech.wav
    --file-format=WAVE --data-format=LEI16@16000 '...'`.
  - Build `create_app(...)` with a `MemoryStore` and a `LocalAudioStore`
    in a temp directory holding that WAV as `interviews/<id>/candidate.wav`.
    Save a graded interview whose `recording` is `{"status": "uploaded",
    "candidate": {"start_ms": -180.0, ...}}` and whose report says
    `"transcript": "recording"`, with a few quotes timed to the speech.
  - Serve it on the dev server's port and open `#/report/<id>` through
    Vite. Play, pause, switch quotes, and seek near the start and the end
    of the track. Do it once more with the network throttled in the
    browser's dev tools, to hear a first click play to the end of its
    quote.
- [ ] **Step 4: Run them.**
  - `uv run pytest -q`
  - `uv run ruff check .`
  - `cd web && npm test && npx tsc --noEmit && npm run lint && npm run build`

  Expected: PASS.
- [ ] **Step 5: Commit**

  `git commit -m "Play the recording from a quote on the report page"`

---

## After phase 3

1. Run a live interview with the worker running locally, then on ECS after
   the deploy. Check:
   - The report says `"transcript": "recording"`. If it says `live`, the
     worker did not finish: its log, or a RunTask that IAM denied, says
     why.
   - Timing: the worker's log gives cold start, transcription and grading
     seconds. Tune the model, the beam size and the task size from them.
   - Bleed: with speakers and no headphones, see whether the interviewer's
     words show up in the candidate's transcript. If they do, add a guard
     that drops a run of words matching the reply that was playing.
   - Playback: on the deployed site, in Chrome and in Safari, the first
     click on a quote starts on its first word and ends after its last.
   - The share of quotes that needed the fuzzy path, and the share
     dropped. The worker logs both counts.
2. Watch the Supabase usage page for storage and egress.
3. Phase 4: deploy on merge, and a remote Terraform state.
4. Phase 5: the benchmark harness and the grader eval. The eval's hand
   corrected transcripts are also the test of whether `small.en` is
   accurate enough (the spec's Unknown 7, My call 9).
