# Mockingbird: design

Date: 2026-10-19
Status: draft, waiting for Ruhan's approval

## What it is

Mockingbird is a mock interviewer you talk to. You open a link, it runs a
behavioral interview out loud, you can cut it off mid-sentence like a real
person, and when it ends you get a graded report. Every score is backed by a
quote of what you actually said, and clicking the quote plays that moment of
the recording.

## Why

- Portfolio project for Summer 2027 SWE internships at big tech companies and
  startups. It has to be easy to explain in two minutes and hold up to
  thirty minutes of follow-up questions.
- Ruhan wants to learn from it. Every architecture decision below was made
  by Ruhan after seeing options and tradeoffs, and the "Decisions" section
  records why, so the reasoning is easy to retell in an interview.
- Hook: "I practiced for this interview with it."

Talking points the build must support with real code and real numbers:

1. A realtime WebSocket relay (backpressure, reconnects, two-way audio).
2. A latency budget, measured from the user's ears, broken down per hop.
3. Interruptions, including syncing two channels (audio socket and webhooks).
4. An interview brain where code owns the plan and Claude writes the words.
5. Grading where every quote is checked against the transcript, plus an eval
   that says how often the grader agrees with a human.
6. Production deployment on AWS with Terraform and CI/CD.

## Goals (v1)

- Run a spoken behavioral interview in the browser: intro, questions,
  follow-ups, wrap-up.
- Let the candidate interrupt the interviewer at any time.
- Median time from "candidate stops talking" to "interviewer audio starts
  playing" under 800 ms, measured in the browser, with p50 and p95 reported
  and a per-hop breakdown. This is a goal, not a hard requirement (Ruhan's
  decision): it is tuned together with the early cut-in rate (see Latency)
  and the README reports both numbers as measured.
- Graded report after every interview, with verified quotes and click-to-play.
- Grader eval on about 20 hand-graded sample answers, with the agreement
  number in the README and a command to reproduce it.
- Live public demo: anyone with the link gets one 5-minute interview.
- Deployed on AWS (ECS Fargate behind a load balancer), defined in Terraform,
  shipped by GitHub Actions.

## Non-goals (v1)

- System design interviews. They need a shared whiteboard; planned for v2.
- A background planner model (planned v2 upgrade on top of the script).
- Accounts, sign-in, or interview history across visits.
- Phone calls, mobile apps, languages other than English.

## Anti-goals (failure even if it technically works)

- An interviewer that drifts: asks two questions at once, ignores the clock,
  gives feedback mid-interview.
- A report that quotes something the candidate never said.
- Latency numbers measured in a flattering place (server-side only) or that
  nobody else can reproduce.
- A public demo that can run up an unbounded bill.
- README or UI copy that reads like filler. Plain, specific language; every
  claim in the README backed by something a reader can run.

## Constraints

- Backend: Python. Frontend: React + Vite. Live voice: Vapi. LLM: Claude.
- Data: Supabase only (Postgres for data, Supabase Storage for audio). No
  Cloudflare services.
- Hosting: AWS ECS Fargate + Application Load Balancer, Terraform, GitHub
  Actions.
- API keys never reach the browser.
- Repo lives at `~/dev/mockingbird` (the Desktop is iCloud-synced and breaks
  git).

## Architecture

```
 Browser (React + Vite)
   mic -> 16 kHz PCM frames ----WebSocket---->  Your server (Python)
   speaker <- audio frames <------------------    |  audio relay
   latency timer                                  |  interview brain  <--> Claude Haiku 5.5
                                                  |  event listener
                                                  |
                         raw audio over WebSocket |  ^ "give me the next reply"
                                                  v  | (custom-LLM call)
                                        Vapi (hosted): speech-to-text,
                                        turn-taking, interruption detection,
                                        text-to-speech
                                                  |
                                                  +-- webhooks --> event listener

 After the call: recorded audio -> batch speech-to-text (word timestamps)
                 -> grader (Claude Opus 5.5) -> quote verifier -> report page
```

### Components

**Browser (React + Vite).**
Captures the mic with an AudioWorklet and sends 16 kHz, 16-bit mono PCM
frames (about 20 ms each) to the server over one WebSocket. Plays interviewer
audio through a second AudioWorklet with a small jitter buffer that can be
flushed instantly. Runs a local voice-activity detector so it can timestamp
when the candidate stopped talking (see Latency). Shows a consent notice
before the mic turns on. After the call, shows the report page.

**Server (Python, one process, asyncio).** Four parts:

