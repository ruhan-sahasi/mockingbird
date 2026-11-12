# Mockingbird Phase 2 (Recording and Latency) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Record both sides of every interview and measure, per interviewer
turn:
- the latency from the moment the candidate stops talking to the moment the
  interviewer is audible, broken down by hop
- the early cut-in rate
- how long an interruption takes to silence the interviewer

Show all three on a stats page.

**Architecture:**
- The relay gains a synchronous audio tap. A `CallRecorder` keeps both
  16 kHz tracks in memory and uploads them as WAV to Supabase Storage, or a
  local folder in development, after the call.
- A `CallTracer` per call stamps each hop on the server's monotonic clock:
  Vapi's user-stopped event, the brain request, Claude's first token, the
  first chunk sent to Vapi, Vapi's assistant-started event, and the first
  audible interviewer frame.
- The browser stamps the two ends the candidate actually experiences. The
  capture worklet detects end of speech with an energy threshold. The
  playback worklet reports when the interviewer becomes audible. The browser
  sends both stamps over the existing socket.
- Traces land in a new Postgres table, and `/api/stats` and `#/stats` report
  p50 and p95.

**Tech Stack:** Python 3.12, FastAPI, httpx, asyncpg, pytest. React, TypeScript,
AudioWorklet, Vitest. Supabase Storage REST API. Terraform.

**Spec:** `docs/superpowers/specs/2026-10-19-mockingbird-design.md`. Read the
"Latency: definition, budget, measurement", "Transcripts and grading" and
"Demo access, cost, and privacy" sections, and the decisions table (phase 2
rows). Phase 1's plan, including its Amendment 1, describes the code this
plan extends: `docs/superpowers/plans/2026-10-19-phase-1-thin-slice.md`.

## Global Constraints

- **Ruhan's phase 2 decisions:**
  - Stop-of-speech is an energy threshold in the browser capture worklet.
  - Traces go in a Postgres table plus a stats page.
  - Recordings are WAV 16 kHz s16le mono in Supabase Storage.
  - Browser ducking is NOT built. This phase only measures the
    interrupt-to-silence delay, and the stats page says to consider ducking
    if p95 is above 300 ms.
- **The headline metric:** time from the candidate's end of speech to the
  first audible interviewer sample, both stamped in the browser on
  `performance.now()`. Server-side hops break it down, but never replace it.
- **Cut-in proxy for live calls:** a Vapi `user-interrupted` event within
  1500 ms of the assistant-started event of the same turn marks that turn
  `cut_in = true`.
- **Recording paths:** `interviews/{interview_id}/candidate.wav` and
  `interviews/{interview_id}/interviewer.wav`. The Supabase bucket is named
  `recordings` and is private (`public: false`). Never create a public URL.
- **Retention:** recordings are deleted together with the demo interview's
  rows after 7 days (`RETENTION_DAYS`). If a storage delete fails, the row
  stays so the next hourly purge retries, so audio is never orphaned.
- **Consent copy on the Landing page:** "Your voice is recorded and sent to
  Vapi and Claude to run and grade this interview. Recordings are deleted
  after 7 days. Nothing is shared publicly."
- **Ruling R19 carries over:** the Vapi call id, socket URLs, one-time
  tokens and every key never reach logs, error text or the browser. The
  Supabase service key is a secret of the same kind.
- **Callback safety:** relay callbacks (`on_audio`, `on_browser_message`)
  are synchronous and must never block or raise into the pumps. Wrap them
  and log only the exception type.
- **Migrations:** new files `002_recordings.sql` and
  `003_latency_traces.sql`. Every new table enables row level security with
  no policies, like `001_init.sql`.
- **Commits and PRs:**
  - Every commit has an imperative subject and a prose body, and no AI
    attribution of any kind.
  - Each task is one PR, stacked on the previous one, with the four-part
    walkthrough: What changed / Why it is built this way / How to explain it
    in an interview / How to check it.
