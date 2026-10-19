# Vapi contract: what Vapi really sends and expects

Recorded on 2026-10-19 with `scripts/spike_vapi.py` against real Vapi: two live
calls over a `vapi.websocket` transport, about $0.05 each. The fixtures in
`server/tests/fixtures/vapi/` come from the second call
(`01a15690-48f3-7331-a348-64c2e5c04886`). The first call
(`01a1568c-1ec2-700b-a8a3-19354850c60b`) found the same behaviour; the second
only changed the fake brain so it could show which request Vapi speaks.

This document is the authority where it disagrees with the plan. Tasks 4, 5, 6,
8, 9 and 11 follow it. The disagreements are listed under "What changes in the
plan" near the end.

## How the calls were made

- Create call: `POST https://api.vapi.ai/call` with the assistant and transport
  from Task 8 exactly as the plan lists them. Vapi accepted the body on the first
  try, with no transcriber or voice in it (see question 5 and "Defaults").
  Nothing in the create-call body had to change.
- A fake brain (`/spike/llm/...`) and webhook receiver (`/spike/webhook`) sat
  behind ngrok. The script streamed two spoken answers (12.2 s and 6.5 s,
  16 kHz mono) in real time as 20 ms frames, with silence frames in between. The
  second answer started 1.5 s after the interviewer's reply began, so it spoke
  over the reply.
- Scenario in order: opening line, answer 1, reply (interrupted by answer 2),
  answer 2, short reply, `{"type": "end-call"}` text frame.
- Fixtures are scrubbed: API key, Bearer tokens, capability URLs
  (`websocketCallUrl`, `listenUrl`, `controlUrl`), presigned storage URLs, the
  org id, and the ngrok host (shown as `tunnel.example`).

Fixture index:

| File | What it is |
|---|---|
| `create_call_response.json` | the `POST /call` response |
| `llm_request_turn1.json` | first model request of user turn 1 (a partial utterance) |
| `llm_request_n2.json`, `_n3`, `_n4` | requests 2 to 4 of user turn 1; n4 holds the full utterance and is the one whose reply was spoken |
| `llm_request_after_interrupt.json` | first model request after the interruption |
| `llm_request_n6.json` | the request for the full second utterance |
| `llm_request_paths.json` | for every model request: path, query, headers, arrival time, and how our reply stream ended |
| `webhook_speech_update*.json` | speech-update, one per role and status |
| `webhook_user_interrupted.json` | user-interrupted |
| `webhook_status_update_in-progress.json`, `webhook_status_update_ended.json` | status-update |
| `webhook_end_of_call_report.json` | end-of-call-report |
| `ws_text_frames.jsonl` | every text frame received on the audio socket, with seconds since connect |
| `ws_binary_frames_first20.json` | size and arrival time of the first 20 binary frames |
| `call_summary.json` | timeline, byte counts, close code from the script |
| `get_call_after.json` | `GET /call/{id}` after the call |

## 1. What path does Vapi append to `model.url`?

`/chat/completions`. We set `model.url` to `<public url>/spike/llm`, and all six
requests arrived as `POST /spike/llm/chat/completions` with an empty query
string. The catch-all routes (bare `/spike/llm` and `/spike/llm/<anything>`) were
never hit.

So for Task 8 the plan holds: `llm_url = f"{public_base_url}/vapi/llm/{brain_secret}"`
and the route Vapi calls is `/vapi/llm/{secret}/chat/completions`.

Other facts about the request: `Content-Type: application/json`,
`User-Agent: OpenAI/JS 4.89.0` (Vapi uses the OpenAI Node SDK), the body has
`"stream": true`, and an `Authorization` header is present. We configured no key
or secret, so we did not examine its value and do not rely on it. The shared
secret stays in the URL path as planned.

Evidence: `llm_request_paths.json` (`path`, `appended_to_model_url`,
`matched_chat_completions_route`, `headers`).

## 2. Where do `call.id` and the newest user utterance sit in the request body?

- Call id: `body["call"]["id"]` (a UUID). `body["call"]` is the whole call object
  (`id`, `orgId`, `type`, `status`, `transport`, `assistant`, `monitor`, ...).