1. *Audio relay.* For each interview it creates a Vapi call using Vapi's
   WebSocket transport (`vapi.websocket`, `pcm_s16le`, 16 kHz) and bridges
   frames both ways between the browser socket and the Vapi socket. It
   records both audio tracks, stamped with the server's monotonic clock, and
   enforces the time cap by ending the call itself.
2. *Interview brain.* An OpenAI-compatible streaming `/chat/completions`
   endpoint that Vapi calls on every turn (Vapi's custom-LLM mode). It runs
   the interview plan and streams Claude's reply back to Vapi.
3. *Event listener.* Receives Vapi webhooks (speech updates,
   `user-interrupted`, conversation updates, status, end-of-call report),
   matches them to the live interview, and pushes interruption signals to
   the browser socket so playback flushes.
4. *Post-call pipeline.* Uploads audio to Supabase Storage, runs batch
   speech-to-text with word timestamps, runs the grader and the quote
   verifier, and writes the report.

**Vapi.** Transcribes live speech, decides when the candidate's turn is over,
detects interruptions, and speaks the brain's replies. Its model setting
points at the brain endpoint, so Vapi never calls Claude directly.

**Claude.**
- Live replies: `claude-haiku-5-5`, streaming, thinking off, a stable system
  prompt with prompt caching. Per-turn instructions go in as a
  mid-conversation system message so the cached prefix stays intact.
- Grading: `claude-opus-5-5` with structured output.

**Supabase.** Postgres for interviews, turns, words, latency traces, scores
and quotes. A Storage bucket for audio.

**AWS.** ECR for images; one ECS Fargate service behind an Application Load
Balancer; CloudFront in front of both the static site and the API; secrets
in SSM Parameter Store; logs in CloudWatch. Everything in Terraform under
`infra/`.

### One turn, step by step

1. The candidate talks. Audio frames flow browser -> relay -> Vapi. The relay
   writes them to the candidate track.
2. Vapi transcribes as the candidate speaks and decides the turn is over.
3. Vapi calls the brain endpoint with the conversation so far.
4. The brain advances the interview plan, picks the next move, and streams
   Haiku's reply back to Vapi.
5. Vapi turns the reply into speech and streams audio to the relay, which
   records it and forwards it to the browser.
6. The browser plays it. The latency timer logs stop-talking to first sound.

### Latency: definition, budget, measurement

**The metric.** For each interviewer turn: the time from the candidate's
speech ending (browser VAD, on the AudioContext clock) to the first non-silent
interviewer sample actually played (same clock). Measuring both ends in one
place, at the user's ears, means the number includes every hop and can't be
flattered.

**Per-hop trace.** Every turn also logs server-side timestamps: relay
receives the last candidate frame, brain request arrives, Claude's first
token, first interviewer audio frame reaches the relay, frame sent to the
browser. Stored in `latency_traces`, so the README can show where time goes.

**Target.** p50 under 800 ms; p95 reported alongside it.

**The counterweight: early cut-in rate.** A shorter wait before replying
lowers latency but makes the interviewer jump in while the candidate is only
pausing to think. So every tuning change is judged on both numbers.
- In the benchmark, the scripted answers contain thinking pauses at known
  positions. A cut-in is the interviewer starting to speak during one of
  those pauses. This gives an exact rate.
- In live sessions, a proxy is used: the candidate keeps talking over the
  interviewer within the first 1.5 seconds of its turn.
- The README reports latency and cut-in rate side by side, as measured.

**Benchmark harness.** A scripted candidate: a headless client that connects
to the server like a browser, streams recorded answer audio, and measures
the same metric. It runs against real Vapi (costs money, triggered by hand)
so the README numbers can be reproduced.

**Levers to tune (each measured, not assumed).** Vapi's wait-before-reply and
interruption settings, Haiku with thinking off, prompt caching, a short first
sentence, server region, frame size, and jitter buffer size.

### Interruptions

- Measured in the Task 1 recording (`docs/vapi-contract.md`): Vapi sends
  several brain requests per answer and speaks only the last reply; its
  history after an interruption holds only the spoken words; and live events
  (`speech-update`, `user-interrupted`, `hangup`) arrive as text frames on the
  audio WebSocket, in order with the audio. Ruhan's decisions: a reply counts
  only once Vapi starts speaking it, and live events are read from the audio
  socket, so the relay flushes the browser the moment the interruption frame
  arrives. Webhooks carry only the end-of-call report. This replaces the
  webhook-driven design below where they differ.
- Vapi detects the candidate talking over the interviewer, stops speaking, and
  sends a `user-interrupted` webhook.
- Problem: interviewer audio already buffered in the browser keeps playing,
  and the webhook arrives on a different channel than the audio.
- Design (my call, see below): two layers. The browser ducks playback right
  away when its own VAD hears the candidate while the interviewer is
  speaking. The server's interruption signal, triggered by the webhook, is
  the authority that flushes the buffer and marks the turn interrupted.
  v1 asks candidates to use headphones so the interviewer's voice doesn't
  trigger the browser VAD through the speakers.
- The interviewer's turn is stored as what the candidate actually heard,
  not what was generated.
- The brain keeps its own conversation history instead of trusting the
  history Vapi sends (my call; it makes Unknown 2 non-blocking). For an
  interrupted turn, the heard text is estimated from how much of that turn's
  audio the browser actually played, cut at a word boundary, and corrected
  after the call by the batch transcript of the interviewer track.
- What the plan does after an interruption (my call): if the candidate's
  interruption is a real answer (at least a set number of words), the
  interrupted question counts as asked and the plan moves on as usual. If
  it is a short aside ("sorry, go ahead"), the brain re-asks the question
  in one short sentence. The word threshold is set by tests.

### Interview brain

- Code owns a plan: `INTRO -> QUESTION -> FOLLOW_UP (at most 2) -> next
  QUESTION ... -> WRAP_UP -> END`.
- Two modes from one config. Demo: 5 minutes, one main question plus
  follow-ups, then a short wrap-up. Full (owner only): about 20 minutes,
  three questions.
- A curated question bank in a data file, tagged by competency.
- Code picks the move from the time left, follow-ups used, and simple
  signals about the last answer. Claude writes the sentence for that move,
  and for follow-ups is told to probe whichever part of the answer
  (situation, task, action, result) was thin.
- Hard rules in the prompt and enforced by tests: one question at a time, at
  most two sentences, plain speech with no markdown, no feedback until the
  report.
- The plan is pure logic with no I/O, so it is unit-tested directly
  (for example "after two follow-ups it always moves on", "at the time limit
  it always wraps up").

### Transcripts and grading

- **Transcript source.** The relay's own recordings. After the call, batch
  speech-to-text produces word-level timestamps on the relay's clock. Vapi's
  transcript is not used for grading.
- **Rubric v1.** Situation, Task, Action, Result, Specificity, Ownership,
  each scored 1 to 4 with written descriptors in a rubric file.
- **What gets scored (my call).** Each main question's answer, together with
  its follow-ups, is scored on its own. Full mode adds a short overall
  summary across questions. This keeps the live grading task the same as
  the eval task.
- **Grader.** Opus 5.5 returns structured output: for each item, a score, a
  short rationale, and verbatim quotes.
- **Quote verifier.** Fuzzy-matches each quote against the candidate's words
  (normalized text with a tolerance threshold, because batch transcription
  and the model's quoting won't agree word for word). A matched quote gets
  start and end timestamps. An unmatched quote is dropped and logged. An
  item left with no verified quote shows "not enough evidence" instead of a
  score.
- **Report page.** Scores, rationales, and quotes. Clicking a quote plays the
  recording from that timestamp.
- **Grader eval.** About 20 sample answers covering strong and weak ones,
  each one main question plus its follow-ups. Metrics: exact agreement,
  agreement within 1 point, and the share of quotes that verify. A single
  command reruns it.
- **Where the eval set comes from (my call).** Ruhan records the answers
  through the live app, acting out strong and weak versions, then
  hand-grades them on the rubric and hand-corrects their transcripts. The
  corrected transcripts double as the reference for choosing the batch
  speech-to-text vendor (Unknown 7), so the eval set is recorded before the
  post-call pipeline is finalized. Audio lives in a private Supabase bucket,
  not in git. Labels and reference transcripts live in `eval/`.

### Data model (sketch; the builder may refine)

- `interviews`: id, created_at, mode, status, vapi_call_id, ended_reason,
  duration_s, visitor_hash
- `turns`: interview_id, idx, speaker, start_ms, end_ms, text, interrupted
- `words`: interview_id, speaker, word, start_ms, end_ms
- `latency_traces`: interview_id, turn_idx, plus one column per timestamp
  from the per-hop trace, plus the browser-measured total
- `scores`: interview_id, question_idx, item, score, rationale, status
- `quotes`: score_id, text, start_ms, end_ms, match_ratio, verified
- Storage: `interviews/{id}/candidate.wav`, `interviews/{id}/interviewer.wav`

### Cost: no out-of-pocket spending

Ruhan's constraint: the project must cost nothing out of pocket. It runs on
existing Vapi, Claude, and AWS credits, plus Supabase's free tier.

- Batch speech-to-text runs faster-whisper inside the project's own AWS
  account, so AWS credits cover it. It runs as a one-off ECS task per
  finished interview (ECS RunTask), not an always-on worker, so it only uses
  credits when there is work.
- Supabase's free tier pauses projects that sit idle, so a scheduled ping
  (for example a GitHub Actions cron job) keeps it awake.
- Hard caps everywhere: an AWS Budgets alarm on credit burn, a spend limit
  in the Claude console, and a daily demo-minute cap sized from the
  remaining Vapi credit. Every cap is a config value, not a code change.
- The always-on load balancer and one small Fargate task are the fixed
  monthly draw on AWS credits. The plan records the credit balances and
  expiry dates and sizes the caps from them.

### Demo access, cost, and privacy

- Anyone with the link can start one 5-minute demo interview. The relay ends
  the call at the cap, and Vapi's own max-duration setting is a backup.
- Per-visitor limit (anonymous cookie plus IP rate limit) and a global daily
  minute budget. When the budget runs out, the page says the demo is resting
  until tomorrow.
- Full 20-minute mode sits behind an owner key.
- Consent notice before the mic turns on: the interview is recorded and
  graded. Demo audio and transcripts are deleted automatically after 7 days
  (my call).

### Deployment

- Terraform defines the VPC and subnets, ALB, target group, ECR, ECS
  cluster, service and task definition, IAM roles, secrets, logs, the S3
  bucket for the frontend, and the CloudFront distribution.
- The ALB's idle timeout is raised above the default 60 s, and the app sends
  a heartbeat ping on both sockets, so a long pause doesn't drop a call.
- GitHub Actions: tests on every PR. On merge to main: build the image, push
  it to ECR, deploy to ECS; build the frontend and publish it. AWS access
  through GitHub OIDC, with no long-lived keys.
- Region chosen by measuring round trips to Vapi and Anthropic from the
  candidate regions.
- Local development: run the server locally and expose it through a tunnel
  (ngrok, not Cloudflare) so Vapi can reach the brain and webhook endpoints.
- Networking (my call): Fargate tasks in public subnets with public IPs and
  locked-down security groups, no NAT gateway, to avoid its fixed monthly
  cost.
- Post-call work (batch transcription, grading) runs in a separate worker,
  never in the process carrying live audio, so a heavy job can't add
  latency to someone else's call.
- No custom domain in v1 (my call, from the no-spending rule: credits
  generally don't cover buying a domain). CloudFront's free
  `*.cloudfront.net` HTTPS address serves the site and proxies `/api/*`,
  `/ws/*`, `/vapi/*`, and `/healthz` to the load balancer, so there is no
  certificate to manage. The load balancer listens on HTTP and accepts
  traffic only from CloudFront's origin-facing IP ranges. A custom domain
  can be added later without changing the app.
- One server task (my call): the live-session registry is in memory, so the
  ECS service runs exactly one task. Running more would need sticky routing
  or a shared registry, which is out of scope for v1.

### Browser support (my call)

Desktop Chrome, Edge, Firefox, and Safari. Phones get a short "open this on
a laptop" screen instead of the interview, which avoids iOS audio quirks in
v1.

### Testing

- Unit tests: interview plan, quote verifier, latency math, audio framing.
- Integration tests against a fake Vapi: a local WebSocket server plus fake
  webhooks, so relay and interruption flows are tested without spending
  money.
- Benchmark harness against real Vapi (manual trigger).
- Grader eval script.

## How it gets built (Ruhan's decisions)

- **Who writes the code.** The agent writes the code in small PRs. Each PR
  comes with a plain-English walkthrough of what changed and why, and Ruhan
  approves every merge. The parts interviewers will dig into (the relay,
  interruption sync, latency measurement) get extra explanation so Ruhan
  can explain them without notes.
- **Order: thin slice first.**
  1. A rough version working end to end and deployed: talk, get a reply,
     interrupt, get a basic report.
  2. Interruptions and latency tracing done properly.
  3. Real grading with quote checks.
  4. Terraform hardening and CI/CD.
  5. The benchmark harness and the grader eval.

  If time runs short, step 5 is cut first. It still has to exist before
  the README states any numbers.

## Decisions (made by Ruhan, with the alternatives considered)

| Decision | Chosen | Considered |
|---|---|---|
| Live voice | Vapi | Build the pipeline by hand; Pipecat or LiveKit; OpenAI Realtime; Gemini Live |
| Server's place | Audio relay plus own brain endpoint | Vapi web SDK direct; relay with Vapi calling Claude itself |
| v1 scope | Behavioral only | System design first; both at once |
| Brain | Script in code, Claude writes the words | One big prompt; script plus background planner |
| Live model | Claude Haiku 5.5 | Sonnet 5.5; Opus 5.5 |
| Transcript | Record in relay, batch transcribe after | Vapi end-of-call transcript; live transcript webhooks |
| Grading | Rubric, verified quotes, grader eval | Without the eval; free-form feedback |
| Storage | Supabase Postgres + Supabase Storage | Supabase + Cloudflare R2; SQLite |
| Frontend | React + Vite | Next.js |
| Hosting | AWS ECS Fargate + ALB | Fly.io; Railway or Render; single EC2 |
| Infra as code | Terraform | AWS CDK in Python; console by hand |
| Demo access | Link plus per-visitor time cap | Sign-in with quota; private plus video |
| Latency target | Goal, reported with early cut-in rate | Hard requirement |
| Who codes | Agent writes, Ruhan reviews every PR | Ruhan writes the core; agent writes all, Ruhan skims |
| Build order | Thin slice first | Finish each part fully |
| Budget | Nothing out of pocket; credits plus free tiers | Paid monthly budget |
| Which reply counts (after measuring Vapi) | Only the reply Vapi starts speaking: drafts on a copy of the plan, committed on speech start | Group requests by the candidate's words; make Vapi wait longer before replying |
| Live event source (after measuring Vapi) | Text frames on the audio WebSocket; webhooks only for the end-of-call report | Webhooks as first planned; both |

## My calls (flagged for Ruhan to check)

- Grader model is Opus 5.5 (latency doesn't matter after the call, quality
  does).
- FastAPI for the server.
- Frontend hosted on S3 + CloudFront, to keep everything on AWS.
- Two-layer interruption handling (browser ducks, server flushes).
- Browser-side VAD to define "stopped talking" for the latency metric.
- Demo mode is one main question with follow-ups in 5 minutes.
- Demo audio deleted after 7 days.
- ngrok for the local tunnel.
- Each main question is scored on its own.
- The brain keeps its own history; the after-interruption rule above.
- Ruhan records the eval set; eval audio stays out of git.
- Public subnets, no NAT gateway; post-call work in a separate worker.
- Desktop browsers only in v1.
- faster-whisper as an on-demand ECS task for batch transcription, to stay
  free on AWS credits.
- No custom domain: CloudFront's default HTTPS address fronts everything.
- One server task, with the live-session registry in memory.

Two of these are expensive to change once code exists, because the latency
harness, schema, and interruption tests are built around them: the
two-layer interruption handling and browser-side VAD as the definition of
"stopped talking". Ruhan should sign off on those two explicitly.

## Unknowns to check before writing code

These come from Vapi docs that were incomplete when the design was written,
or from things only a measurement can answer.

1. Vapi WebSocket transport: which server-to-client events, if any, arrive on
   the audio socket (the docs only show `hangup` and `end-call` from the
   client).
2. Vapi custom-LLM: the exact request and streaming response format, and
   whether the history it sends reflects what was actually spoken after an
   interruption.
3. Vapi's settings for wait-before-reply and interruption sensitivity, and
   whether its own recording and transcription can be turned off (we record
   ourselves).
4. Vapi per-minute pricing and the max-duration setting.
5. Haiku 5.5 time to first token from the chosen AWS region.
6. ALB WebSocket behavior under long idle gaps.
7. Whether faster-whisper on a Fargate CPU task is accurate and fast enough
   on the eval set. It is the default because it is free on credits. If it
   falls short, a cloud vendor is only an option if its free tier covers
   the usage.
8. Credit balances and expiry dates for Vapi, Claude, and AWS, which set the
   demo caps.

## Left to the builder

- Code structure, libraries, and exact thresholds, each validated by tests or
  the benchmark.
- UI design: clean and specific, nothing that looks like a template.
- First drafts of the question bank and rubric wording, for Ruhan to review.
- Repo layout. Suggested: `server/`, `web/`, `infra/`, `eval/`, `bench/`,
  `docs/`.