- **Copy:** README and UI copy are plain and specific, with no em or en
  dashes and no apostrophes.

## Review Focus

1. **A recording upload fails** (Supabase down, timeout). The report is
   still built, cleanup still finishes, the failure is logged by type only,
   and the interview's `recording` field says `upload_failed`. Pinned in
   Task 3.
2. **A browser sends a malformed, huge, negative or flood of timing
   messages.** They are ignored without disturbing the relay, and values
   outside 0 to 60000 ms are dropped. Pinned in Task 5.
3. **A turn has no measurable start,** such as the opening line or a reply
   with no user-stopped stamp. It is excluded from latency percentiles,
   never counted as 0 ms. Pinned in Tasks 4 and 6.
4. **A 20-minute full-mode call** stays inside the recorder's memory cap.
   Frames past the cap are dropped and counted, never grown without bound.
   Pinned in Task 1.
5. **A storage delete fails during the 7-day purge.** The interview row
   stays for the next purge, so no recording is left without its row.
   Pinned in Task 3.

---

## File Structure

```
server/mockingbird/recording.py        TrackRecorder, CallRecorder, wav_bytes()
server/mockingbird/audio_store.py      AudioStore protocol, LocalAudioStore, SupabaseAudioStore
server/mockingbird/tracing.py          TurnTrace, CallTracer
server/mockingbird/stats.py            percentile(), summarize_traces()
server/migrations/002_recordings.sql   interviews.recording jsonb
server/migrations/003_latency_traces.sql  latency_traces table
server/mockingbird/relay.py            + on_audio, on_browser_message callbacks
server/mockingbird/routes_ws.py        wire recorder + tracer into a call; upload after the call
server/mockingbird/routes_vapi.py      stamp request, first token, first chunk on the draft's trace
server/mockingbird/brain.py            draft(..., trace=None); trace gets the committed turn's idx
server/mockingbird/store.py            + add_latency_traces, list_latency_traces, recording field
server/mockingbird/sessions.py         purge deletes recordings before rows
server/mockingbird/routes_api.py       + GET /api/stats
web/src/audio/capture.worklet.ts       tagged messages + energy VAD
web/src/audio/vad.ts                   EnergyVad (pure, tested)
web/src/audio/mic.ts                   startMic(onFrame, onVad)
web/src/timing.ts                      TurnTimer (pure, tested)
web/src/interviewSession.ts            send {"type":"timing", ...}
web/src/pages/Stats.tsx                the stats page
infra/secrets.tf, infra/ecs.tf, scripts/entrypoint.sh, .env.example   Supabase storage settings
```

---

### Task 1: Recorder and the relay audio tap

**Files:**
- Create: `server/mockingbird/recording.py`
- Modify: `server/mockingbird/relay.py`
- Test: `server/tests/test_recording.py`, `server/tests/test_relay.py`

**Interfaces:**
- Produces:
  - `wav_bytes(pcm: bytes, sample_rate: int = 16000) -> bytes`: a
    canonical 44-byte RIFF header plus the data, PCM s16le mono.
  - `class TrackRecorder(max_frames: int)`:
    - `append(frame: bytes, at_ms: float) -> None`
    - `pcm() -> bytes`
    - `first_frame_ms: float | None`
    - `frames: int`
    - `dropped: int`
  - `class CallRecorder(clock: Callable[[], float], cap_s: int)`:
    - Holds `candidate` and `interviewer` `TrackRecorder`s with
      `max_frames = (cap_s + 60) * 50`.
    - `start() -> None` sets t0.
    - `on_audio(direction: Literal["candidate", "interviewer"], frame: bytes)
      -> None` appends to the matching track, with `at_ms = (clock() - t0) *
      1000`.
    - `metadata() -> dict` returns
      `{"candidate": {"frames", "dropped", "first_frame_ms"}, "interviewer":
      {...}}`.
  - Relay: new keyword
    `on_audio: Callable[[str, bytes], None] | None = None`.
    - It is called with `("candidate", frame)` right before each mic frame
      goes to Vapi.
    - It is called with `("interviewer", frame)` right before each Vapi frame
      goes to the browser.
    - It is synchronous. An exception it raises is logged by type and
      swallowed.