- Newest user utterance: the last element of `body["messages"]`, a plain
  `{"role": "user", "content": "<string>"}`. Message objects have only `role` and
  `content`.
- `messages[0]` is a `system` message, `"You are an assistant."`. Vapi inserts a
  default because the assistant has no system prompt. `messages[1]` is the
  opening line as an `assistant` message.
- Other top-level keys: `model`, `temperature` (0.5), `max_tokens` (250),
  `stream` (true), `timestamp` (epoch ms), `assistant` (echo of our config),
  `metadata`.
- `metadata` holds `numAssistantTurns`, `numUserTurns`, `numModelRequestInTurn`
  and `assistantTurnInterrupted`. See "Surprises" for why the last one cannot be
  trusted.

The user message is cumulative for the turn: it holds everything the user has
said so far in that turn, not a delta. When a request ends with several user
messages, joining the user messages after the last `assistant` message (Task 4's
rule) gives the right text.

Evidence: `llm_request_turn1.json`, `llm_request_n4.json`,
`llm_request_after_interrupt.json`, `llm_request_n6.json`.

For Task 4's tests: `llm_request_turn1.json` is the first of four requests for
the first answer, so its `user_text` is only the first sentence ("At my last
internship, I led a migration ... to a few smaller services."). The full answer is
in `llm_request_n4.json`. Use that one where the plan says "the spoken answer's
transcript". Numbers in the transcript come back as digits ("1 big monolith",
"3 engineers") and the text is re-punctuated, so compare against the recorded
text, not against the WAV's script.

## 3. After an interruption, does the history hold the full reply or only what was spoken?

Only what was spoken. The fake brain streamed 45 words of "Request 4. Thanks,
that is helpful. I would like to dig a little deeper ..." before Vapi dropped the
stream. Vapi had spoken the first few words, and the next request's history has:

```
assistant: "Request for Thanks. That is"
user:      "Sorry to cut in."
```

(`llm_request_after_interrupt.json`, `messages[3]`). The later request for the full
second answer has the same assistant message (`llm_request_n6.json`).

Details that matter to Task 6 and Task 10:

- The text is rebuilt from the words actually spoken, not copied from our
  reply. "Request 4." came back as "Request for", and a comma became a period.
  Even a reply that played to the end changes punctuation ("Understood, thanks."
  became "Understood. Thanks."). Never compare history to our own generated text
  with `==`. The Brain should keep its own record of what it generated per turn
  and use the next request's history only to learn how much was spoken.
- There is no marker such as "[interrupted]" in the history.
- A reply that was generated but never spoken (our "Request 5" reply, which
  completed while the user was still talking) does not appear in the history.
- Replies to the earlier, discarded requests of the same turn never appear.

Evidence: `llm_request_after_interrupt.json`, `llm_request_n6.json`,
`llm_request_paths.json` (`stream_words_sent: 45` for the request that was
interrupted), `webhook_user_interrupted.json` (its `artifact.messages` already
shows the truncated bot message `Request for`).

## 4. Which text frames arrive on the audio WebSocket?

Yes, text frames arrive, and the first one arrives immediately. 270 text frames in
this call, nine types. Every frame is a JSON object with a `type`:

