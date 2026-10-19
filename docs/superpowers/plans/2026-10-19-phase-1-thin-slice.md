# Mockingbird Phase 1 (Thin Slice) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A rough Mockingbird working end to end and deployed: a visitor opens
the CloudFront link, talks to the interviewer, can interrupt it, and gets a
basic graded report.

**Architecture:** The browser streams 16 kHz PCM over a WebSocket to a FastAPI
server. The server relays the audio to a Vapi WebSocket-transport call. Vapi
calls the server's custom-LLM endpoint for every reply. There, an interview
plan written in code picks the move, and Claude Haiku 5.5 writes the words.
Vapi webhooks drive audio flushes and the end of the call. After the call,
Claude Opus 5.5 grades the stored turns. Every quote it cites is checked
against the text.

**Tech Stack:**
- Python 3.12, uv, FastAPI, uvicorn, websockets, httpx, asyncpg,
  pydantic-settings, PyYAML, anthropic (1.x), pytest, pytest-asyncio, respx,
  ruff
- React 19, Vite, TypeScript, Vitest
- Terraform, AWS (ECR, ECS Fargate ARM64, ALB, S3, CloudFront, SSM, Budgets)
- Supabase Postgres
- GitHub Actions

**Spec:** `docs/superpowers/specs/2026-10-19-mockingbird-design.md`

**What phase 1 deliberately leaves out** (covered by the phase 2 to 5 plans,
written after this one ships):
- recording audio, the two-layer interruption handling, the re-ask rule,
  heard-text truncation, latency tracing (phase 2)
- batch transcription, word timestamps, fuzzy quote matching, the report's
  click-to-play (phase 3)
- deploy-on-merge CI, a remote Terraform state (phase 4)
- the benchmark harness and the grader eval (phase 5)

Phase 1 grades the text Vapi transcribed, with exact-after-normalizing quote
checks.

## Global Constraints

- Live reply model `claude-haiku-5-5`: `thinking={"type": "disabled"}`,
  `output_config={"effort": "low"}`, `max_tokens=200`, streaming, stable
  system prompt with `cache_control: {"type": "ephemeral"}`. Per-turn
  instructions go in as a mid-conversation `{"role": "system"}` message
  placed after the user turn.
- Grader model `claude-opus-5-5`:
  - `output_config={"effort": "high"}`, thinking left at its default
    (adaptive).
  - Server-side refusal fallback on: `betas=["server-side-fallback-2026-07-01"]`,
    `fallbacks="default"`.
  - Structured output through `messages.parse(..., output_format=GradeResult)`.
- Brain history is append-only. Every `system` message is immediately
  followed by an `assistant` message, or is the last message.
- Demo cap is 300 s. Full mode is 1200 s. Vapi `maxDurationSeconds` is the
  cap plus 30.
- Audio on both sockets: raw `pcm_s16le`, mono, 16000 Hz, 20 ms frames
  (320 samples, 640 bytes).
- API keys never reach the browser.
- No Cloudflare services. Data lives in Supabase Postgres only.
- One ECS task. The live-session registry is in memory.
- Interview rules: one question at a time, at most 2 follow-ups per main
  question, at most two sentences per interviewer line, plain speech with no
  markdown, no feedback before the report.