- [ ] **Step 1: Write the failing tests**

  ```python
  def test_wav_bytes_header():
      out = wav_bytes(b"\x01\x00" * 160)
      assert out[:4] == b"RIFF" and out[8:16] == b"WAVEfmt "
      assert int.from_bytes(out[24:28], "little") == 16000      # sample rate
      assert int.from_bytes(out[22:24], "little") == 1          # mono
      assert int.from_bytes(out[34:36], "little") == 16         # bits
      assert int.from_bytes(out[40:44], "little") == 320        # data bytes
      assert len(out) == 44 + 320

  def test_track_recorder_caps_frames_and_counts_drops():
      t = TrackRecorder(max_frames=2)
      for i in range(5): t.append(b"\x00" * 640, at_ms=i * 20)
      assert t.frames == 2 and t.dropped == 3 and len(t.pcm()) == 1280
      assert t.first_frame_ms == 0

  def test_call_recorder_routes_directions_and_stamps_first_frame(): ...
      # fake clock; start() at 10.0; on_audio("interviewer", f) at 10.25 -> interviewer.first_frame_ms == 250

  def test_full_mode_cap_fits_twenty_minutes():  # Review Focus 4
      r = CallRecorder(clock=lambda: 0.0, cap_s=1200)
      assert r.candidate.max_frames == (1200 + 60) * 50
  ```

  In `test_relay.py`, using the existing `make_relay` and `FakeSocket`
  helpers:
  - `test_on_audio_taps_both_directions_in_order`: mic frames tap as
    "candidate" before Vapi receives them, and Vapi frames tap as
    "interviewer" before the browser receives them.
  - `test_on_audio_exception_does_not_stop_relaying`: frames still flow,
    and the log names the exception type only.

- [ ] **Step 2: Run them**

  Run: `uv run pytest -q server/tests/test_recording.py server/tests/test_relay.py`
  Expected: FAIL.

- [ ] **Step 3: Implement `recording.py`, and add `on_audio` to `Relay`**

  Add it at relay.py `_browser_to_vapi` before `self._vapi.send_bytes`, and
  at `_vapi_to_browser` before `self._send_browser`.

- [ ] **Step 4: Run them**

  Run: `uv run pytest -q`
  Expected: PASS.

- [ ] **Step 5: Commit**

  `git commit -m "Record both audio tracks as the relay passes them"`

---

### Task 2: Audio storage (local folder or Supabase Storage)

**Files:**
- Create: `server/mockingbird/audio_store.py`
- Modify: `server/mockingbird/config.py`, `server/mockingbird/app.py`,
  `.env.example`, `.gitignore`
- Test: `server/tests/test_audio_store.py`

