# Mockingbird

[![ci](https://github.com/ruhan-sahasi/mockingbird/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/ruhan-sahasi/mockingbird/actions/workflows/ci.yml)

A mock interviewer you talk to.

It runs a behavioral interview out loud in your browser, you can cut it off mid-sentence the way
you would a person, and when the call ends you get a graded report where every score points to
words you actually said.

What makes it interesting: Vapi sends several brain requests per answer and speaks only the
reply to the last one, so the brain commits only the draft Vapi actually speaks, and every quote
in the report is checked against the transcript before it is shown. More in
[The interesting engineering](#the-interesting-engineering).

It runs locally today. The AWS deployment is defined in Terraform (`infra/`) but has not been
applied, so there is no public link yet.

## How it works

```mermaid
flowchart LR
    browser["Browser<br/>React + Vite"]
    subgraph server["Python server (FastAPI, one process)"]
        relay["Audio relay"]
        brain["Brain endpoint<br/>interview plan in code"]
        report["Grading and quote checks"]
    end
    vapi["Vapi<br/>speech-to-text, turn-taking,<br/>text-to-speech"]
    haiku["Claude Haiku 5.5"]
    opus["Claude Opus 5.5"]

    browser <-->|"WebSocket: 16 kHz PCM audio,<br/>flush and ended messages"| relay
    relay <-->|"WebSocket: audio,<br/>live events as text frames"| vapi
    vapi <-->|"POST /chat/completions,<br/>several per answer"| brain
    brain <-->|"streamed line for each reply"| haiku
    brain -.->|"after the call, a background task<br/>grades the stored turns"| report
    report <-->|"structured grade"| opus
    browser -.->|"polls GET /api/interviews/{id}/report"| report
```

- **Browser.** An AudioWorklet resamples the mic to 16 kHz, 16-bit mono and sends one 640-byte
  frame every 20 ms over a single WebSocket, silence included. Interviewer audio comes back on
  the same socket and plays through a second worklet with a 2 s ring buffer, which a flush
  empties at once (`web/src/audio/`).
- **Relay.** When the browser socket opens, the server creates a Vapi call on the
  `vapi.websocket` transport and passes audio both ways unchanged
  (`server/mockingbird/routes_ws.py`, `relay.py`). API keys and the Vapi call id never reach
  the browser.
- **Vapi.** It transcribes the candidate, decides when they have finished, and speaks the
  replies. Its model is set to the brain endpoint on this server, an OpenAI-style streaming
  `/chat/completions` route, so Vapi never calls Claude itself (`vapi_client.py`,
  `routes_vapi.py`).
- **Brain.** The interview plan in code picks the next move, and Claude Haiku 5.5 writes that
  one line, streamed, with thinking off and a cached system prompt (`plan.py`, `brain.py`,
  `claude.py`). Anyone gets the demo: 5 minutes, one main question, up to two follow-ups. The
  owner key starts a full 20 minute interview with three questions.
- **Report.** When the call ends, the turns are saved, and a background task in the same process
  asks Claude Opus 5.5 to score each main question from 1 to 4 on six rubric items (situation,
  task, action, result, specificity, ownership), citing quotes. The quotes are checked, and the
  report page asks for the result every 2 s (`report.py`, `server/data/rubric.yaml`).
- **Storage.** Postgres (Supabase) when `DATABASE_URL` is set, otherwise in memory (`store.py`,
  `server/migrations/`). Both sides of every call are recorded as 16 kHz WAV files and kept in a
  private Supabase Storage bucket, or a local folder when Supabase is not set up, and a demo
  recording is deleted with its interview after 7 days (`recording.py`, `audio_store.py`).

## The interesting engineering

- **A request is not a turn.** Vapi sends the brain several requests per answer as its
  transcript grows, and speaks only the reply to the last one: four requests for one answer in
  the recording. This was measured against real Vapi with `scripts/spike_vapi.py`, written up in
  `docs/vapi-contract.md`, and saved as test fixtures in `server/tests/fixtures/vapi/`. So each
  request drafts its reply on a copy of the interview plan, a new request cancels the draft
  before it, and a draft becomes real only when Vapi reports that the interviewer started
  speaking it. If the candidate cut the reply off, the history keeps only the words Vapi says
  were spoken (`brain.py`; see
  `test_four_drafts_for_one_answer_then_one_commit_advance_the_plan_once` in `test_brain.py`).
- **Interruptions are flushed in order with the audio.** Vapi puts its live events
  (`speech-update`, `user-interrupted`, `hangup`) on the audio WebSocket as text frames, in
  order with the audio. The relay reads them there instead of from webhooks, so when the
  candidate talks over the interviewer, the relay sends the browser `{"type": "flush"}` before
  any later audio frame, and the browser drops the reply it was still playing. Webhooks carry
  only the end-of-call report (`relay.py`; see
  `test_user_interrupted_frame_flushes_before_later_audio` in `test_relay.py`).
- **Code owns the interview, and Claude only words it.** `plan.py` is a pure state machine with
  no I/O and no clock of its own. It sets the question order, allows at most two follow-ups per
  main question, and wraps up once the time left reaches a reserve (40 s in the demo). Claude
  gets each move as a system message placed after what the candidate said. The wrap-up line is
  fixed text that never goes to Claude, so the model cannot run past time. If Claude errors,
  times out or refuses, a fixed line for that move is spoken and the interview goes on.
  `test_plan.py` checks these rules over 500 seeded random interviews for each mode and pace.
- **Every quote is checked before it is shown.** The grader has to cite the words of the
  candidate for every score. `verify_quotes` lowercases the quote and the answer, removes all
  punctuation (curly quotes and dashes included) and collapses whitespace. It then keeps the
  quote only if what is left appears, as one unbroken run of text, in what the candidate said
  under that same main question, follow-up answers included. It is a character match, not a
  whole-word match, so a quote can start or end inside a word. A quote with no words in it never
  counts. Dropped quotes stay in the saved report as `dropped_quotes`, and an item with no quote
  left shows "not enough evidence" instead of a score (`report.py`, `test_report.py`).
- **Cost and abuse guards.** Each visitor gets one demo per UTC day, counted by an anonymous
  cookie and by IP address, both stored only as salted SHA-256 hashes, with at most three start
  attempts a day. All demos share a daily budget (`DAILY_DEMO_MINUTES`, 60 by default), and a
  start must come from a page on the site itself. The relay ends every call at its cap, with
  the Vapi limit 30 s later as a backstop. It also ends the call when there is no frame from
  the browser for 2 s, or a send to the browser stalled for 2 s, so a closed tab or a dropped
  network stops the bill in about 2 s. Demo
  interviews are deleted after 7 days, along with the copy Vapi keeps of each call, and Vapi
  recording and end-of-call analysis are turned off (`limits.py`, `relay.py`, `sessions.py`,
  `vapi_client.py`).

## Latency

The number that matters is how long the candidate waits: from the moment they stop talking to
the moment they hear the interviewer. The browser measures it at both ends, and the server
breaks it into hops. When a call ends, each interviewer reply that became a turn is saved as one
row of `latency_traces` (`server/migrations/003_latency_traces.sql`, with more stamps added in
`004_latency_traces_more_stamps.sql`), and `/api/stats` sums up the rows.

**Measured in the browser.** Both ends of each number are stamped on the page clock,
`performance.now()`, so no other clock has to agree with it (`web/src/timing.ts`).

- **Stop to audible**, the headline number. The end of speech comes from an energy threshold on
  the mic in the capture worklet: speech is an RMS of 0.02 or more on the -1 to 1 scale, and it
  ends after 300 ms below that (`web/src/audio/vad.ts`). The stop is backdated by those 300 ms.
  The other end is the first interviewer audio loud enough to count as speech in the playback
  worklet, plus the output latency the AudioContext reports. So it includes everything the
  candidate waits through: Vapi deciding the answer is over, the brain, Claude, text to speech,
  the network, and the 0.1 s of audio the player keeps queued. Only the end of an answer is
  timed. A stop while the interviewer is talking, or one followed by more speech, is not.
- **Interrupt to silence**: from the candidate starting to talk over the interviewer to the
  interviewer going quiet, after the server tells the page to flush the reply. The start is
  backdated by the three loud frames the threshold needs, and the output latency is added. Talk
  over the reply that ends with no flush, such as an "mm-hmm" Vapi lets pass, is not a start.

The page sends each number on the call socket as a `timing` message. The server ties it to the
reply Vapi is speaking and keeps only numbers from 0 to 60000 ms, the first of each kind per
reply (`server/mockingbird/tracing.py`).

**Measured on the server**, on its monotonic clock (`server/mockingbird/tracing.py`):

- last candidate speech to request: the relay passes the candidate's last frame of speech, by the
  same 0.02 threshold the browser uses, and then Vapi asks the brain for a reply. This is Vapi
  deciding the answer is over plus its speech to text, usually the biggest part of the wait, and
  the hop that Vapi's wait-before-reply setting tunes. A reply with no candidate speech since the
  line before it is left out.
- request to first token: Vapi asks the brain for a reply, and Claude streams its first text.
  The wrap-up line never goes to Claude, and neither does a fallback line said when Claude wrote
  nothing, so a reply that starts with one is left out of this hop and the next.
- first token to first chunk: that text goes out to Vapi.
- first chunk to assistant started: Vapi says it started speaking the reply.
- assistant started to first audio: the first loud interviewer frame passes through the relay.
- **Cut-in rate**: the share of spoken replies the candidate talked over within 1.5 s of their
  start. It stands in for the interviewer starting before the candidate had finished.

A turn missing a stamp, such as a reply the page sent no timing for, is left out of that number
and never counts as 0 ms. The two browser numbers say how many turns they are over
(`server/mockingbird/stats.py`).

**Reading it.** With the API running:

```bash
curl http://localhost:8000/api/stats             # the last 7 days
curl "http://localhost:8000/api/stats?days=1"    # any number of days from 1 to 30
```

It gives the p50, p95 and count of both browser numbers, the median of each server hop, the
cut-in rate, and `consider_ducking`. The page at http://localhost:5173/#/stats shows the same
numbers for the last 7 days. They are about all the calls together and name no interview, so
the endpoint and the page are public.

**Known biases and gaps.**

- Neither browser number includes the mic input latency, usually 10 to 20 ms, so both read that
  much short.
- An energy threshold can hear a stop early or late in a noisy room, which moves stop to
  audible with it.
- The server puts a browser number on the reply Vapi is speaking when the number arrives. Tests
  check this against a fake Vapi only, so the first live call has to confirm it.

**Where it stands.** No live call has been measured this way yet, so there are no numbers here.
The design goal is a stop to audible p50 under 800 ms. The one reading so far, about 2.1 s from
the smoke call (below), came from a script, not a browser. Real numbers come from interviews run
in a browser and read from `/api/stats`. Browser ducking, which would turn the interviewer down
the moment the candidate starts talking, is not built: `consider_ducking` turns true, and the
page says so, once interrupt to silence p95 is above 300 ms.

## Run it locally

You need:

- macOS or Linux with bash. Without `--wav`, the smoke call makes its spoken answers with the
  macOS `say` and `afconvert` tools.
- [uv](https://docs.astral.sh/uv/), which sets up Python 3.12 from `.python-version`.
  `scripts/dev.sh` needs `uv` on your PATH. A default uv install puts it in `~/.local/bin`, so
  if your shell cannot find it, run `export PATH="$HOME/.local/bin:$PATH"`.
- Node 24 and npm.
- [ngrok](https://ngrok.com/), signed in with your authtoken: run
  `ngrok config add-authtoken <token>` once (a free account works). Vapi has to reach the brain
  endpoint from the internet.
- A Vapi private API key and an Anthropic API key. Every call bills both: the recorded Vapi
  test call came to about $0.075 a minute (`docs/vapi-contract.md`), plus Claude usage.
- Headphones, so the interviewer voice does not leak into your mic and count as you talking
  over it.

Set up:

```bash
git clone https://github.com/ruhan-sahasi/mockingbird.git
cd mockingbird
uv sync
npm --prefix web ci
cp .env.example .env
```

Fill in `.env`:

| Setting | What to put there |
|---|---|
| `ANTHROPIC_API_KEY` | your Anthropic API key |
| `VAPI_API_KEY` | your Vapi private key |
| `BRAIN_SECRET`, `WEBHOOK_SECRET` | random strings: the secret part of the two URLs Vapi calls |
| `OWNER_KEY` | a random string: it starts a full 20 minute interview and skips the demo limits |
| `VISITOR_SALT` | a random string that salts the visitor hashes |
| `PUBLIC_BASE_URL` | leave it empty: `dev.sh` sets it to the ngrok address |
| `DATABASE_URL` | optional. Empty keeps interviews in memory until the API stops. For Postgres (a Supabase session-pooler string works), apply the schema once with `uv run --env-file .env python -m mockingbird.migrate` |

Make each random string with `openssl rand -hex 32`. The other settings have defaults.

Start everything:

```bash
./scripts/dev.sh
```

It checks `.env`, starts an ngrok tunnel, the API on port 8000 and the web app on port 5173,
and stops all three on Ctrl-C. Open http://localhost:5173 in Chrome. The page starts the
5 minute demo, so the demo limits apply: one per visitor per day. With the in-memory store,
restarting `dev.sh` resets them.

### The smoke call

```bash
./scripts/dev.sh --no-web               # terminal 1: tunnel and API only
uv run python scripts/smoke_call.py     # terminal 2
```

The smoke call acts like the browser. It starts a full interview with the owner key, streams
two spoken answers in real time (or pass `--wav answer.wav`, a 16 kHz mono 16-bit file, to use
it for both), talks over the reply to the first answer, hangs up, and waits up to 120 s for the
report. It prints ok or FAIL for each step and exits 0 only if they all pass. It makes a real
call, so it costs money.

After the report it reads `/api/stats?days=1` and prints the stop to audible p50 and how many
turns it is over, the cut-in rate, and the median of each server hop. These are for reading
only and never fail the run. The script stands in for the browser over a raw socket with no
speakers, so it sends no timing: its call adds server hops and cut-ins, but nothing to the two
browser numbers.

**Last live run.** These numbers come from the scripted smoke call run on 2026-10-21 against
real Vapi and Claude, and the description of
[PR #18](https://github.com/ruhan-sahasi/mockingbird/pull/18) records its output. It passed:
the opening line was heard, the reply to the first answer started about 2.1 s after the answer
ended (a rough reading), a flush arrived after the interruption, the call ended cleanly, and the
report came back graded with 6 scored items and 13 verified quotes.

**Latency.** The design sets a goal of a median under 800 ms from the moment the candidate stops
talking to the moment the interviewer audio starts, measured in the browser. That goal has not
been met, and it has not been measured properly yet. The 2.1 s above is one reading taken by the
smoke call script, not the browser, and it includes the time Vapi waits to decide the answer is
over. The browser measurement and the server hops are built now (see [Latency](#latency)), but
no live call has been measured with them yet.

## Tests

Server:

```bash
uv run ruff check .
uv run pytest
```

Web app:

```bash
cd web
npm test
npx tsc --noEmit
npm run lint
```

Terraform (from the repo root; `init` downloads the AWS provider and needs no AWS credentials):

```bash
terraform -chdir=infra init -backend=false
terraform -chdir=infra validate
```

Counts from a run on this branch on 2026-11-11: `uv run pytest -q` printed
`786 passed, 66 skipped`, and `npm test` printed `280 passed (280)` across 19 test files. The
66 skipped tests are the PostgresStore tests in `server/tests/test_store.py`, which run only when
`TEST_DATABASE_URL` points at a Postgres database (a direct connection, for example
`postgresql://postgres:postgres@localhost:5432/postgres`).

CI (`.github/workflows/ci.yml`) runs on every pull request and every push to main, in four
jobs:

- **python:** `uv sync --locked`, `ruff check` and `pytest`, against a Postgres 16 service so
  the PostgresStore tests run too.
- **web:** Node 24, `npm ci`, `npm test`, `npx tsc --noEmit`, `npm run lint` (oxlint) and
  `npm run build`.
- **terraform:** `terraform fmt -check`, `terraform init -backend=false` and
  `terraform validate`.
- **docker:** `shellcheck` on `scripts/*.sh`, then it builds the ARM64 server image under QEMU,
  boots it with dummy settings, checks `/healthz`, and checks that the app runs as an
  unprivileged user.

A separate workflow, `keepalive.yml`, asks the deployed server for `/healthz?db=1` once a day
so the free Supabase project is not paused. It is skipped until the `MB_URL` repository
variable is set.

## Deploying

`infra/` is Terraform for one AWS stack. CloudFront serves the web app from S3 and passes
`/api/*`, `/ws/*`, `/vapi/*` and `/healthz` to an Application Load Balancer, which takes
traffic only from CloudFront and sends it to one Fargate task (ARM64). The image lives in ECR,
the secrets are SSM SecureString parameters, and an AWS Budgets alert sends an email at 80% of
a monthly limit. There is no NAT gateway and no custom domain. It runs exactly one task,
because live interviews are held in the memory of that task.

Deploying needs Terraform, Docker with buildx, npm, and the AWS CLI signed in to the account
that will hold the stack.

```bash
cp infra/terraform.tfvars.example infra/terraform.tfvars   # then set alert_email and supabase_url
terraform -chdir=infra init
terraform -chdir=infra apply
# set each secret with aws ssm put-parameter, as infra/secrets.tf shows
# (SUPABASE_SERVICE_KEY is one of them)
./scripts/deploy.sh
```

`scripts/deploy.sh` pushes the server image to ECR, restarts the ECS service, builds the web
app and publishes it to S3 behind CloudFront. The container refuses to start while any secret
still holds the Terraform placeholder. None of this has been applied yet, so nothing is
deployed.

## Design and decisions

- [Design spec](docs/superpowers/specs/2026-10-19-mockingbird-design.md): goals, architecture,
  and a decisions table that records every architecture choice with the options that were
  rejected, for example Vapi over a hand-built voice pipeline or Pipecat, and a script in code
  over one big prompt.
- [Phase 1 plan](docs/superpowers/plans/2026-10-19-phase-1-thin-slice.md): the thin slice,
  task by task. Its Amendment 1 records how measuring Vapi changed the plan.
- [Vapi contract](docs/vapi-contract.md): what Vapi really sends and expects, measured on live
  calls.

The spec describes the whole design, and phase 1 builds a thin slice of it. Where the two
differ, the code is current: for example, phase 1 grades the text Vapi transcribed during the
call and matches quotes exactly after normalizing, and grading runs in the server process
instead of a separate worker.

## What is not built yet

- Browser ducking: turning the interviewer audio down the moment the candidate starts talking,
  before the flush from the server arrives. It is left out on purpose until `/api/stats` shows
  interrupt to silence p95 above 300 ms.
- Word-timestamped transcripts from batch speech-to-text, and quotes you can click to play
  (phase 3).
- Deploy on merge from GitHub Actions (phase 4). Deploying is `scripts/deploy.sh` by hand for
  now.
- The benchmark harness that runs scripted answers against real Vapi, and the grader eval
  against hand-graded answers (phase 5).