- Commit messages: imperative subject line and a prose body that says what
  and why. No AI attribution or co-author lines (Ruhan's rule).
- One PR per task, each on its own branch. Each branch is cut from the
  previous task's branch, so the PRs stack, and Ruhan merges them in order.
  Every PR body uses this walkthrough template:

  ```markdown
  ## What changed
  ## Why it is built this way
  ## How to explain it in an interview
  (3 to 5 terse cue-card bullets)
  ## How to check it
  (exact commands)
  ```

- README and UI copy: plain and specific. No claims without a command behind
  them.

## Review Focus

1. **The browser tab closes or the network drops mid-interview.** The Vapi
   call must end within about a second, not keep burning credits until
   `maxDurationSeconds`. Pinned in Task 9.
2. **Vapi calls the brain for an unknown or already-ended call, or sends a
   new request while the previous reply is still streaming.** It gets a short
   fixed line, never a 500. The old stream is cancelled, and the history
   stays valid. Pinned in Tasks 6 and 11.
3. **Claude errors, times out, or refuses during a live turn.** The
   interviewer says a fixed line for that move and the interview continues.
   Pinned in Task 6.
4. **The candidate never says anything,** for example the mic is muted and
   the call times out on silence. The report says no answer was recorded,
   without calling the grader. Pinned in Task 10.
5. **Someone replays a WebSocket token, opens a second tab, or posts to the
   webhook with a wrong secret.** The token is rejected (close code 4401),
   and the webhook gets a 404. Pinned in Task 11.

---

## Prerequisites (Ruhan, before Task 1)

- `brew install terraform awscli ngrok`, plus `ngrok config add-authtoken
  <token>` (free ngrok account).
- Keys, saved to a local `.env` (never committed):
  - `ANTHROPIC_API_KEY`
  - `VAPI_API_KEY` (the private key)
  - `DATABASE_URL` (Supabase session-pooler connection string)
- AWS CLI signed in to the account with credits (`aws sts get-caller-identity`
  works).
- An alert email for AWS Budgets.
- A monthly spend limit set in the Claude console, and a check of the Vapi
  credit balance. Together these set `DAILY_DEMO_MINUTES`.
- Create a public GitHub repo `mockingbird` under `ruhan-sahasi`. The agent
  asks before creating or pushing it.

## File Structure

```
pyproject.toml                      uv project: server package + dev tools
.env.example                        every setting name, no values
server/mockingbird/
  config.py                         Settings (env vars)
  app.py                            create_app(): routes, registry, store, clients
  models.py                         Turn (Task 6), Interview (Task 7)
  plan.py                           InterviewPlan state machine (pure)
  questions.py                      load_questions() from data/questions.yaml
  brain.py                          Brain: history, move -> Claude stream, fallbacks
  claude.py                         AnthropicStreamer (live), make_anthropic_client()
  sse.py                            OpenAI chat-completion SSE encoding
  vapi_types.py                     parse custom-LLM requests and webhook messages
  vapi_client.py                    build_assistant(), VapiClient.create_call()
  relay.py                          Relay: browser socket <-> Vapi socket
  sessions.py                       LiveSession, SessionRegistry
  store.py                          Store protocol, MemoryStore, PostgresStore
  limits.py                         visitor and daily-budget checks
  report.py                         rubric, Grader, verify_quotes(), build_report()
  routes_api.py                     /api/interviews, /api/interviews/{id}/report, /healthz
  routes_ws.py                      /ws/interview/{id}
  routes_vapi.py                    /vapi/llm/{secret}/chat/completions, /vapi/webhook/{secret}
  migrate.py                        apply migrations/*.sql in order
server/data/questions.yaml          behavioral question bank
server/data/rubric.yaml             six rubric items with 1-4 descriptors
server/migrations/001_init.sql      interviews, turns, scores, quotes
server/tests/...                    one test file per module, fixtures/vapi/*.json
scripts/spike_vapi.py               Task 1 throwaway recorder
docs/vapi-contract.md               what the spike found
web/                                Vite + React + TS app
  src/audio/resample.ts, ring.ts, pcm.ts       pure audio helpers
  src/audio/capture.worklet.ts, playback.worklet.ts
  src/audio/mic.ts, player.ts
  src/protocol.ts, api.ts, device.ts
  src/pages/Landing.tsx, Interview.tsx, Report.tsx
  src/App.tsx, main.tsx, styles.css
Dockerfile
infra/*.tf                          network, ecr, ecs, alb, cdn, secrets, iam, budgets
scripts/deploy.sh
.github/workflows/ci.yml, keepalive.yml
```

---

### Task 1: Vapi contract spike

Throwaway. Its outputs are the fixtures and notes that Tasks 4, 8 and 9
build on. It spends a few minutes of Vapi credit.

**Files:**
- Create: `scripts/spike_vapi.py`
- Create: `server/tests/fixtures/vapi/` (recorded JSON files)
- Create: `docs/vapi-contract.md`

**Interfaces:**
- Produces:
  - `server/tests/fixtures/vapi/llm_request_turn1.json`
  - `llm_request_after_interrupt.json`
  - `webhook_speech_update.json`
  - `webhook_user_interrupted.json`
  - `webhook_status_update_ended.json`
  - `webhook_end_of_call_report.json`
  - `ws_text_frames.jsonl`
  - `create_call_response.json`
  - `docs/vapi-contract.md`, with one section per numbered question below

- [ ] **Step 1: Write `scripts/spike_vapi.py`**

  A FastAPI app on port 8000 with three handlers:
  - `POST /spike/llm/chat/completions` saves the raw request body, then
    streams one fixed sentence in OpenAI SSE chunk format followed by
    `data: [DONE]`.
  - `POST /spike/webhook` saves each body under its `message.type`.
  - A `/spike/mic` WebSocket bridges a local client. A CLI subcommand
    `call` creates a Vapi call with `transport.provider = "vapi.websocket"`
    and the assistant from Task 8's field list, with `model.url` and
    `server.url` pointing at the ngrok URL. It then connects to
    `transport.websocketCallUrl`, streams a 16 kHz WAV of a spoken answer,
    and logs every text frame to `ws_text_frames.jsonl`.

  Include `artifactPlan: {"recordingEnabled": false}` to test whether Vapi
  accepts it.

- [ ] **Step 2: Run it against real Vapi**

  Run: `ngrok http 8000` in one shell, `uv run python scripts/spike_vapi.py
  serve` in a second, `uv run python scripts/spike_vapi.py call --wav
  answer.wav --interrupt-at 1.5` in a third.

  Expected:
  - The fixture files above exist.
  - The call ends on `end-call`.
  - The second answer is sent while the interviewer is speaking, which
    produces `llm_request_after_interrupt.json` and
    `webhook_user_interrupted.json`.

- [ ] **Step 3: Write `docs/vapi-contract.md`**

  Answer each question with the evidence file:
  1. What path does Vapi append to `model.url`?
  2. Where do `call.id` and the newest user utterance sit in the request
     body?
  3. After an interruption, does the request history hold the full generated
     reply or only what was spoken?
  4. Which text frames arrive on the audio WebSocket?
  5. Is `artifactPlan.recordingEnabled` accepted?
  6. Which sample rate comes back on the socket?
  7. Is the `end-call` text frame honored?

- [ ] **Step 4: Commit**

  ```bash
  git add scripts/spike_vapi.py server/tests/fixtures/vapi docs/vapi-contract.md
  git commit -m "Record Vapi's real call payloads as test fixtures"
  ```

  Commit body: the facts found, one line each.

**If the spike contradicts this plan** (for example the path is not
`/chat/completions`, or the call id lives elsewhere), the contract doc is
the authority. Tasks 4 and 8 follow it, and the PR walkthrough says what
changed.

---

### Task 2: Project scaffold, settings, health check, CI

**Files:**
- Create: `pyproject.toml`
- Create: `.env.example`
- Create: `.gitignore`
- Create: `server/mockingbird/__init__.py`
- Create: `server/mockingbird/config.py`
- Create: `server/mockingbird/app.py`
- Create: `server/tests/test_health.py`
- Create: `.github/workflows/ci.yml`

**Interfaces:**
- Produces:
  - `Settings` (pydantic-settings, env-driven) with fields:
    - `anthropic_api_key: str`
    - `vapi_api_key: str`
    - `database_url: str | None = None`
    - `public_base_url: str`
    - `brain_secret: str`
    - `webhook_secret: str`
    - `owner_key: str`
    - `visitor_salt: str`
    - `daily_demo_minutes: int = 60`
    - `demo_cap_s: int = 300`
    - `full_cap_s: int = 1200`
  - `get_settings() -> Settings`
  - `create_app(settings: Settings, *, store: Store | None = None,
    vapi: VapiClient | None = None, streamer_factory=None, grader=None) ->
    FastAPI`. The keyword injections are how later tasks pass in fakes.
  - Dependencies: fastapi, uvicorn[standard], websockets, httpx, asyncpg,
    pydantic-settings, pyyaml, anthropic>=1. Dev: pytest, pytest-asyncio
    (mode=auto), respx, ruff.

- [ ] **Step 1: Write the failing test**

  ```python
  def test_healthz_ok(client):
      r = client.get("/healthz")
      assert r.status_code == 200
      assert r.json() == {"ok": True}
  ```

  `client` is a fixture in `server/tests/conftest.py`. It builds
  `create_app(test_settings(), store=MemoryStore())` and wraps it in
  `fastapi.testclient.TestClient`. `test_settings()` returns a `Settings`
  with dummy values. Add the store import in Task 7. Until then, the
  fixture passes `store=None`.

- [ ] **Step 2: Run it**

  Run: `uv run pytest server/tests/test_health.py -v`
  Expected: FAIL (`create_app` not defined).

- [ ] **Step 3: Implement `create_app` with `GET /healthz`**

  `GET /healthz` returns `{"ok": true}`. With `?db=1` it also runs
  `SELECT 1` through the store, which is the Supabase keep-alive used in
  Task 14.

- [ ] **Step 4: Run it**

  Run: `uv run pytest -v && uv run ruff check .`
  Expected: PASS, no lint errors.

- [ ] **Step 5: Add `.github/workflows/ci.yml`**

  A `python` job on `ubuntu-latest`: `astral-sh/setup-uv`, `uv sync`,
  `uv run ruff check .`, `uv run pytest`. Task 12 adds a `web` job. Task 14
  adds a `terraform` job.

- [ ] **Step 6: Commit**

  ```bash
  git add pyproject.toml uv.lock .env.example .gitignore server .github
  git commit -m "Scaffold the server with settings, a health check and CI"
  ```

---

### Task 3: Interview plan (pure state machine)

**Files:**
- Create: `server/mockingbird/plan.py`
- Create: `server/mockingbird/questions.py`
- Create: `server/data/questions.yaml`
- Test: `server/tests/test_plan.py`

**Interfaces:**
- Produces:
  - `Question(id: str, text: str, competency: str)` (frozen dataclass).
  - `PlanConfig(total_s: int, main_questions: int, max_follow_ups: int,
    wrap_up_reserve_s: int, new_question_min_s: int, short_answer_words: int)`
    (frozen), with these presets:
    - `DEMO = PlanConfig(300, 1, 2, 40, 0, 25)`
    - `FULL = PlanConfig(1200, 3, 2, 90, 240, 25)`
  - `MoveKind(StrEnum)` with the values `ASK_QUESTION`, `FOLLOW_UP`,
    `WRAP_UP`, `DONE`.
  - `Move(kind: MoveKind, question_idx: int, question: Question | None,
    probe: Literal["expand", "weakest_part"] | None)`.
  - `InterviewPlan(config, questions: Sequence[Question])`. Raises
    `ValueError` if `len(questions) < config.main_questions`.
  - `.opening_line() -> str`
  - `.next_move(elapsed_s: float, answer_words: int) -> Move`
  - `.finished: bool`
  - `.current_question_idx: int`
  - `load_questions(path: Path, k: int, rng: random.Random) -> list[Question]`

**Rules for `next_move`, applied in order:**
1. If `finished`, return `DONE`.
2. If `total_s - elapsed_s <= wrap_up_reserve_s`, return `WRAP_UP` and set
   `finished`.
3. If the follow-ups used on the current question are fewer than
   `max_follow_ups`, return `FOLLOW_UP`. Its `probe` is `"expand"` when
   `answer_words < short_answer_words`, otherwise `"weakest_part"`.
4. If questions asked are fewer than `main_questions` and the time left is
   at least `new_question_min_s`, return `ASK_QUESTION` for the next
   question and reset the follow-up count.
5. Otherwise return `WRAP_UP` and set `finished`.

**`opening_line()` copy (exact):** `"Hi, I'm Mockingbird, and I'll be your
interviewer. This is a {minutes} minute behavioral interview, and you can
cut me off any time. {question.text}"`. Here `minutes` is
`total_s // 60`. Calling it counts question 0 as asked.

`questions.yaml` holds at least 12 questions across these competencies:
ownership, conflict, failure, ambiguity, leadership, impact.

- [ ] **Step 1: Write the failing tests**

  ```python
  def test_demo_follows_up_twice_then_wraps_up():
      p = InterviewPlan(DEMO, qs(3)); p.opening_line()
      assert p.next_move(30, 80).kind is MoveKind.FOLLOW_UP
      assert p.next_move(60, 80).kind is MoveKind.FOLLOW_UP
      assert p.next_move(90, 80).kind is MoveKind.WRAP_UP
      assert p.next_move(95, 10).kind is MoveKind.DONE

  def test_short_answer_probes_expand():
      p = InterviewPlan(DEMO, qs(1)); p.opening_line()
      assert p.next_move(20, 10).probe == "expand"
      assert p.next_move(40, 60).probe == "weakest_part"

  def test_wraps_up_inside_reserve_even_with_follow_ups_left():
      p = InterviewPlan(DEMO, qs(1)); p.opening_line()
      assert p.next_move(261, 80).kind is MoveKind.WRAP_UP

  def test_full_moves_to_next_question_after_two_follow_ups():
      p = InterviewPlan(FULL, qs(3)); p.opening_line()
      p.next_move(60, 80); p.next_move(120, 80)
      m = p.next_move(180, 80)
      assert m.kind is MoveKind.ASK_QUESTION and m.question_idx == 1

  def test_full_skips_new_question_when_under_min_time():
      p = InterviewPlan(FULL, qs(3)); p.opening_line()
      p.next_move(900, 80); p.next_move(950, 80)
      assert p.next_move(1000, 80).kind is MoveKind.WRAP_UP

  def test_never_more_than_max_follow_ups_property():
      # 500 seeded random sequences of (elapsed increasing, words 0..200):
      # per question_idx, count FOLLOW_UPs <= 2; exactly one WRAP_UP; DONE after it
      ...

  def test_opening_line_copy():
      p = InterviewPlan(DEMO, [Question("q1", "Tell me about a time you disagreed with a teammate.", "conflict")])
      assert p.opening_line() == ("Hi, I'm Mockingbird, and I'll be your interviewer. This is a 5 minute "
          "behavioral interview, and you can cut me off any time. Tell me about a time you disagreed with a teammate.")

  def test_too_few_questions_raises():
      with pytest.raises(ValueError): InterviewPlan(FULL, qs(2))
  ```

  `qs(n)` builds `n` dummy questions. The property test body is spelled out
  in the comment above: use `random.Random(seed)` for seeds 0 to 499.

- [ ] **Step 2: Run them**

  Run: `uv run pytest server/tests/test_plan.py -v`
  Expected: FAIL (import error).

- [ ] **Step 3: Implement `plan.py`, `questions.py` and `questions.yaml`**

  Follow the rules and copy above.

- [ ] **Step 4: Run them**

  Run: `uv run pytest server/tests/test_plan.py -v`
  Expected: PASS.

- [ ] **Step 5: Commit**

  `git commit -m "Add the interview plan as a tested state machine"`

---

### Task 4: Vapi payload parsing

**Files:**
- Create: `server/mockingbird/vapi_types.py`
- Test: `server/tests/test_vapi_types.py`

**Interfaces:**
- Consumes: the Task 1 fixtures and `docs/vapi-contract.md`.
- Produces:
  - `LlmRequest(call_id: str, user_text: str)`. `user_text` is the content
    of every `user` message after the last `assistant` message, joined with
    a space and stripped.
  - `parse_llm_request(body: dict) -> LlmRequest`. Raises `ValueError` if
    there is no call id.
  - Webhook events, all frozen dataclasses:
    - `SpeechUpdate(call_id, role: str, status: str)`
    - `UserInterrupted(call_id)`
    - `StatusUpdate(call_id, status: str)`
    - `EndOfCallReport(call_id, ended_reason: str)`
    - `Ignored(call_id: str | None, type: str)`
  - `parse_server_message(body: dict) -> SpeechUpdate | UserInterrupted |
    StatusUpdate | EndOfCallReport | Ignored`. It reads the
    `{"message": {"type": ..., "call": {"id": ...}}}` envelope.

- [ ] **Step 1: Write the failing tests**

  One test per fixture. Each asserts the parsed dataclass and its fields
  against the recorded values, for example:
  - `parse_server_message(load("webhook_user_interrupted.json")) ==
    UserInterrupted(call_id=<recorded id>)`
  - for `llm_request_turn1.json`, `user_text` equals the spoken answer's
    transcript as recorded

  Add hand-written cases:
  - an unknown `type` gives `Ignored`
  - a request with two trailing user messages joins them
  - a request with no `call` raises `ValueError`

- [ ] **Step 2: Run them**

  Run: `uv run pytest server/tests/test_vapi_types.py -v`
  Expected: FAIL.

- [ ] **Step 3: Implement `vapi_types.py`**

  Plain dict access. Read only the fields named above, so unrelated changes
  in Vapi's payloads can't break parsing.

- [ ] **Step 4: Run them**

  Run: `uv run pytest server/tests/test_vapi_types.py -v`
  Expected: PASS.

- [ ] **Step 5: Commit**

  `git commit -m "Parse Vapi's LLM requests and webhooks into typed events"`

---

### Task 5: OpenAI-format SSE encoding

**Files:**
- Create: `server/mockingbird/sse.py`
- Test: `server/tests/test_sse.py`

**Interfaces:**
- Produces:
  - `sse_chunk(text: str, *, chunk_id: str, created: int, first: bool = False,
    finish: bool = False) -> str`
  - `SSE_DONE = "data: [DONE]\n\n"`
  - `MODEL_NAME = "mockingbird-brain"`

- [ ] **Step 1: Write the failing test**

  ```python
  def test_chunk_shape():
      line = sse_chunk("Hi", chunk_id="c1", created=1, first=True)
      assert line.startswith("data: ") and line.endswith("\n\n")
      body = json.loads(line[6:])
      assert body["object"] == "chat.completion.chunk" and body["model"] == "mockingbird-brain"
      assert body["choices"][0]["delta"] == {"role": "assistant", "content": "Hi"}
      assert body["choices"][0]["finish_reason"] is None

  def test_finish_chunk():
      body = json.loads(sse_chunk("", chunk_id="c1", created=1, finish=True)[6:])
      assert body["choices"][0]["finish_reason"] == "stop" and body["choices"][0]["delta"] == {}
  ```

- [ ] **Step 2: Run it.** Expected: FAIL.
- [ ] **Step 3: Implement `sse.py`.**
- [ ] **Step 4: Run it.** Expected: PASS.
- [ ] **Step 5: Commit**

  `git commit -m "Encode brain replies as OpenAI chat-completion chunks"`

---

### Task 6: Interview brain

**Files:**
- Create: `server/mockingbird/brain.py`
- Create: `server/mockingbird/claude.py`
- Create: `server/mockingbird/models.py` (`Turn` only; Task 7 adds the rest)
- Test: `server/tests/test_brain.py`

**Interfaces:**
- Consumes: `InterviewPlan`, `Move` and `MoveKind` (Task 3).
- Produces:
  - `ClaudeStreamer` (Protocol): `def stream(self, *, system: list[dict],
    messages: list[dict]) -> AsyncIterator[str]`. Raises `BrainError` on an
    API error, a timeout, or `stop_reason == "refusal"`.
  - `AnthropicStreamer(client: anthropic.AsyncAnthropic)` implements it
    with:

    ```python
    client.messages.stream(
        model="claude-haiku-5-5",
        max_tokens=200,
        thinking={"type": "disabled"},
        output_config={"effort": "low"},
        system=system,
        messages=messages,
    )
    ```

    It yields from `stream.text_stream`. The client is made with
    `timeout=anthropic.Timeout(10.0, connect=2.0)` and `max_retries=1`.
  - `Brain(plan: InterviewPlan, streamer: ClaudeStreamer, clock:
    Callable[[], float])`. The plan's elapsed time is `clock() -
    started_at`, where `started_at` is set by `.start()`. Members:
    - `.start() -> str`. Sets the start time, seeds the history, and returns
      the opening line.
    - `async .reply(candidate_text: str) -> AsyncIterator[str]`
    - `.history: list[dict]` (read-only view)
    - `.finished: bool`
    - `.turns: list[Turn]`: one entry per spoken line, starting with the
      opening line as interviewer turn 0. Each entry's `question_idx` is the
      plan's `current_question_idx` at that moment, and `idx` counts up
      from 0.
  - `Turn` dataclass in `models.py`: `idx: int`, `speaker:
    Literal["interviewer", "candidate"]`, `text: str`, `question_idx: int`,
    `interrupted: bool = False`.
  - `STABLE_SYSTEM: str`: persona plus every rule from Global Constraints.
    It must exceed 512 tokens so it caches. It is sent as
    `[{"type": "text", "text": STABLE_SYSTEM, "cache_control": {"type":
    "ephemeral"}}]`.
  - `MOVE_INSTRUCTION`, keyed by move:
    - `FOLLOW_UP` + `"expand"`: `"Ask one short follow-up that asks them to
      say more about what they just described."`
    - `FOLLOW_UP` + `"weakest_part"`: `"Ask one short follow-up about
      whichever part of their answer was thinnest: the situation, their
      task, the actions they took, or the result."`
    - `ASK_QUESTION`: `"In a few words acknowledge their answer, then ask
      exactly this question: {text}"`
  - `FALLBACK_LINE`, used when the streamer raises:
    - `FOLLOW_UP`: `"Could you walk me through that in a bit more detail?"`
    - `ASK_QUESTION`: `"Thanks. {question.text}"`
  - `WRAP_UP_LINE = "Thanks, that's all the time we have. Your report will be
    ready in about a minute."` Wrap-up never calls Claude.

**History shape:**
- `.start()` appends `{"role": "user", "content": "(The call has
  connected.)"}` and then the opening line as an assistant message.
- Each `reply()` appends, in order:
  1. the user message with the candidate's text
  2. the system message with the move instruction
  3. after streaming, the assistant message with the full text
- If the stream is cancelled (`asyncio.CancelledError`), the assistant
  message holds the text produced so far, or `"(cut off)"` if there was
  none. That keeps the history valid.

- [ ] **Step 1: Write the failing tests**

  These use a `FakeStreamer` that yields scripted chunks, raises on demand,
  and records each call's `system` and `messages`.

  ```python
  async def test_reply_sends_cached_system_and_trailing_move_instruction(): ...
      # recorded system[0]["cache_control"] == {"type": "ephemeral"}
      # recorded messages[-1]["role"] == "system" and messages[-2] == {"role": "user", "content": "I led the migration."}

  async def test_history_alternates_validly_after_three_turns(): ...
      # every "system" entry is followed by an "assistant" entry; history[0]["role"] == "user"

  async def test_streamer_error_yields_fallback_line_and_interview_continues(): ...
      # FakeStreamer raises BrainError; output == ["Could you walk me through that in a bit more detail?"]
      # next reply() works normally

  async def test_wrap_up_uses_fixed_line_without_calling_claude(): ...
      # clock past total_s - wrap_up_reserve_s; output == [WRAP_UP_LINE]; FakeStreamer.calls unchanged; brain.finished

  async def test_cancelled_reply_records_partial_text_and_keeps_history_valid(): ...
      # cancel after first chunk; history[-1] == {"role": "assistant", "content": "<first chunk>"}

  async def test_done_after_wrap_up_returns_empty(): ...

  async def test_turns_record_speaker_and_question_idx(): ...
      # FULL plan, scripted through a move to question 1:
      # [t.speaker for t in brain.turns] alternates starting with "interviewer"
      # and the turn after the ASK_QUESTION has question_idx == 1
  ```

  `test_anthropic_streamer_request_shape` uses `respx` to mock
  `POST https://api.anthropic.com/v1/messages`. It asserts the JSON body has
  `model == "claude-haiku-5-5"`, `thinking == {"type": "disabled"}`,
  `output_config == {"effort": "low"}`, `max_tokens == 200` and
  `stream == true`. It returns a minimal SSE stream so the call completes.

- [ ] **Step 2: Run them**

  Run: `uv run pytest server/tests/test_brain.py -v`
  Expected: FAIL.

- [ ] **Step 3: Implement `brain.py` and `claude.py`**

  Follow the interfaces and history shape above. Word count for the plan is
  `len(candidate_text.split())`.

- [ ] **Step 4: Run them**

  Run: `uv run pytest server/tests/test_brain.py -v`
  Expected: PASS.

- [ ] **Step 5: Commit**

  `git commit -m "Add the interview brain: code picks the move, Haiku writes the line"`

---

### Task 7: Store and migrations

**Files:**
- Modify: `server/mockingbird/models.py` (add `Mode`, `Status`, `Interview`)
- Create: `server/mockingbird/store.py`
- Create: `server/mockingbird/migrate.py`
- Create: `server/migrations/001_init.sql`
- Test: `server/tests/test_store.py`

**Interfaces:**
- Produces:
  - `Mode = Literal["demo", "full"]`
  - `Status = Literal["created", "live", "ended", "graded", "abandoned"]`
  - `Interview` dataclass: `id: str`, `mode: Mode`, `status: Status`,
    `visitor_hash: str`, `ip_hash: str`, `cap_s: int`, `questions:
    list[str]` (main question texts in order), `created_at: datetime`,
    `vapi_call_id: str | None = None`, `ended_reason: str | None = None`,
    `duration_s: int | None = None`
  - `Turn` is from Task 6.
  - `Store` (Protocol, all async):
    - `create_interview(i: Interview) -> None`
    - `get_interview(id) -> Interview | None`
    - `update_interview(id, **fields) -> None`
    - `add_turn(interview_id, t: Turn) -> None`
    - `list_turns(interview_id) -> list[Turn]`
    - `save_report(interview_id, report: dict) -> None`
    - `get_report(interview_id) -> dict | None`
    - `seconds_used_today() -> int`
    - `starts_today(visitor_hash: str, ip_hash: str) -> int`
    - `ping() -> None`
    - `delete_demo_data_older_than(days: int) -> int`. Deletes demo
      interviews created more than `days` days ago, with their turns, scores
      and quotes, and returns how many interviews it deleted. This is the
      design's 7-day retention.
  - Implementations: `MemoryStore()` and `PostgresStore(pool:
    asyncpg.Pool)`.
  - `seconds_used_today` = the sum of `duration_s` for demo interviews that
    ended today (UTC), plus `cap_s` for every demo interview with status
    `live`.
  - `starts_today` counts today's demo interviews with status other than
    `abandoned` where the visitor hash or the IP hash matches.

**`001_init.sql`:**
- `interviews`: columns as in the `Interview` dataclass (`questions` is
  `jsonb`), plus `report jsonb`. Foreign keys from `turns`, `scores` and
  `quotes` use `on delete cascade`.
- `turns`: `interview_id`, `idx`, `speaker`, `text`, `question_idx`,
  `interrupted`. Primary key `(interview_id, idx)`.
- `scores`: `interview_id`, `question_idx`, `item`, `score int null`,
  `rationale`, `status`.
- `quotes`: `score_id`, `text`, `start_ms int null`, `end_ms int null`,
  `verified bool`.

Phase 1 writes the report jsonb and fills `scores` and `quotes` from it.
Phase 3 adds the timestamps.

- [ ] **Step 1: Write the failing tests**

  These are parametrized over `MemoryStore`, plus `PostgresStore` when
  `TEST_DATABASE_URL` is set. Otherwise the Postgres cases are skipped.

  - create, get and update round-trip
  - turns come back ordered by `idx`
  - `seconds_used_today` counts a live demo at its `cap_s` and an ended demo
    at its `duration_s`, and ignores full-mode and abandoned interviews
  - `starts_today` matches on the visitor hash alone or the IP hash alone,
    and ignores abandoned interviews
  - `delete_demo_data_older_than(7)`:
    - removes an 8-day-old demo and its turns
    - keeps a 6-day-old demo
    - keeps an 8-day-old full-mode interview

- [ ] **Step 2: Run them**

  Run: `uv run pytest server/tests/test_store.py -v`
  Expected: FAIL.

- [ ] **Step 3: Implement the store and migrations**

  `models.py`, `store.py`, `001_init.sql` and `migrate.py`. `migrate.py`
  applies each file in `server/migrations/` in name order, inside a
  transaction, and tracks applied files in a `schema_migrations` table.

- [ ] **Step 4: Run them**

  Run: `uv run pytest server/tests/test_store.py -v`
  Expected: PASS.

  Then run once against Supabase: `TEST_DATABASE_URL=$DATABASE_URL uv run
  pytest server/tests/test_store.py`.

- [ ] **Step 5: Commit**

  `git commit -m "Store interviews and turns in memory or Supabase Postgres"`

---

### Task 8: Vapi client and assistant config

**Files:**
- Create: `server/mockingbird/vapi_client.py`
- Test: `server/tests/test_vapi_client.py`

**Interfaces:**
- Consumes: `docs/vapi-contract.md` (the path appended to `model.url`, and
  whether the recording field is accepted).
- Produces:
  - `build_assistant(opening_line: str, *, llm_url: str, webhook_url: str,
    cap_s: int) -> dict`
  - `VapiCall(id: str, websocket_url: str)`
  - `VapiClient(api_key: str, http: httpx.AsyncClient)` with
    `async create_call(assistant: dict) -> VapiCall`. It POSTs
    `https://api.vapi.ai/call` with `Authorization: Bearer <key>` and this
    body:

    ```python
    {
        "assistant": assistant,
        "transport": {
            "provider": "vapi.websocket",
            "audioFormat": {"format": "pcm_s16le", "container": "raw", "sampleRate": 16000},
        },
    }
    ```

    A non-2xx response raises `VapiError`.

**`build_assistant` returns exactly these keys:**

```python
{
    "firstMessage": opening_line,
    "model": {"provider": "custom-llm", "url": llm_url, "model": "mockingbird-brain"},
    "server": {"url": webhook_url},
    "serverMessages": ["status-update", "speech-update", "user-interrupted", "end-of-call-report"],
    "maxDurationSeconds": cap_s + 30,
    "silenceTimeoutSeconds": 30,
    "startSpeakingPlan": {"waitSeconds": 0.4},
    "stopSpeakingPlan": {"numWords": 0, "voiceSeconds": 0.2, "backoffSeconds": 1},
    "artifactPlan": {"recordingEnabled": False},  # only if the spike found it accepted
}
```

The URLs it receives:
- `llm_url = f"{public_base_url}/vapi/llm/{brain_secret}"`. Vapi appends the
  path recorded in the contract doc.
- `webhook_url = f"{public_base_url}/vapi/webhook/{webhook_secret}"`.

- [ ] **Step 1: Write the failing tests**

  - `test_build_assistant_exact`: compares the dict to the literal above for
    `cap_s=300`, with `maxDurationSeconds == 330`.
  - `test_create_call_parses_id_and_ws_url`: `respx` mocks the POST and
    returns `create_call_response.json` from Task 1. Asserts
    `VapiCall.id` and `.websocket_url` match `transport.websocketCallUrl`,
    and that the sent body's `transport.provider` is `"vapi.websocket"`.
  - `test_create_call_raises_on_400`

- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement `vapi_client.py`.**
- [ ] **Step 4: Run them.** Expected: PASS.
- [ ] **Step 5: Commit**

  `git commit -m "Create Vapi WebSocket calls with a custom-LLM assistant"`

---

### Task 9: Audio relay

**Files:**
- Create: `server/mockingbird/relay.py`
- Test: `server/tests/test_relay.py`

**Interfaces:**
- Produces:
  - `Socket` (Protocol):
    - `async recv() -> bytes | str | None` (`None` means closed)
    - `async send_bytes(b: bytes) -> None`
    - `async send_text(s: str) -> None`
    - `async close(code: int = 1000) -> None`
  - Two adapters: `StarletteSocket(ws)` for the browser side and
    `WebsocketsSocket(conn)` for the Vapi side.
  - `Relay(browser: Socket, vapi: Socket, *, cap_s: float, ping_every_s:
    float = 20.0)`, with:
    - `async run() -> str`. Returns the end reason: `"cap"`,
      `"browser_closed"`, `"vapi_closed"` or `"plan_done"`.
    - `async flush_browser() -> None`. Sends `{"type": "flush"}` to the
      browser.
    - `async end(reason: str) -> None`. Idempotent. Sends `{"type":
      "end-call"}` to Vapi, then `{"type": "ended", "reason": reason}` to
      the browser, closes both, and makes `run()` return `reason`.

**Behavior:**
- Browser binary frames go to Vapi unchanged.
- Vapi binary frames go to the browser unchanged.
- Vapi text frames are logged and not forwarded.
- Browser text `{"type": "end"}` means `end("browser_closed")`.
- After `cap_s`, `end("cap")`.
- The browser gets `{"type": "ping"}` every `ping_every_s`.

- [ ] **Step 1: Write the failing tests**

  These use `FakeSocket`, backed by two `asyncio.Queue`s, and a
  `cap_s=0.2` where needed.

  - `test_frames_flow_both_ways`
  - `test_vapi_text_frames_not_forwarded`
  - `test_cap_ends_call_and_tells_both_sides`: Vapi received `{"type":
    "end-call"}`, the browser received `{"type": "ended", "reason": "cap"}`,
    and `run()` returned `"cap"`.
  - `test_browser_disconnect_ends_vapi_call_within_one_second` (Review
    Focus 1): browser `recv()` returns `None`. Assert that Vapi received
    `end-call` and was closed within 1.0 s.
  - `test_end_is_idempotent`
  - `test_flush_browser_sends_flush`

- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement `relay.py`**

  Two pump tasks plus a cap timer and a ping task under one
  `asyncio.TaskGroup`. `end()` cancels the group.

- [ ] **Step 4: Run them.** Expected: PASS.
- [ ] **Step 5: Commit**

  `git commit -m "Relay audio between the browser and Vapi with a hard time cap"`

---

### Task 10: Basic grader and report

**Files:**
- Create: `server/mockingbird/report.py`
- Create: `server/data/rubric.yaml`
- Test: `server/tests/test_report.py`

**Interfaces:**
- Consumes: `Store` and `Turn` (Task 7).
- Produces:
  - Pydantic models:
    - `ItemGrade(item: Literal["situation", "task", "action", "result",
      "specificity", "ownership"], score: int = Field(ge=1, le=4),
      rationale: str, quotes: list[str])`
    - `QuestionGrade(question_idx: int, items: list[ItemGrade])`
    - `GradeResult(questions: list[QuestionGrade])`
  - `normalize(text: str) -> str`: lowercase, curly quotes to straight,
    punctuation removed, whitespace collapsed.
  - `verify_quotes(result: GradeResult, candidate_text_by_q: dict[int, str])
    -> dict`. The report dict looks like this:

    ```
    {"status": "graded", "questions": [{"question_idx", "question_text",
      "items": [{"item", "score" | None, "rationale", "quotes",
      "dropped_quotes", "status": "scored" | "not_enough_evidence"}]}]}
    ```

    A quote is kept only if `normalize(quote)` is a substring of
    `normalize(answer text for that question)`. An item left with no kept
    quote gets `score = None` and status `not_enough_evidence`.
  - `Grader(client: anthropic.AsyncAnthropic, rubric: dict)` with
    `async grade(turns: list[Turn], questions: list[str]) -> GradeResult`.
    The call:

    ```python
    client.beta.messages.parse(
        model="claude-opus-5-5",
        max_tokens=16000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        output_config={"effort": "high"},
        system=GRADER_SYSTEM,
        messages=[{"role": "user", "content": transcript_text}],
        output_format=GradeResult,
    )
    ```

    If the installed SDK has no `beta.messages.parse`, use
    `beta.messages.create` with `output_config.format` built from
    `GradeResult.model_json_schema()` and validate the result. Note which
    path was used in the PR. A refusal raises `GradingError`.
  - `GRADER_SYSTEM`:
    - grade each main question (with its follow-ups) separately on the six
      items, using the rubric descriptors
    - every score cites one or more verbatim quotes of the candidate's own
      words
    - never quote the interviewer
  - `async build_report(store, grader, interview_id) -> None`. It reads the
    question texts from `Interview.questions` and labels each report
    question with its `question_text`.
    - With zero candidate turns, it saves `{"status": "no_answers",
      "questions": []}` without calling the grader (Review Focus 4).
    - On `GradingError`, it saves `{"status": "grading_failed",
      "questions": []}`.
    - Otherwise it saves the verified report, sets status `graded`, and
      fills the `scores` and `quotes` tables.

- [ ] **Step 1: Write the failing tests**

  - `test_normalize`: `normalize("I “led” it,  fast!") == "i led
    it fast"`
  - `test_verify_keeps_real_quote_and_drops_invented`
  - `test_item_without_verified_quote_is_not_enough_evidence`
  - `test_quotes_checked_against_their_own_question_only`
  - `test_no_candidate_turns_skips_grader` (Review Focus 4): a `FakeGrader`
    whose `grade` raises if called
  - `test_grading_error_saves_failed_status`
  - `test_grader_request_shape`: `respx` mocks `POST
    https://api.anthropic.com/v1/messages*`. Asserts that the body has
    `model == "claude-opus-5-5"` and `fallbacks == "default"`, and that the
    `anthropic-beta` header contains `server-side-fallback-2026-07-01`.
    Returns a canned structured response.

- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement `report.py` and `rubric.yaml`**

  `rubric.yaml` holds the six items, each with descriptors for scores 1
  to 4.

- [ ] **Step 4: Run them.** Expected: PASS. Then run `uv run pytest -v`.
- [ ] **Step 5: Commit**

  `git commit -m "Grade each answer with Opus and keep only quotes the candidate said"`

---

### Task 11: Sessions, limits and routes

**Files:**
- Create: `server/mockingbird/sessions.py`
- Create: `server/mockingbird/limits.py`
- Create: `server/mockingbird/routes_api.py`
- Create: `server/mockingbird/routes_ws.py`
- Create: `server/mockingbird/routes_vapi.py`
- Modify: `server/mockingbird/app.py`
- Test: `server/tests/test_limits.py`, `server/tests/test_routes.py`

**Interfaces:**
- Consumes: everything from Tasks 3 to 10.
- Produces:
  - `LiveSession(interview: Interview, brain: Brain, token: str,
    token_expires_at: float, relay: Relay | None, call_id: str | None,
    reply_task: asyncio.Task | None)`.
  - `SessionRegistry` with:
    - `add(s)`
    - `by_interview(id) -> LiveSession | None`
    - `by_call(call_id) -> LiveSession | None`
    - `bind_call(interview_id, call_id)`
    - `consume_token(interview_id, token) -> LiveSession | None`.
      Single use; fails after `token_expires_at`, which is 60 s after
      creation.
    - `remove(id)`
  - `check_can_start(store, *, visitor_hash, ip_hash, cap_s, daily_demo_s)
    -> Literal["ok", "visitor_limit", "daily_budget"]`.
    - Returns `visitor_limit` if `starts_today >= 1`.
    - Returns `daily_budget` if `seconds_used_today + cap_s > daily_demo_s`.
  - `visitor_id(request) -> tuple[visitor_hash, ip_hash, set_cookie: str |
    None]`.
    - The cookie is `mb_visitor`: uuid4, `HttpOnly`, `Secure`,
      `SameSite=Lax`, one-year max-age.
    - The IP comes from the `CloudFront-Viewer-Address` header (the part
      before the last `:`), else from `request.client.host`.
    - Both values are hashed as `sha256(visitor_salt + value)`.

**Routes:**
- `POST /api/interviews`
  - With header `X-Owner-Key` equal to `owner_key`: full mode, no limits.
    Otherwise demo mode, after `check_can_start`.
  - On success:
    - Loads `qs = load_questions(questions.yaml, config.main_questions,
      random.Random())`.
    - Creates the `Interview` with status `created` and `questions=[q.text
      for q in qs]`.
    - Creates the `LiveSession` with a `Brain` over
      `InterviewPlan(DEMO|FULL, qs)`.
    - Returns `201 {"interview_id", "ws_path":
      "/ws/interview/{id}?token=...", "cap_s"}`.
  - When a limit is hit: `429 {"error": "visitor_limit" | "daily_budget"}`.
- `WS /ws/interview/{id}?token=`
  - A failed `consume_token` closes with code 4401.
  - Otherwise, in order:
    1. `brain.start()` gives the opening line.
    2. `vapi.create_call(build_assistant(...))`, then `bind_call`.
    3. Connect `websockets.connect(call.websocket_url)`.
    4. Set status `live`.
    5. `relay.run()`.
  - When `run()` returns:
    1. Set status `ended`, `duration_s`, and `ended_reason`.
    2. Write `brain.turns` to the store.
    3. Remove the session.
    4. Schedule `build_report` as a background task (Task 10).
  - A sweeper task started by `create_app` (`sweep_once(registry, store,
    now)`, run every 60 s) does two jobs:
    - It marks a `created` interview whose token expired unused as
      `abandoned` and removes its session.
    - Once an hour, it calls `store.delete_demo_data_older_than(7)`.
- `POST /vapi/llm/{secret}/chat/completions`
  - Wrong secret: 404.
  - Parses with `parse_llm_request` and streams SSE (`text/event-stream`).
  - Unknown call, or `brain.finished`: streams the single line `"Sorry,
    this interview has ended."` then `SSE_DONE`, with status 200.
  - If `reply_task` is still running, cancel it first (Review Focus 2).
- `POST /vapi/webhook/{secret}`
  - Wrong secret: 404. Otherwise 200 `{}` for every message.
  - `UserInterrupted`: `relay.flush_browser()`.
  - `SpeechUpdate(role="assistant", status="stopped")` while
    `brain.finished`: `relay.end("plan_done")`.
  - `StatusUpdate(status="ended")` for a live session:
    `relay.end("vapi_closed")`.
- `GET /api/interviews/{id}/report`
  - `200` with the report when one exists.
  - `202 {"status": "grading"}` while `ended`.
  - `404` for an unknown id.

- [ ] **Step 1: Write the failing tests**

  `test_limits.py`, against `MemoryStore`:
  - second start same day for the same cookie gives `visitor_limit`
  - same IP with a new cookie gives `visitor_limit`
  - a budget of 600 s with one live 300 s demo gives `ok` for the next
    start, then `daily_budget`
  - an owner key bypasses limits

  `test_routes.py`. Uses `create_app` with `MemoryStore`, a `FakeVapi`
  (returns `VapiCall("call_1", "ws://fake")`) and a `FakeStreamer`.
  `websockets.connect` is patched to return a `FakeSocket`.

  - `test_start_returns_ws_path_and_sets_cookie`
  - `test_ws_token_reuse_closes_4401` (Review Focus 5)
  - `test_webhook_wrong_secret_404` and `test_llm_wrong_secret_404` (Review
    Focus 5)
  - `test_llm_unknown_call_streams_ended_line` (Review Focus 2)
  - `test_llm_new_request_cancels_inflight_reply` (Review Focus 2): the
    first request's streamer blocks, then a second request arrives. Assert
    the first task was cancelled and the brain history is still valid.
  - `test_user_interrupted_webhook_flushes_browser`
  - `test_assistant_stopped_after_wrap_up_ends_call`
  - `test_report_202_then_200`
  - `test_sweep_marks_expired_unused_interview_abandoned`: create an
    interview, advance `now` by 61 s, and run `sweep_once`. Assert the
    status is `abandoned` and that it no longer counts in `starts_today`.

- [ ] **Step 2: Run them**

  Run: `uv run pytest server/tests/test_limits.py server/tests/test_routes.py -v`
  Expected: FAIL.

- [ ] **Step 3: Implement**

  `sessions.py`, `limits.py`, the three route modules, and the wiring in
  `create_app`. The real dependencies are built from `Settings` when no
  fakes are injected.

- [ ] **Step 4: Run the full suite**

  Run: `uv run pytest -v`
  Expected: all PASS.

- [ ] **Step 5: Commit**

  `git commit -m "Wire interviews end to end: start, relay, brain endpoint and webhooks"`

---

### Task 12: Web app scaffold and pure audio helpers

**Files:**
- Create: `web/` (Vite React TS template)
- Create: `web/src/audio/resample.ts`, `web/src/audio/ring.ts`,
  `web/src/audio/pcm.ts`
- Create: `web/src/protocol.ts`, `web/src/device.ts`
- Test: `web/src/audio/*.test.ts`, `web/src/protocol.test.ts`,
  `web/src/device.test.ts`
- Modify: `.github/workflows/ci.yml` (add the `web` job)

**Interfaces:**
- Produces:
  - `pcm.ts`:
    - `floatToInt16(x: Float32Array): Int16Array` (clamps to [-1, 1])
    - `int16ToFloat(x: Int16Array): Float32Array`
    - `FRAME_SAMPLES = 320`
  - `resample.ts`: `class LinearResampler { constructor(inRate: number,
    outRate: number); push(x: Float32Array): Float32Array }`. It keeps
    fractional position and the last sample across calls, so a stream split
    into chunks matches the same stream resampled in one go.
  - `ring.ts`: `class FloatRing { constructor(capacity: number); write(x:
    Float32Array): number; read(out: Float32Array): number; clear(): void;
    get available(): number }`. `read` fills any shortfall with zeros and
    returns the real sample count.
  - `protocol.ts`:
    - `type ServerMessage = {type: "flush"} | {type: "ping"} | {type:
      "ended", reason: string} | {type: "error", message: string}`
    - `parseServerMessage(s: string): ServerMessage | null`
  - `device.ts`: `isPhone(ua: string, maxTouchPoints: number, width:
    number): boolean`

- [ ] **Step 1: Scaffold the app**

  `npm create vite@latest web -- --template react-ts`, then `npm i -D vitest`.
  Add `"test": "vitest run"` to `package.json`.

- [ ] **Step 2: Write the failing tests**

  - `LinearResampler(48000, 16000)` on 4800 samples gives 1600 ± 1 samples.
  - Chunked resampling equals one-shot resampling within 1e-6.
  - A 1 kHz sine keeps its frequency after a 16000 to 48000 round trip:
    the zero-crossing count stays within 2%.
  - `FloatRing` wrap-around read and write.
  - Underrun zero-fill.
  - `clear()` empties the ring.
  - `floatToInt16([1.5, -2]) -> [32767, -32768]`.
  - `parseServerMessage`: valid types parse, and garbage gives `null`.
  - `isPhone`: an iPhone user agent gives true, and desktop Chrome gives
    false.

- [ ] **Step 3: Run them**

  Run: `cd web && npm test`
  Expected: FAIL.

- [ ] **Step 4: Implement the helpers**

- [ ] **Step 5: Run them**

  Run: `cd web && npm test && npx tsc --noEmit`
  Expected: PASS.

- [ ] **Step 6: Add the CI job**

  A `web` job in `ci.yml`: `actions/setup-node` (node 24), `npm ci`,
  `npm test`, `npx tsc --noEmit`, with working-directory `web`.

- [ ] **Step 7: Commit**

  `git commit -m "Scaffold the web app with tested resampling and buffering"`

---

### Task 13: Audio I/O and the three pages, first real interview

**Files:**
- Create: `web/src/audio/capture.worklet.ts`,
  `web/src/audio/playback.worklet.ts`
- Create: `web/src/audio/mic.ts`, `web/src/audio/player.ts`
- Create: `web/src/api.ts`
- Create: `web/src/pages/Landing.tsx`, `Interview.tsx`, `Report.tsx`
- Modify: `web/src/App.tsx`, `web/src/main.tsx`, `web/src/styles.css`
- Modify: `web/vite.config.ts` (dev proxy `/api`, `/ws` to
  `localhost:8000`)

**Interfaces:**
- Consumes: Task 12 helpers, and the Task 11 routes and wire protocol.
- Produces:
  - `startMic(onFrame: (pcm: Int16Array) => void): Promise<{ stop(): void
    }>`.
    - Uses `getUserMedia({audio: {echoCancellation: true,
      noiseSuppression: true, autoGainControl: true, channelCount: 1}})`.
    - Uses an `AudioContext` at the device rate. The capture worklet
      resamples to 16 kHz and posts 320-sample `Int16Array` frames.
  - `createPlayer(): Promise<{ push(pcm: Int16Array): void; flush(): void;
    close(): void }>`. The playback worklet holds a `FloatRing` of 2 s at
    the context rate and resamples from 16 kHz. `flush()` clears it
    immediately.
  - `startInterview(): Promise<{interview_id: string, ws_path: string,
    cap_s: number} | {error: "visitor_limit" | "daily_budget"}>`
  - `getReport(id): Promise<{status: "grading"} | Report>`
  - Worklets load with `import url from "./capture.worklet.ts?worker&url"`
    and `ctx.audioWorklet.addModule(url)`.
  - Routing is hash-based: `#/` for Landing, `#/interview` for Interview,
    `#/report/{id}` for Report.

**Pages:**
- **Landing.**
  - One sentence on what it is.
  - A note to use headphones.
  - A consent checkbox with this exact copy: "Your voice is sent to Vapi
    and Claude to run and grade this interview. Nothing is shared
    publicly."
  - A Start button, disabled until consent is checked.
  - The limit messages: "You've used today's demo. Come back tomorrow." and
    "The demo is resting for today. Come back tomorrow."
  - Phones (`isPhone`) see "Mockingbird needs a laptop or desktop browser
    with a mic. Open this link there." in place of Start.
- **Interview.**
  - Mic permission is requested before calling `startInterview`, so a
    denied mic never creates an interview.
  - Opens a WebSocket to `ws_path`. Binary frames go to the player. Text
    goes through `parseServerMessage`:
    - `flush` calls `player.flush()`
    - `ended` routes to the report
  - Shows a countdown from `cap_s` and an End button, which sends `{"type":
    "end"}`.
- **Report.**
  - Polls `getReport` every 2 s until it isn't grading.
  - Shows each question, with each item's score or "Not enough evidence",
    the rationale, and the quotes.
  - Statuses `no_answers` and `grading_failed` each get one plain sentence.

- [ ] **Step 1: Implement the worklets, `mic.ts`, `player.ts`, `api.ts` and
  the pages**

- [ ] **Step 2: Run the whole stack locally**

  Run, each in its own shell:
  - `ngrok http 8000`
  - `PUBLIC_BASE_URL=<ngrok url> uv run uvicorn mockingbird.app:app
    --factory --port 8000`
  - `cd web && npm run dev`

  Then open `http://localhost:5173` in desktop Chrome.

  Expected:
  - The interviewer speaks the opening line.
  - You answer and get a follow-up.
  - Talking over the interviewer stops its audio within about half a
    second.
  - The call ends after the wrap-up line.
  - The report page shows scores with quotes you actually said.

  Repeat once in Firefox and once in Safari.

- [ ] **Step 3: Run the tests**

  Run: `cd web && npm test && npx tsc --noEmit && cd .. && uv run pytest`
  Expected: PASS.

- [ ] **Step 4: Commit**

  `git commit -m "Add mic capture, playback and the interview pages"`

  The PR walkthrough includes a 1-minute screen recording of the local run.

---

### Task 14: Docker, Terraform, deploy script, keep-alive

**Files:**
- Create: `Dockerfile`
- Create: `.dockerignore`
- Create: `infra/main.tf`, `variables.tf`, `outputs.tf`, `network.tf`,
  `ecr.tf`, `ecs.tf`, `alb.tf`, `cdn.tf`, `secrets.tf`, `iam.tf`,
  `budgets.tf`
- Create: `scripts/deploy.sh`
- Create: `.github/workflows/keepalive.yml`
- Modify: `.github/workflows/ci.yml` (add the `terraform` job)

**Interfaces:**
- Consumes: `create_app` (Task 2) and `migrate.py` (Task 7).
- Produces:
  - Terraform outputs: `cloudfront_url`, `ecr_repo_url`, `ecs_cluster`,
    `ecs_service`, `web_bucket`, `distribution_id`.

**Docker:**
- Base image `python:3.12-slim`, built for `linux/arm64`.
- Installs with `uv sync --frozen --no-dev`.
- `CMD ["uv", "run", "uvicorn", "mockingbird.app:app", "--factory",
  "--host", "0.0.0.0", "--port", "8000"]`.
- Migrations run on startup through `migrate.py` when `DATABASE_URL` is
  set.

**Terraform:**
- Variables: `region` (default `us-east-1`), `alert_email`,
  `monthly_budget_usd` (default 50, which is credit burn, not cash).
- `network.tf`: a VPC with `10.20.0.0/16`, two public subnets in two
  availability zones, an internet gateway, and no NAT.
- `alb.tf`:
  - The ALB with `idle_timeout = 300`.
  - Its security group allows TCP 80 only from the managed prefix list
    `com.amazonaws.global.cloudfront.origin-facing`.
  - Target group: HTTP 8000, health check `/healthz`, deregistration delay
    30 s.
  - Listener: HTTP 80.
- `ecs.tf`:
  - A Fargate task with `ARM64`, CPU 512, memory 1024.
  - `desired_count = 1`, public IP on.
  - Logs go to a CloudWatch group with 14-day retention.
  - The task's security group allows 8000 only from the ALB's security
    group.
- `cdn.tf`:
  - A private S3 bucket with origin access control.
  - A CloudFront distribution whose default behavior serves S3 with
    `CachingOptimized`.
  - Ordered behaviors for `/api/*`, `/ws/*`, `/vapi/*` and `/healthz`: ALB
    origin, `http-only`, all methods, `CachingDisabled`, origin request
    policy `AllViewerAndCloudFrontHeaders-2022-06`.
  - `default_root_object = "index.html"`.
- `secrets.tf`:
  - SSM SecureString parameters under `/mockingbird/`: `ANTHROPIC_API_KEY`,
    `VAPI_API_KEY`, `DATABASE_URL`, `BRAIN_SECRET`, `WEBHOOK_SECRET`,
    `OWNER_KEY`, `VISITOR_SALT`.
  - Each is created with the value `"unset"` and `lifecycle {
    ignore_changes = [value] }`, so real secrets never enter Terraform
    state.
  - Values are set with `aws ssm put-parameter --overwrite`.
- The task definition passes those parameters as `secrets`.
  `PUBLIC_BASE_URL` is a plain env var set to the CloudFront URL.
- `budgets.tf`: a monthly cost budget with an email alert at 80% to
  `alert_email`.
- State is local in phase 1 (`infra/terraform.tfstate`, gitignored).

**`scripts/deploy.sh`:**
1. `docker buildx build --platform linux/arm64`, then push to the ECR repo
   from the outputs.
2. `aws ecs update-service --force-new-deployment`.
3. `npm --prefix web run build`.
4. `aws s3 sync web/dist s3://<bucket> --delete`.
5. `aws cloudfront create-invalidation --paths "/*"`.

**`keepalive.yml`:** a daily cron at `0 12 * * *` that runs `curl -fsS
"$MB_URL/healthz?db=1"`. `MB_URL` is a repository variable.

**CI `terraform` job:** `hashicorp/setup-terraform`, `terraform fmt -check`,
`terraform init -backend=false`, `terraform validate`, with working
directory `infra`.

- [ ] **Step 1: Validate the infrastructure locally**

  Write the Dockerfile and the Terraform. Then run: `docker buildx build
  --platform linux/arm64 -t mockingbird:test . && cd infra && terraform
  init && terraform validate && terraform plan -var
  alert_email=<email>`.

  Expected: the image builds, `validate` reports "Success", and `plan`
  shows only creates.

- [ ] **Step 2: Apply, set secrets, deploy (Ruhan approves the apply)**

  Run, in order:
  1. `terraform apply`
  2. `aws ssm put-parameter --overwrite` for each parameter
  3. `scripts/deploy.sh`

  Expected: `curl -fsS <cloudfront_url>/healthz?db=1` returns `{"ok":true}`.

- [ ] **Step 3: Run a real interview on the deployed app**

  Run the same check as Task 13 Step 2, at `cloudfront_url`.

  Expected:
  - The opening line plays.
  - An interruption stops the audio.
  - The report renders.
  - A second demo from the same browser shows the used-today message.

  Then start one with the mic muted and say nothing. The call ends on
  silence after about 30 s (`silenceTimeoutSeconds`), and the report says
  no answer was recorded.

- [ ] **Step 4: Write `README.md`**

  Include:
  - the pitch line
  - the live link
  - an architecture diagram
  - how to run it locally (the Task 13 Step 2 commands)
  - "What is not built yet", pointing at phases 2 to 5

  No numbers that no command produces.

- [ ] **Step 5: Commit**

  `git commit -m "Deploy to ECS Fargate behind CloudFront with Terraform"`

  The PR walkthrough includes the CloudFront URL, the `terraform plan`
  summary, and the costs that draw on credits, with their sources.

---

## After phase 1

Write the phase 2 plan, which covers recording, interruptions done properly
and latency tracing. It starts from what `docs/vapi-contract.md` found.