**Interfaces:**
- Produces:
  - `AudioStore` (Protocol, async):
    - `put_wav(path: str, data: bytes) -> None`
    - `delete_prefix(prefix: str) -> None`
    - `ensure_ready() -> None`
  - `LocalAudioStore(root: Path)`:
    - Writes under `root/path`, creating parents.
    - `delete_prefix` removes `root/prefix` recursively.
    - It refuses any path that escapes `root` (`..`, absolute paths) with
      `ValueError`.
  - `SupabaseAudioStore(url: str, service_key: str, bucket: str, http:
    httpx.AsyncClient)`:
    - `put_wav`:
      `POST {url}/storage/v1/object/{bucket}/{path}` with headers
      `Authorization: Bearer <key>`, `Content-Type: audio/wav`,
      `x-upsert: true`.
    - `delete_prefix`: list the objects under the prefix with
      `POST {url}/storage/v1/object/list/{bucket}`, body
      `{"prefix": prefix, "limit": 100}`. Then delete them with
      `DELETE {url}/storage/v1/object/{bucket}`, body
      `{"prefixes": ["<prefix><name>", ...]}`.
    - `ensure_ready`: `POST {url}/storage/v1/bucket`, body
      `{"id": bucket, "name": bucket, "public": false}`. A 200 or a 409
      already-exists response both count as success.
    - Non-2xx responses raise `AudioStoreError(status)`. The message holds
      the status only, never the key, the URL or the body.
  - New `Settings` fields:
    - `supabase_url: str | None = None`
    - `supabase_service_key: str | None = None`
    - `recordings_bucket: str = "recordings"`
    - `recordings_dir: str = "recordings"`
  - `create_app` gains `audio_store: AudioStore | None = None`.
    - When not injected:
      - `SupabaseAudioStore` if both Supabase settings are set.
      - Otherwise `LocalAudioStore(Path(settings.recordings_dir))`, with
        one warning log line.
    - `ensure_ready()` runs in the lifespan, and `state.audio_store` holds
      the store.

- [ ] **Step 1: Write the failing tests**
  - Local store:
    - round-trip
    - `delete_prefix` removes only that interview's folder
    - path traversal (`"../x"`, `"/etc/x"`) raises `ValueError`
  - Supabase store, mocked with `respx` (the app uses plain `httpx` here):
    - each exact URL, method, header and body above
    - `ensure_ready` treats 409 as ok
    - a 500 raises `AudioStoreError` whose `str()` contains neither the key
      nor `/storage/v1`
    - the bucket body has `"public": False`
  - Settings: the new fields and their defaults, and that
    `supabase_service_key` never appears in `repr(Settings(...))`. Use
    `SecretStr` or `repr=False`, whichever matches how config.py hides the
    other secrets.
  - App factory: no Supabase settings gives a `LocalAudioStore`; both set
    gives a `SupabaseAudioStore`.

- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement the store, settings and wiring**
  Implement `audio_store.py`, the settings, and the app wiring. Add
  `recordings/` to `.gitignore`, and the three new names to `.env.example`
  with comments.
- [ ] **Step 4: Run them.** Run `uv run pytest -q` and `uv run ruff check .`.
  Expected: PASS.
- [ ] **Step 5: Commit**

  `git commit -m "Store recordings in a private Supabase bucket or a local folder"`

---

### Task 3: Record every call, upload after it, delete with the rows

**Files:**
- Create: `server/migrations/002_recordings.sql`
- Modify:
  - `server/mockingbird/models.py` (`Interview.recording: dict | None = None`)
  - `server/mockingbird/store.py`
  - `server/mockingbird/routes_ws.py`
  - `server/mockingbird/sessions.py`
  - `web/src/pages/Landing.tsx` (consent copy)
  - `infra/secrets.tf`, `infra/ecs.tf`, `scripts/entrypoint.sh`
- Test: `server/tests/test_routes.py`, `server/tests/test_sessions_purge.py`
  (or the existing purge tests), `server/tests/test_store.py`, the Landing
  test, and `terraform validate`

**Interfaces:**
- Consumes: `CallRecorder` (Task 1), and `AudioStore` /
  `state.audio_store` (Task 2).
- Produces:
  - `002_recordings.sql`: `alter table interviews add column recording jsonb`.
  - `Interview.recording`, updatable through `update_interview`.
  - `upload_recording(store: Store, audio: AudioStore, interview_id: str,
    recorder: CallRecorder) -> None` in `recording.py`:
    - It puts both WAVs at the Global Constraints paths.
    - Then it sets `recording = {"status": "uploaded", **metadata}`.
    - On any exception it sets `{"status": "upload_failed"}`, logs the
      exception type only, and never raises.
  - `purge_once(store, vapi, audio)` takes the audio store. For each demo
    interview older than 7 days it calls
    `audio.delete_prefix(f"interviews/{id}/")` before deleting rows.
    Interviews whose storage delete failed are kept for the next purge, the
    same way failed Vapi deletes are handled.
  - `run_sweeper(...)` gains an `audio: AudioStore` parameter, passed on to
    `purge_once`, and the app's lifespan passes `state.audio_store`.