| `type` | Count | Shape |
|---|---|---|
| `assistant.started` | 1 | first frame (t = 0.002 s); `newAssistant` echoes the assistant config |
| `status-update` | 2 | `status`: `in-progress` (t = 0.15 s), then `ended` with `endedReason` |
| `speech-update` | 10 | `status` (`started` or `stopped`), `role` (`assistant` or `user`), `turn` (int) |
| `transcript` | 35 | `role`, `transcriptType` (`partial` or `final`), `transcript` |
| `conversation-update` | 14 | `conversation` (OpenAI-style list), `messages` (with `time`, `secondsFromStart`, `duration`) |
| `model-output` | 103 | `output` (one token or word of the model's streamed reply), `turnId` |
| `voice-input` | 103 | `input` (text handed to the voice), `turn` (int) |
| `user-interrupted` | 1 | `turnId` |
| `hangup` | 1 | no other fields; arrives right after `status-update` ended |

Things to know before parsing:

- `model-output` and `voice-input` are NOT "what was spoken". They also carry
  text from the discarded speculative requests: the `voice-input` text for
  reply turn 1 holds "Request 1. ... Request 2. ..." although only Request 4 was
  spoken. Use `speech-update`, `transcript` finals and the next LLM request's
  history to know what was said.
- User `transcript` finals arrive per phrase ("At my last internship,", "I
  wrote the plan.", ...), not per turn.
- `user-interrupted` arrives 8 ms before the assistant `speech-update stopped`.
- The same events also arrive as webhooks (same `type` names inside
  `message`). The socket frames are flat; the webhooks wrap them in
  `{"message": {...}}` and add `call` and `artifact`.

Webhooks recorded (14 events): `status-update` (in-progress, ended, 2),
`speech-update` (assistant and user, started and stopped, 10), `user-interrupted` (1),
`end-of-call-report` (1). Envelope:
`{"message": {"type": ..., "timestamp": ..., "call": {"id": ...}, "assistant": {...}, "artifact": {...}, ...}}`.
`message.call.id` is the call id for every type. The `end-of-call-report` came
about 2.6 s after the `status-update ended` webhook.

Evidence: `ws_text_frames.jsonl`, `webhook_*.json`.

## 5. Is `artifactPlan.recordingEnabled` accepted?

Yes. Vapi accepted `artifactPlan: {"recordingEnabled": false}` in the create-call
body (no error, so no retry was needed) and echoed it back in the response's
`assistant.artifactPlan`. It was honoured: the end-of-call report's
`artifact.recording` is `{"mono": {}}` with no recording URL anywhere in the
payload.

Keep it in `build_assistant` (Task 8 says "only if the spike found it accepted",
and it was).

Two things it does not turn off: Vapi still stores call logs (`logUrl`,
`presignedLogUrl` in the report), and it still ran its default end-of-call
analysis (a summary and a success evaluation by a Google model, about $0.001
together). We do not need the analysis. Disabling it is likely done with an
`analysisPlan` setting in the assistant, but that was not tested here.

Evidence: `create_call_response.json` (`assistant.artifactPlan`),
`webhook_end_of_call_report.json` (`message.artifact.recording`,
`message.analysis`), `call_summary.json` (`artifact_plan_accepted: true`).

## 6. Which sample rate comes back on the socket?

16 kHz signed 16-bit little-endian mono PCM, the same as what we asked for. The
create-call response echoes `transport.audioFormat` as
`{"sampleRate": 16000, "format": "pcm_s16le", "container": "raw"}`, and the
measurements agree:

- Every binary frame is 640 bytes (320 samples = 20 ms at 16 kHz).
- 1,185,920 bytes arrived, which is 37.06 s of audio at 16 kHz. The first frame
  came at t = 0.16 s and the `hangup` frame at t = 37.11 s, so the stream ran for
  36.95 s: a ratio of 1.003. At 24 kHz the ratio would have been 1.5.
- The byte count matches the call's wall-clock length, so Vapi streams
  continuously, silence included, for the whole call.
- Arrival is bursty, not paced. The first 20 frames (0.4 s of audio) arrived
  over 0.35 s, with runs of frames 0.1 ms apart and gaps up to 0.14 s. The
  browser side needs a small jitter buffer.

Evidence: `ws_binary_frames_first20.json`, `call_summary.json`
(`binary_bytes_received`, `first_frame_byte_sizes`, `received_seconds_if_16k`),
`create_call_response.json`.

## 7. Is the `end-call` text frame honored?

Yes. The script sent `{"type": "end-call"}` at t = 36.46 s. 0.65 s later a
`status-update` frame with `status: "ended"` arrived, then a `hangup` frame, and
about 1.1 s after that Vapi closed the socket (close code 1005, no reason).
`GET /call/{id}` then reported `status: "ended"`. The `status-update ended` and
`end-of-call-report` webhooks followed.

Oddity: Vapi labels the end `endedReason: "assistant-ended-call-after-message-spoken"`
even though the client asked for it. Task 9 should treat `end-call` as ours and not
read meaning into that string. It is the same string in the webhook, the report and
`GET /call`.

Evidence: `ws_text_frames.jsonl` (last three frames), `call_summary.json`
(`marks.end_call_sent`, `marks.closed_after_end_call`, `close`,
`get_call_status_after`), `get_call_after.json`, `webhook_status_update_ended.json`.

## Surprises, and what changes in the plan

1. **Vapi sends several model requests per user turn.** Turn 1 produced four
   requests, each with a longer partial transcript (`numModelRequestInTurn` 1 to 4;
   see `llm_request_turn1.json` and `llm_request_n2.json` to `_n4.json`). Vapi then
   speaks the reply to the last one. Our fake streams for requests 1 to 3 were
   cancelled (the connection dropped) within about a second of starting, before the
   next request arrived; request 4's stream was cancelled when the user
   interrupted. After the interruption there were two more requests: the first
   (`llm_request_after_interrupt.json`, "Sorry to cut in.") got a complete reply
   that was never spoken, because the user kept talking; the second
   (`llm_request_n6.json`) was spoken.
   See `llm_request_paths.json` (`stream_outcome`, `arrived_at_s`,
   `stream_ended_at_s`).
   - **Task 6 (Brain), Task 11 (route):** a request is not a turn. The Brain must
     not advance the interview, save a turn, or count a question until its reply
     stream has been fully consumed, and it must tolerate being cancelled mid-stream.
     Several requests per turn means several calls to Claude per turn unless the
     route waits a moment or debounces. In turn 1 the four requests arrived 1.0 s,
     2.2 s and 3.0 s apart.
   - A fast model finishes its stream before Vapi cancels it (our slow fake kept
     streams open on purpose). So "completed" does not mean "spoken". Only the
     history in the next request says what was spoken.
2. **`metadata.assistantTurnInterrupted` is `true` on every request**, including
   the very first one, where nothing was interrupted. Do not use it. Use the
   `user-interrupted` webhook or the history instead.
3. **History is rebuilt text** (question 3). Plan Task 4 and Task 6 must not
   assume `history == what we generated`.
4. **`llm_request_turn1.json` is a partial utterance** (question 2). Task 4's test
   for "the spoken answer's transcript" should use `llm_request_n4.json`.
5. **Capability URLs travel in every request and webhook.** The `call` object in
   LLM requests and webhooks has `monitor.listenUrl` and `monitor.controlUrl`, and
   `transport.websocketCallUrl`. Anyone holding them can listen to or control the
   call. Do not log full request bodies in the deployed server, and keep them out
   of fixtures (the scrubber replaces them).
6. **Defaults Vapi filled in** because the assistant did not set them: the system
   prompt "You are an assistant.", transcriber Deepgram `nova-2`, voice provider
   `vapi` voice `Elliot` model `vapi-v2`, and the end-of-call analysis. None were
   required for the create call to succeed. Task 8 should decide whether to set a
   system prompt (our Brain ignores the system message) and disable the analysis.
7. **Text is normalised.** The opening line "Hi, this is a Mockingbird test call."
   shows up as "Hi. This is a Mockingbird test call." in history and transcripts.
8. **Cost.** The 37 s call cost $0.0469 (stt $0.0065, tts $0.0083, Vapi $0.031,
   llm $0 because the model is ours). That is roughly $0.075 per minute with the
   default voice and transcriber.
9. **Latency.** `artifact.performanceMetrics.turnLatencies` in the report gives
   per-turn numbers (model 283 ms and 226 ms, voice 505 ms and 796 ms, endpointing
   102 ms and 1855 ms; total turn latency 1.05 s and 2.90 s). Vapi's own endpointing
   (deciding the user stopped) was 1.9 s on the second turn.

## What was not verified

- The value of the `Authorization` header Vapi sends to the custom-LLM URL (the
  recorder redacted it before saving).
- Webhook request headers (the recorder did not save them).
- Whether `analysisPlan` turns off the default analysis.
- What the stream cancellation looks like for a real Claude stream. Our fake
  streamed at about 16 words a second, so the cancellation timings above are for
  that pace.
- Behaviour on a long idle gap, a second call on the same assistant, or a
  rejected body (the only create-call body tried was accepted).