- [ ] **Step 1: Write the failing tests**
  - `test_a_call_records_and_uploads_both_tracks`:
    - Use the existing route-test fakes (`FakeVapi`, `FakeBrowserSocket`),
      plus a fake `AudioStore` that records `put_wav` calls.
    - Run a short call that sends 5 mic frames and 3 Vapi frames.
    - Assert both paths were uploaded and that `candidate.wav` decodes to
      5 * 640 bytes of data.
    - Assert `interview.recording["status"] == "uploaded"`.
  - `test_upload_failure_still_builds_the_report` (Review Focus 1): the fake
    store raises. The report is still saved,
    `recording == {"status": "upload_failed"}`, and caplog holds no
    exception message.
  - `test_purge_deletes_recordings_before_rows` and
    `test_purge_keeps_rows_when_storage_delete_fails` (Review Focus 5).
  - Store: the `recording` field round-trips in `MemoryStore`, and in
    `PostgresStore` when `TEST_DATABASE_URL` is set.
  - Landing: the consent label equals the Global Constraints copy exactly.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement**

  Wiring and placement:
  - In `_run_call`, build `CallRecorder(time.monotonic, interview.cap_s)`,
    call `start()` when the call goes live, and pass `on_audio=recorder.on_audio`
    to `Relay`.
  - In `_clean_up`, schedule `upload_recording` with `_in_background`,
    before or alongside `build_report`. It must not delay the report.
  - Put the migration in `002_recordings.sql`.

  Infra:
  - Add an SSM SecureString `SUPABASE_SERVICE_KEY` (with the same
    `value_wo` pattern as the others) and a plain environment variable
    `SUPABASE_URL` (a Terraform variable).
  - Add `SUPABASE_SERVICE_KEY` to the entrypoint's placeholder check.

- [ ] **Step 4: Run all checks**

  Run, and expect every one to pass:
  - `uv run pytest -q`
  - `uv run ruff check .`
  - `cd web && npm test && npx tsc --noEmit`
  - `terraform -chdir=infra init -backend=false && terraform -chdir=infra validate`,
    then delete `infra/.terraform`
- [ ] **Step 5: Commit**

  `git commit -m "Record every call, upload it after the call, and delete it with its rows"`

---

### Task 4: Server-side latency tracing

**Files:**
- Create: `server/mockingbird/tracing.py`,
  `server/migrations/003_latency_traces.sql`
- Modify:
  - `server/mockingbird/brain.py`
  - `server/mockingbird/routes_vapi.py`
  - `server/mockingbird/routes_ws.py`
  - `server/mockingbird/store.py`
- Test: `server/tests/test_tracing.py`, `server/tests/test_brain.py`,
  `server/tests/test_routes.py`, `server/tests/test_store.py`

**Interfaces:**
- `TurnTrace` (dataclass, every time in ms since the call went live, `None`
  until stamped):
  - `turn_idx: int | None`
  - `user_stopped_ms`, `request_ms`, `first_token_ms`, `first_chunk_ms`,
    `assistant_started_ms`, `first_audio_ms`, `browser_stop_to_audible_ms`,
    `interrupt_to_silence_ms`
  - `cut_in: bool = False`
- `CallTracer(clock: Callable[[], float])`:
  - `start()`
  - `new_trace() -> TurnTrace`: stamps `request_ms`, and copies the latest
    unconsumed `user_stopped_ms`.
  - `user_stopped()`
  - `first_token(t)`, `first_chunk(t)`
  - `assistant_started(t: TurnTrace | None)`
    - With `None`, as for the opening line, it creates an untimed trace so
      later events attach somewhere and nothing is mis-attributed.
  - `on_audio(direction, frame)`:
    - Only "interviewer" frames count.
    - The first frame whose RMS is at or above 300 on the int16 scale,
      after the current turn's `assistant_started_ms`, sets `first_audio_ms`.
  - `user_interrupted()`: sets `cut_in` if within 1500 ms of the current
    turn's `assistant_started_ms`.
  - `browser_timing(msg: dict)` (Task 5 calls this)
  - `committed() -> list[TurnTrace]`: traces with a `turn_idx`.
- Brain changes:
  - `draft(candidate_text, spoken_assistant_text, trace: TurnTrace | None = None)`
    stores the trace on `_Draft`.
  - When a committed draft becomes an interviewer `Turn`, set
    `trace.turn_idx = turn.idx`.
  - `commit_spoken()` keeps returning `bool`.
  - New property `Brain.committing_trace -> TurnTrace | None`: the trace of
    the draft the last successful `commit_spoken()` adopted.
- Routes:
  - `routes_vapi._draft` calls `tracer.new_trace()` when the request
    arrives, passes it to `brain.draft`, calls `first_token` on the first
    yielded chunk, and calls `first_chunk` where `_sse` emits its first
    chunk.
  - `routes_ws._live_events`:
    - `SpeechUpdate(user, stopped)` calls `tracer.user_stopped()`.
    - On `SpeechUpdate(assistant, started)`, call `brain.commit_spoken()`,
      then `tracer.assistant_started(brain.committing_trace if committed
      else None)`.
    - `UserInterrupted` calls `tracer.user_interrupted()`.
  - The relay's `on_audio` fans out to both the recorder and the tracer.
  - `LiveSession` gains `tracer: CallTracer`.
- `003_latency_traces.sql`:
  - Table `latency_traces(interview_id text references interviews on delete
    cascade, turn_idx int, user_stopped_ms, request_ms, first_token_ms,
    first_chunk_ms, assistant_started_ms, first_audio_ms,
    browser_stop_to_audible_ms, interrupt_to_silence_ms` (all `double
    precision null`), `cut_in boolean not null default false, created_at
    timestamptz not null default now(), primary key (interview_id,
    turn_idx))`.
  - Enable row level security.
- Store:
  - `add_latency_traces(interview_id, traces: list[TurnTrace]) -> None`
  - `list_latency_traces(since: datetime) -> list[TurnTrace]`
  - On both `MemoryStore` and `PostgresStore`.
  - `_clean_up` writes `tracer.committed()` after `finalize()`.

- [ ] **Step 1: Write the failing tests**
  - Tracer, with a fake clock:
    - each stamp lands in ms since `start()`
    - `first_audio_ms` ignores frames before `assistant_started`, and
      silent frames
    - cut-in at 1499 ms is true and at 1501 ms is false
    - the opening line's untimed trace never appears in `committed()`
      (Review Focus 3)
    - `user_stopped` is consumed by the next `new_trace`, never by two
  - Brain: the committed trace gets the interviewer turn's idx, and a
    dropped draft's trace never gets one.
  - Routes, end to end with the fakes: the stored traces have
    `request_ms <= first_token_ms <= first_chunk_ms`, and
    `assistant_started_ms` is set.
  - Store round-trip, with Postgres when `TEST_DATABASE_URL` is set.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement as specified.**
- [ ] **Step 4: Run them.** `uv run pytest -q` and `uv run ruff check .`.
  Expected: PASS.
- [ ] **Step 5: Commit**

  `git commit -m "Trace each interviewer turn hop by hop on the server"`

---

### Task 5: Browser timing (end of speech and interrupt to silence)

**Files:**
- Create: `web/src/audio/vad.ts`, `web/src/timing.ts`
- Modify:
  - `web/src/audio/capture.worklet.ts`
  - `web/src/audio/mic.ts`
  - `web/src/interviewSession.ts`
  - `web/src/protocol.ts`
  - `server/mockingbird/relay.py`
  - `server/mockingbird/routes_ws.py`
- Test: `web/src/audio/vad.test.ts`, `web/src/timing.test.ts`,
  `web/src/interviewSession.test.ts`, `web/src/audio/capture.worklet.test.ts`,
  `server/tests/test_relay.py`, `server/tests/test_tracing.py`

**Interfaces:**
- `EnergyVad`:
  - Constructor:
    `{ speechRms = 0.02, startFrames = 3, quietMs = 300, frameMs = 20 }`.
  - `push(frame: Float32Array): { speaking: boolean, quietMs?: number } | null`
    returns an event only on a state change.
  - Speaking starts after `startFrames` consecutive frames at or above
    `speechRms`.
  - Stopped fires after `quietMs` of frames below it, and carries
    `quietMs` so the caller can backdate.
- Capture worklet messages become tagged:
  `{ type: 'frame', pcm: Int16Array }` and
  `{ type: 'vad', speaking: boolean, quietMs?: number }`.
- `startMic(onFrame, onVad?: (e: { speaking: boolean, quietMs?: number }) => void)`.
- `TurnTimer`:
  - Constructor: `({ now: () => number, outputLatencyMs: () => number })`.
  - `vad(e)`:
    - stopped: `stoppedAt = now() - quietMs`
    - speaking while the interviewer is audible: `interruptAt = now()`
  - `interviewerAudible(audible: boolean)`:
    - On true, returns `{ stop_to_audible_ms }` when a `stoppedAt` is
      pending, with `audibleAt = now() + outputLatencyMs()`, then clears
      it.
  - `flushed()`, then the next `interviewerAudible(false)`, returns
    `{ interrupt_to_silence_ms }` when `interruptAt` is set.
- Client message: `{"type": "timing", "stop_to_audible_ms"?: number,
  "interrupt_to_silence_ms"?: number}`, built by `timingMessage(...)` in
  `protocol.ts`.
- `interviewSession`:
  - Feeds the capture VAD events and the player's `onSpeaking` into the
    `TurnTimer`, and the server's `flush` into `timer.flushed()`.
  - Sends each result over the socket as a text frame.
  - Gets output latency from the player's `AudioContext`
    (`outputLatency || baseLatency || 0`, in ms).
- Relay: new keyword
  `on_browser_message: Callable[[dict], None] | None = None`.
  - It receives any browser text frame that parses as a JSON object with
    `type == "timing"`.
  - `end` keeps its current handling. Everything else is still dropped at
    debug.
  - At most 10 timing messages per second are passed on; the rest are
    dropped.
- `CallTracer.browser_timing(msg)`:
  - Accepts only finite numbers in 0 to 60000 for the two keys.
  - Attaches them to the current turn's trace.
  - Ignores everything else (Review Focus 2).

- [ ] **Step 1: Write the failing tests**
  - `vad.test.ts`, with synthetic frames:
    - silence, then 3 loud frames, gives speaking
    - 300 ms quiet gives stopped with `quietMs: 300`
    - 2 loud frames then quiet gives no event
  - `timing.test.ts`, with a fake clock:
    - stopped at t=1000 with quietMs 300, then audible at t=2100 with
      outputLatency 20, gives `stop_to_audible_ms` 1420
    - an interrupt at 5000, then flushed, then silent at 5180, gives
      `interrupt_to_silence_ms` 180
    - audible with no pending stop gives nothing
  - `capture.worklet.test.ts`: frames are tagged `frame`, and a VAD change
    posts a `vad` message.
  - `interviewSession.test.ts`: the socket receives a timing text frame
    after the scripted VAD and playback events.
  - Server:
    - the relay passes timing messages to `on_browser_message` and drops
      non-JSON and other types
    - more than 10 per second are cut
    - the tracer ignores `-5`, `1e9`, `"300"`, `NaN` and missing keys
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement as specified**

  Update `mic.ts` for the tagged messages. The Task 9 rule from phase 1
  still holds: mic frames never stop.
- [ ] **Step 4: Run all checks**

  Run, and expect every one to pass:
  - `cd web && npm test && npx tsc --noEmit && npm run lint && npm run build`
  - `uv run pytest -q`
  - `uv run ruff check .`
- [ ] **Step 5: Commit**

  `git commit -m "Measure end of speech and interrupt to silence in the browser"`

---

### Task 6: Stats endpoint, stats page, smoke call and README

**Files:**
- Create: `server/mockingbird/stats.py`, `web/src/pages/Stats.tsx`
- Modify:
  - `server/mockingbird/routes_api.py`
  - `web/src/App.tsx` (or `route.ts`) for `#/stats`
  - `web/src/api.ts`
  - `scripts/smoke_call.py`
  - `README.md`
- Test: `server/tests/test_stats.py`, `server/tests/test_routes.py`,
  `web/src/pages/Stats.test.tsx`, `server/tests/test_smoke_call.py`

**Interfaces:**
- `percentile(values: list[float], p: float) -> float | None`
  - Nearest-rank on the sorted values.
  - `None` for an empty list.
- `summarize_traces(traces: list[TurnTrace]) -> dict` returns:

  ```
  {"turns": n,
   "stop_to_audible_ms": {"p50", "p95", "n"},
   "hops_ms": {"request_to_first_token", "first_token_to_first_chunk",
               "first_chunk_to_assistant_started",
               "assistant_started_to_first_audio"},   # medians
   "cut_in_rate": cut_in_turns / turns or None,
   "interrupt_to_silence_ms": {"p50", "p95", "n"},
   "consider_ducking": p95 of interrupt_to_silence > 300 (False when no data)}
  ```

  - Every percentile skips `None` values (Review Focus 3).
- `GET /api/stats?days=7`: returns `summarize_traces` over traces from the
  last `days` days (1 to 30, default 7). Public, with no ids and no
  per-interview data.
- `#/stats` page:
  - Shows the numbers in a plain table, and the `consider_ducking` hint as
    one sentence.
  - With no data, shows "No measured turns yet."
- The smoke call fetches `/api/stats?days=1` after the report and prints
  the stop-to-audible p50 and n.
- README:
  - A "Latency" section explaining what is measured and where (browser ends,
    server hops).
  - How to read it: `curl http://localhost:8000/api/stats`.
  - The honest current state, in plain words.

- [ ] **Step 1: Write the failing tests**
  - `percentile([], 50) is None`
  - `percentile([1, 2, 3, 4], 50) == 2`
  - `percentile([1, 2, 3, 4], 95) == 4`
  - `summarize_traces`:
    - skips `None` stop-to-audible values
    - computes `cut_in_rate`
    - `consider_ducking` is true when p95 is 301 and false when it is 300
  - Route: `days=0` or `days=31` gives 422. The response has no
    `interview_id`.
  - Stats page: renders the numbers, and the empty state.
  - Smoke call: the summary line includes the stats.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement as specified.**
- [ ] **Step 4: Run all checks**

  Run, and expect every one to pass:
  - `uv run pytest -q`
  - `uv run ruff check .`
  - `cd web && npm test && npx tsc --noEmit && npm run lint && npm run build`
- [ ] **Step 5: Commit**

  `git commit -m "Report latency, cut-in and interrupt-to-silence on a stats page"`

---

## After phase 2

1. Run a live interview through `scripts/dev.sh`, or the deployed app after
   14b. Read `/api/stats`, then decide two things from the numbers:
   - whether browser ducking is needed (the ducking decision)
   - which hop to cut first toward the 800 ms goal
2. Decide the spec's re-ask rule for interrupted questions from the new
   interruption data. It is deliberately left out of this phase: since
   Amendment 1, the brain already records what the candidate heard.
3. Write the phase 3 plan: word-timestamped batch transcripts from the
   recordings, fuzzy quote matching with timestamps, and click-to-play.
